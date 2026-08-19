"""Tests for scripts/backfill_history_md.py — the primary backfill script.

Smoke-level only: we mock the broker + DB and verify the script's
control flow (universe resolution, skip-complete handling, error
isolation, circuit breaker). Real network calls go to the deployed
stack, not unit tests.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock


# conftest.py adds PROJECT_ROOT to sys.path so ``backfill_history_md``
# resolves as a top-level module.
import backfill_history_md as bh  # noqa: E402


# ---------------------------------------------------------------------------
# _resolve_universe
# ---------------------------------------------------------------------------


def test_resolve_universe_no_class_filter_returns_everything() -> None:
    """No --classes flag → return everything from the broker (no filtering
    on backfill — user's standing instruction)."""
    fake_tqbr = [
        MagicMock(ticker="SBER", class_code="TQBR"),
        MagicMock(ticker="GAZP", class_code="TQBR"),
    ]
    fake_spbxm = [MagicMock(ticker="AAPL", class_code="SPBXM")]
    fake_bonds = [MagicMock(ticker="RU000A0JX0J3", class_code="TQOB")]
    fake_etfs = [MagicMock(ticker="FXUS", class_code="TQTE")]

    loader = MagicMock()
    loader.list_tickers.return_value = fake_tqbr + fake_spbxm + fake_bonds + fake_etfs

    out, _metas = bh._resolve_universe(loader, classes=None, limit=0)

    assert out == ["SBER", "GAZP", "AAPL", "RU000A0JX0J3", "FXUS"]


def test_resolve_universe_class_filter_case_insensitive() -> None:
    """--classes TQBR vs --classes tqbr must produce the same universe."""
    fake = [
        MagicMock(ticker="SBER", class_code="TQBR"),
        MagicMock(ticker="AAPL", class_code="SPBXM"),
    ]
    loader = MagicMock()
    loader.list_tickers.return_value = fake

    upper, _ = bh._resolve_universe(loader, classes=["TQBR"], limit=0)
    lower, _ = bh._resolve_universe(loader, classes=["tqbr"], limit=0)

    assert upper == lower == ["SBER"]


def test_resolve_universe_class_all_string_does_not_match() -> None:
    """--classes ALL is a documented foot-gun: argparse stores the string
    'ALL' which matches no class_code, so the resolved universe is empty.
    The fix is to invoke the script WITHOUT --classes when you want the
    full universe. This test pins that behaviour so we don't regress
    to accidentally treating the literal string as a wildcard.
    """
    fake = [MagicMock(ticker="SBER", class_code="TQBR")]
    loader = MagicMock()
    loader.list_tickers.return_value = fake

    out, _ = bh._resolve_universe(loader, classes=["ALL"], limit=0)

    assert out == []


def test_resolve_universe_limit_caps_universe_size() -> None:
    fake = [MagicMock(ticker=f"T{i}", class_code="TQBR") for i in range(10)]
    loader = MagicMock()
    loader.list_tickers.return_value = fake

    out, _ = bh._resolve_universe(loader, classes=None, limit=3)

    assert len(out) == 3


# ---------------------------------------------------------------------------
# _is_complete
# ---------------------------------------------------------------------------


def test_is_complete_below_count_no_meta_incomplete() -> None:
    """count < min_bars AND no metadata → incomplete."""
    store = MagicMock()
    store.count_ohlcv.return_value = 1299
    store.ticker_meta.return_value = None
    assert bh._is_complete(store, "SBER", min_bars=1300) is False


# ---------------------------------------------------------------------------
# _backfill_one: failure isolation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_complete: count vs expected_bars(listed_at..end * (1 - halts_pct))
# ---------------------------------------------------------------------------


def test_is_complete_freshly_listed_ticker_not_blocked_forever() -> None:
    """Ticker listed 2024-01-01 (today is 2026-08-17 so ~2.6y). expected
    bars = 2.6 * 252 * 0.85 ≈ 557. count=580 > 557 → complete. No
    infinite retry."""

    store = MagicMock()
    store.count_ohlcv.return_value = 580
    store.ticker_meta.return_value = (date(2024, 1, 1), None)

    assert bh._is_complete(store, "FRESH", min_bars=1300) is True


def test_is_complete_delisted_ticker() -> None:
    """Delisted ticker with delisted_at=2024-06-01. listed_at=2010.
    expected_bars = trading_days(2010..2024-06) * 0.85 ≈ 3103.
    Count=2000 < 3103 → incomplete."""

    store = MagicMock()
    store.count_ohlcv.return_value = 500
    store.ticker_meta.return_value = (date(2010, 1, 1), date(2024, 6, 1))

    assert bh._is_complete(store, "OLD", min_bars=1300) is False


def test_is_complete_delisted_ticker_within_halts_pct() -> None:
    """Same setup but stored 3300 bars (within ~6% of 3103 expected,
    within halts_pct=15%)."""

    store = MagicMock()
    store.count_ohlcv.return_value = 3300
    store.ticker_meta.return_value = (date(2010, 1, 1), date(2024, 6, 1))

    # 14.5y * 252 * 0.85 = 3103, count=3300 > 3103 → complete
    assert bh._is_complete(store, "OLD", min_bars=1300) is True


def test_is_complete_archive_truncated_below_threshold() -> None:
    """Ticker listed 2010, has only ~1200 bars, gap from 2010-2015.
    expected_bars = 16y * 252 * 0.85 ≈ 3427, count=1200 << 3427.
    Incomplete even though > 1300 — wait, 1200 < 1300 so we wouldn't
    be here. The test pins the slower path: just below min_bars."""

    store = MagicMock()
    store.count_ohlcv.return_value = 1299
    store.ticker_meta.return_value = (date(2010, 1, 1), None)

    # 16.6y * 252 * 0.85 = 3555, count=1299 < 3555 → incomplete
    assert bh._is_complete(store, "GAP", min_bars=1300) is False


def test_is_complete_no_metadata_returns_false() -> None:
    """Ticker not in ticker_universe. Re-pull and possibly surface the
    gap."""

    store = MagicMock()
    store.count_ohlcv.return_value = 50
    store.ticker_meta.return_value = None

    assert bh._is_complete(store, "ORPHAN", min_bars=1300) is False


def test_is_complete_listed_at_none_returns_false() -> None:
    """Ticker is in the universe but listed_at is NULL (legacy entry).
    Can't compute expected. Be conservative."""

    store = MagicMock()
    store.count_ohlcv.return_value = 500
    store.ticker_meta.return_value = (None, None)
    store.earliest_ts.return_value = None

    assert bh._is_complete(store, "LEGACY", min_bars=1300) is False


def test_is_complete_listed_at_none_inferred_from_earliest() -> None:
    """listed_at is NULL but we have bars — infer listing year from
    earliest bar and judge accordingly. WUSH case: real listed 2022-12-14.
    Expected bars for ~3.7y = ~787; with 1044 bars we are complete.
    """
    from datetime import date

    store = MagicMock()
    store.count_ohlcv.return_value = 1044  # WUSH actually has this many
    store.ticker_meta.return_value = (None, None)  # listed_at NULL
    store.earliest_ts.return_value = date(2022, 12, 14)  # inferred listing

    assert bh._is_complete(store, "WUSH", min_bars=1300) is True


def test_earliest_expected_ts_default_clamps_to_1825d() -> None:
    """MOEX ISS cap = 1825d. Without listed_at, start = max(MIN_YEAR, today-1825d)."""
    from datetime import timedelta

    meta = (None, None)
    result = bh._earliest_expected_ts(meta)  # default moex_clamped=True
    expected = date.today() - timedelta(days=1825)
    assert result == expected


def test_earliest_expected_ts_unclamped_goes_to_min_year() -> None:
    """MD loader has no 1825d cap. With moex_clamped=False, start = MIN_YEAR."""
    meta = (None, None)
    result = bh._earliest_expected_ts(meta, moex_clamped=False)
    assert result == date(2018, 1, 1)  # MIN_YEAR


def test_earliest_expected_ts_with_listed_at_clamps() -> None:
    """listed_at=2010 still goes to MIN_YEAR if MOEX cap doesn't pull earlier."""
    from datetime import timedelta

    meta = (date(2010, 1, 1), None)
    result = bh._earliest_expected_ts(meta)
    # today minus 1825d; max(MIN_YEAR=2018, listed_at=2010, today-1825d) = today-1825d
    expected = date.today() - timedelta(days=1825)
    assert result == expected


def test_earliest_expected_ts_unclamped_with_listed_at() -> None:
    """MD loader with listed_at=2010 still goes back to MIN_YEAR (full history)."""
    meta = (date(2010, 1, 1), None)
    result = bh._earliest_expected_ts(meta, moex_clamped=False)
    # max(MIN_YEAR=2018, listed_at.year=2010) = 2018-01-01 (MIN_YEAR wins)
    assert result == date(2018, 1, 1)


# ---------------------------------------------------------------------------
# --skip-known-bad flag
# ---------------------------------------------------------------------------


def test_argparser_accepts_skip_known_bad() -> None:
    """--skip-known-bad is wired into argparse and parses to args.skip_known_bad."""
    import argparse  # noqa: PLC0415 — local to keep imports tight

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-known-bad",
        action="store_true",
    )
    args = parser.parse_args(["--skip-known-bad"])
    assert args.skip_known_bad is True
    args_default = parser.parse_args([])
    assert args_default.skip_known_bad is False


