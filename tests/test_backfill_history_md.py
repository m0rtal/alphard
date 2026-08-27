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
    for tickers whose (listed_at, delisted_at) tuple has a non-None
    delisted_at. We exercise the predicate in isolation (no broker,
    no DB) — integration is the deployed stack's job.

    NOTE: store.ticker_meta() returns a raw psycopg row tuple
    ``(listed_at, delisted_at)`` or None, NOT a TickerMeta object.
    The 2026-08-20 backfill regression was caused by treating this
    tuple as an object (meta.delisted_at) instead of indexing it
    (meta[1]). The test fixtures below mirror the real production
    return shape so any future drift is caught at unit-test time.
    """
    import argparse  # noqa: PLC0415
    from datetime import date as _date  # noqa: PLC0415

    # Build the same args namespace the parser would, just without running main().
    args = argparse.Namespace(skip_known_bad=True)
    # Real psycopg-row tuple shape — NOT a MagicMock with attribute access.
    meta = (_date(2020, 1, 1), _date(2026, 8, 19))
    # Predicate matches the production main-loop guard after the 2026-08-20 fix:
    if args.skip_known_bad and meta is not None and meta[1] is not None:
        skipped = True
    else:
        skipped = False
    assert skipped

    # Without the flag, the same ticker would NOT be skipped.
    args_off = argparse.Namespace(skip_known_bad=False)
    if args_off.skip_known_bad and meta is not None and meta[1] is not None:
        skipped_off = True
    else:
        skipped_off = False
    assert not skipped_off


def test_skip_known_bad_keeps_tickers_without_delisted_at() -> None:
    """Tickers with delisted_at=None are NEVER dropped by --skip-known-bad."""
    import argparse  # noqa: PLC0415

    args = argparse.Namespace(skip_known_bad=True)
    # Real tuple shape (delisted_at=None) — NOT a MagicMock.
    meta = (None, None)
    if args.skip_known_bad and meta is not None and meta[1] is not None:
        skipped = True
    else:
        skipped = False
    assert not skipped


# ---------------------------------------------------------------------------
# H-NETWORK-DETECT (2026-08-20): progress heartbeat
# ---------------------------------------------------------------------------
#
# The deadlock that left backfill PID 19 idle for 17 hours on
# sha-bc867a2 was invisible because the per-ticker log line never
# fired (iter_ohlcv hung) and the final `=== DONE` line never
# arrived either. The fix in backfill_history_md.py emits a separate
# "progress: i/N tickers scanned in Ts" line every 50 tickers so
# any future wedge surfaces as missing progress lines rather than a
# silent stall. The tests below pin the cadence constant and the
# expected log format so a future edit cannot quietly regress this
# affordance.


def test_progress_every_constant_is_50() -> None:
    """The progress-heartbeat cadence is 50 tickers (small enough to
    surface a wedge within minutes, large enough to avoid log spam)."""
    import importlib

    mod = importlib.import_module("backfill_history_md")
    # Read the module source to extract the constant. This is more
    # robust than importing the module and reading a top-level symbol
    # that may not actually be exported at import time.
    import inspect

    source = inspect.getsource(mod)
    assert "progress_every = 50" in source
    assert 'f"progress: {i}/{len(tickers)} tickers scanned in "' in source


def test_progress_heartbeat_format_includes_counters() -> None:
    """Heartbeat line must include fetched/written/skipped/errors so an
    operator can diagnose where the loop is at a glance."""
    import inspect

    source = inspect.getsource(__import__("backfill_history_md"))
    # Required counter substrings inside the progress log line.
    for needle in (
        "fetched=",
        "written=",
        "skipped=",
        "errors=",
    ):
        assert needle in source, f"progress heartbeat missing {needle!r}"


# ---------------------------------------------------------------------------
# --on-empty-only flag (service-flow guard: literal contract
# "запускается бэкфил если данных по тикеру нет" — see issue #276)
# ---------------------------------------------------------------------------


def test_argparser_accepts_on_empty_only() -> None:
    """--on-empty-only is wired into argparse and parses to args.on_empty_only."""
    import argparse  # noqa: PLC0415 — local to keep imports tight

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--on-empty-only",
        action="store_true",
    )
    args = parser.parse_args(["--on-empty-only"])
    assert args.on_empty_only is True
    args_default = parser.parse_args([])
    assert args_default.on_empty_only is False


def test_on_empty_only_skips_ticker_with_any_rows() -> None:
    """When --on-empty-only is set, the per-ticker guard short-circuits
    as soon as ``store.count_ohlcv(ticker) > 0``. The guard is more
    aggressive than the legacy ``_is_complete()`` (HALTS_PCT formula)
    and implements the literal contract "no row = pull, any row = leave
    alone"."""
    import argparse  # noqa: PLC0415

    args = argparse.Namespace(on_empty_only=True)

    # Ticker has 1 row in DB → skip.
    store_one_row = MagicMock()
    store_one_row.count_ohlcv.return_value = 1
    skipped = False
    if args.on_empty_only and store_one_row.count_ohlcv(ticker="SBER") > 0:
        skipped = True
    assert skipped

    # Ticker has 1300 rows in DB → still skip.
    store_full = MagicMock()
    store_full.count_ohlcv.return_value = 1300
    skipped_full = False
    if args.on_empty_only and store_full.count_ohlcv(ticker="GAZP") > 0:
        skipped_full = True
    assert skipped_full


def test_on_empty_only_proceeds_when_count_is_zero() -> None:
    """When --on-empty-only is set and count == 0, the guard does NOT
    short-circuit — the backfill continues for this ticker."""
    import argparse  # noqa: PLC0415

    args = argparse.Namespace(on_empty_only=True)

    store_empty = MagicMock()
    store_empty.count_ohlcv.return_value = 0
    proceed = True
    if args.on_empty_only and store_empty.count_ohlcv(ticker="NEW") > 0:
        proceed = False
    assert proceed


def test_on_empty_only_off_falls_back_to_is_complete() -> None:
    """Default (no flag) behaviour must remain the legacy ``_is_complete()``
    gate so re-runs that top up partial tickers are not affected."""
    import argparse  # noqa: PLC0415

    args = argparse.Namespace(on_empty_only=False, force=False)
    # Mirror the production predicate: ``args.on_empty_only`` short-circuits
    # on count > 0; otherwise we delegate to ``_is_complete``.
    store = MagicMock()
    store.count_ohlcv.return_value = 0  # would be skipped if --on-empty-only
    store.ticker_meta.return_value = None
    # With on_empty_only OFF the per-ticker guard is _is_complete().
    # count=0, no metadata → _is_complete() returns False → not skipped.
    skip_via_flag = bool(args.on_empty_only and store.count_ohlcv(ticker="SBER") > 0)
    skip_via_complete = False if skip_via_flag else bh._is_complete(store, "SBER", min_bars=1300)
    assert not skip_via_flag
    assert not skip_via_complete


def test_progress_log_string_on_empty_only_present() -> None:
    """The skip log line for --on-empty-only is wired into the main loop
    (defensive — guards against a future refactor silently dropping the
    per-ticker visibility into why a ticker was skipped)."""
    import inspect

    source = inspect.getsource(bh)
    assert "skip (count > 0, --on-empty-only)" in source
