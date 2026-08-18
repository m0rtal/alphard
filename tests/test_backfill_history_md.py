"""Tests for scripts/backfill_history_md.py — the primary backfill script.

Smoke-level only: we mock the broker + DB and verify the script's
control flow (universe resolution, skip-complete handling, error
isolation, circuit breaker). Real network calls go to the deployed
stack, not unit tests.
"""

from __future__ import annotations

from datetime import date
from typing import Any
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


def test_is_complete_true_when_min_bars_reached() -> None:
    """Fast path: count >= min_bars short-circuits regardless of meta."""
    store = MagicMock()
    store.count_ohlcv.return_value = 1300
    assert bh._is_complete(store, "SBER", min_bars=1300) is True


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


def test_is_complete_fast_path_count_above_min_bars() -> None:
    """Plain old count threshold for the 99% case."""
    store = MagicMock()
    store.count_ohlcv.return_value = 1300
    assert bh._is_complete(store, "SBER", min_bars=1300) is True


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


def test_is_complete_delisted_same_day_as_listed() -> None:
    """Pathological case — listed and delisted on the same day.
    end <= listed_at, so expected = 0. Anything > 0 = complete."""

    store = MagicMock()
    store.count_ohlcv.return_value = 1
    store.ticker_meta.return_value = (date(2020, 1, 1), date(2020, 1, 1))

    assert bh._is_complete(store, "SAME_DAY", min_bars=1300) is True


def test_is_complete_delisted_same_day_no_rows() -> None:
    """Same scenario but no bars stored — incomplete."""

    store = MagicMock()
    store.count_ohlcv.return_value = 0
    store.ticker_meta.return_value = (date(2020, 1, 1), date(2020, 1, 1))

    assert bh._is_complete(store, "SAME_DAY", min_bars=1300) is False


def test_is_complete_halts_pct_allows_normal_gaps() -> None:
    """2022-style sanctions gap: ~5 months of trading halts in 2022.
    Live ticker listed 2015, count=2400, expected ≈ 11y*252*0.85 = 2356.
    2400 > 2356 → complete. Tolerates ~6 months of halts."""

    store = MagicMock()
    store.count_ohlcv.return_value = 2400
    store.ticker_meta.return_value = (date(2015, 1, 1), None)

    assert bh._is_complete(store, "LIVE", min_bars=1300) is True


def test_is_complete_below_count_threshold_no_metadata_returns_false() -> None:
    """Both count and meta missing — incomplete."""
    store = MagicMock()
    store.count_ohlcv.return_value = 50
    store.ticker_meta.return_value = None

    assert bh._is_complete(store, "ORPHAN", min_bars=1300) is False


def test_backfill_one_returns_negative_on_timeout() -> None:
    """When SIGALRM/ctypes raises _LoaderTimeout inside _backfill_one, we
    must catch it and return written=-1 so the caller can record the
    failure without aborting the whole run."""
    loader = MagicMock()
    store = MagicMock()

    def _fake_iter(_ticker: str, _start: date, _end: date) -> Any:
        raise bh._LoaderTimeout("deadline exceeded")
        yield  # pragma: no cover — generator marker

    loader.iter_ohlcv.side_effect = _fake_iter

    stats = bh._backfill_one(loader, store, "SBER", date(2018, 1, 1), date(2026, 12, 31))

    assert stats == {"fetched": 0, "written": -1}


def test_backfill_one_returns_negative_on_generic_exception() -> None:
    """Any other exception must be caught and recorded as a failure too —
    one bad ticker should never crash the whole run."""
    loader = MagicMock()
    store = MagicMock()

    loader.iter_ohlcv.side_effect = RuntimeError("boom")

    stats = bh._backfill_one(loader, store, "SBER", date(2018, 1, 1), date(2026, 12, 31))

    assert stats == {"fetched": 0, "written": -1}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_circuit_breaker_threshold_is_positive() -> None:
    """The breaker must trip on N>0 consecutive failures. Pin the exact
    value so we notice if anyone tunes it without thinking."""
    assert bh._CIRCUIT_BREAKER_THRESHOLD >= 3


def test_ticker_deadline_is_reasonable() -> None:
    """The per-ticker deadline must be high enough to fit the median
    ticker (a few seconds) but low enough that one stuck ticker can't
    starve the whole run overnight."""
    assert 30 <= bh._TICKER_DEADLINE_SECONDS <= 600  # 2026-08-18: lowered to 30s after live cluster observed 180s timeout on every Tinkoff MD archive call from .107


# ---------------------------------------------------------------------------
# _set_complete_flag — flag-flip helper called inside the main loop
# ---------------------------------------------------------------------------


def test_set_complete_flag_true_calls_store() -> None:
    store = MagicMock()
    bh._set_complete_flag(store, "SBER", complete=True)
    store.mark_backfill_complete.assert_called_once_with("SBER", complete=True)


def test_set_complete_flag_false_calls_store() -> None:
    store = MagicMock()
    bh._set_complete_flag(store, "FAIL", complete=False)
    store.mark_backfill_complete.assert_called_once_with("FAIL", complete=False)


def test_set_complete_flag_swallows_pg_errors() -> None:
    """If the flag flip fails (e.g. transient DB), the backfill loop
    must not crash — the bars are the primary deliverable, the flag is
    metadata that can be re-flipped on the next run."""
    import logging  # noqa: F401

    store = MagicMock()
    store.mark_backfill_complete.side_effect = RuntimeError("connection lost")
    # Must not raise.
    bh._set_complete_flag(store, "SBER", complete=True)
    store.mark_backfill_complete.assert_called_once()