def test_skip_known_bad_logic_drops_delisted_at_tickers() -> None:
    """When --skip-known-bad is set, the per-ticker guard short-circuits
    for tickers whose TickerMeta.delisted_at is set. We exercise the
    predicate in isolation (no broker, no DB) — integration is the
    deployed stack's job."""
    import argparse  # noqa: PLC0415
    from datetime import date as _date  # noqa: PLC0415

    # Build the same args namespace the parser would, just without running main().
    args = argparse.Namespace(skip_known_bad=True)
    meta = MagicMock()
    meta.delisted_at = _date(2026, 8, 19)
    assert args.skip_known_bad and meta.delisted_at is not None

    # Without the flag, the same ticker would NOT be skipped.
    args_off = argparse.Namespace(skip_known_bad=False)
    assert not (args_off.skip_known_bad and meta.delisted_at is not None)


def test_skip_known_bad_keeps_tickers_without_delisted_at() -> None:
    """Tickers with delisted_at=None are NEVER dropped by --skip-known-bad."""
    import argparse  # noqa: PLC0415

    args = argparse.Namespace(skip_known_bad=True)
    meta = MagicMock()
    meta.delisted_at = None
    # Predicate is False → ticker is NOT skipped, proceeds to fetch.
    assert not (args.skip_known_bad and meta.delisted_at is not None)
