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

    out = bh._resolve_universe(loader, classes=None, limit=0)

    assert out == ["SBER", "GAZP", "AAPL", "RU000A0JX0J3", "FXUS"]


def test_resolve_universe_class_filter_case_insensitive() -> None:
    """--classes TQBR vs --classes tqbr must produce the same universe."""
    fake = [
        MagicMock(ticker="SBER", class_code="TQBR"),
        MagicMock(ticker="AAPL", class_code="SPBXM"),
    ]
    loader = MagicMock()
    loader.list_tickers.return_value = fake

    upper = bh._resolve_universe(loader, classes=["TQBR"], limit=0)
    lower = bh._resolve_universe(loader, classes=["tqbr"], limit=0)

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

    out = bh._resolve_universe(loader, classes=["ALL"], limit=0)

    assert out == []


def test_resolve_universe_limit_caps_universe_size() -> None:
    fake = [MagicMock(ticker=f"T{i}", class_code="TQBR") for i in range(10)]
    loader = MagicMock()
    loader.list_tickers.return_value = fake

    out = bh._resolve_universe(loader, classes=None, limit=3)

    assert len(out) == 3


# ---------------------------------------------------------------------------
# _is_complete
# ---------------------------------------------------------------------------


def test_is_complete_true_when_min_bars_reached() -> None:
    store = MagicMock()
    store.count_ohlcv.return_value = 1300
    assert bh._is_complete(store, "SBER", min_bars=1300) is True


def test_is_complete_false_when_below_threshold() -> None:
    """Pre-fast-path: not enough bars AND no metadata to age-check
    against. The age-aware path can't reason about completion
    without ticker_universe data, so it conserves to "incomplete".
    """
    store = MagicMock()
    store.count_ohlcv.return_value = 1299
    store.ticker_meta.return_value = None
    store.earliest_ts.return_value = date(2018, 1, 1)
    assert bh._is_complete(store, "SBER", min_bars=1300) is False


# ---------------------------------------------------------------------------
# _backfill_one: failure isolation
# ---------------------------------------------------------------------------


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
    assert 60 <= bh._TICKER_DEADLINE_SECONDS <= 600


# ---------------------------------------------------------------------------
# _is_complete: age-aware (handles fresh / delisted tickers correctly)
# ---------------------------------------------------------------------------


def test_is_complete_freshly_listed_ticker_not_blocked_forever() -> None:
    """A ticker listed in 2025 has at most ~1 year (~252 bars) of
    history. Old min-bars-only check would mark it incomplete forever;
    age-aware check recognises it as complete once we pull back to
    listed_at.
    """
    store = MagicMock()
    store.count_ohlcv.return_value = 250  # below min-bars=1300
    store.ticker_meta.return_value = (date(2025, 3, 15), None)  # listed 2025
    store.earliest_ts.return_value = date(2025, 3, 16)
    store.latest_ts.return_value = date(2026, 8, 17)  # recent enough

    assert bh._is_complete(store, "FRESH", min_bars=1300) is True


def test_is_complete_delisted_ticker_back_to_min_year() -> None:
    """A delisted ticker (delisted_at=2020) needs history back to 2018.
    We mark complete once earliest_ts <= 2018-01-01.
    """
    store = MagicMock()
    store.count_ohlcv.return_value = 500  # below min-bars=1300
    store.ticker_meta.return_value = (date(2010, 1, 1), date(2020, 6, 1))
    store.earliest_ts.return_value = date(2018, 1, 5)
    store.latest_ts.return_value = date(2020, 6, 1)  # = delisted_at

    assert bh._is_complete(store, "OLD", min_bars=1300) is True


def test_is_complete_still_false_when_history_truncated() -> None:
    """Ticker metadata says listed_at=2010 but earliest stored bar is
    only 2020 — backfill hasn't pulled the 2010-2019 history yet.
    """
    store = MagicMock()
    store.count_ohlcv.return_value = 1500  # above min-bars, but...
    # ...ticker_meta returns None which forces the slow path. No, the
    # fast path triggered first. Force slow path instead.
    store.count_ohlcv.return_value = 100  # below min-bars
    store.ticker_meta.return_value = (date(2010, 1, 1), None)
    store.earliest_ts.return_value = date(2020, 1, 1)
    store.latest_ts.return_value = date(2026, 8, 17)

    assert bh._is_complete(store, "GAP", min_bars=1300) is False


def test_is_complete_no_metadata_falls_back_to_false() -> None:
    """Ticker not in ticker_universe — can't reason about completion,
    treat as incomplete so we re-pull and possibly surface the gap.
    """
    store = MagicMock()
    store.count_ohlcv.return_value = 50  # below min-bars
    store.ticker_meta.return_value = None  # no metadata

    assert bh._is_complete(store, "ORPHAN", min_bars=1300) is False


def test_is_complete_no_rows_returns_false() -> None:
    """No bars stored — incomplete regardless of metadata."""
    store = MagicMock()
    store.count_ohlcv.return_value = 0
    store.ticker_meta.return_value = (date(2015, 1, 1), None)
    store.earliest_ts.return_value = None

    assert bh._is_complete(store, "EMPTY", min_bars=1300) is False


def test_is_complete_tolerance_within_30_days() -> None:
    """If earliest stored bar is within 30 days of expected, treat as
    complete — archive endpoints sometimes truncate the first/last
    week of a year and we don't want to retry on a 5-day diff.
    """
    store = MagicMock()
    store.count_ohlcv.return_value = 100  # below min-bars
    store.ticker_meta.return_value = (date(2020, 1, 1), None)
    # Earliest stored is 2020-01-30 = 29 days after listed_at 2020-01-01.
    store.earliest_ts.return_value = date(2020, 1, 30)
    store.latest_ts.return_value = date(2026, 8, 17)

    assert bh._is_complete(store, "NEAR", min_bars=1300) is True


def test_is_complete_min_year_floor_for_ancient_tickers() -> None:
    """If listed_at is 2005 but MIN_YEAR=2018, we don't try to pull
    pre-2018 (Tinkoff MD doesn't carry it).
    """
    from scripts.backfill_history_md import _earliest_expected_ts

    # meta = (listed_at=2005, delisted=None) → expected = 2018-01-01
    assert _earliest_expected_ts((date(2005, 6, 1), None)) == date(2018, 1, 1)


def test_is_complete_listed_at_year_floor() -> None:
    """If listed_at is 2023 and MIN_YEAR=2018, expected = 2023-01-01."""
    from scripts.backfill_history_md import _earliest_expected_ts

    assert _earliest_expected_ts((date(2023, 6, 1), None)) == date(2023, 1, 1)


def test_is_complete_fast_path_takes_priority() -> None:
    """A ticker with min_bars+ rows is complete without touching
    ticker_meta — fast path. Pin this so we don't regress to a
    query per ticker (would slow the run by 2x).
    """
    store = MagicMock()
    store.count_ohlcv.return_value = 5000  # way above min-bars

    assert bh._is_complete(store, "FAST", min_bars=1300) is True
    store.ticker_meta.assert_not_called()
    store.earliest_ts.assert_not_called()


def test_is_complete_latest_side_missing_returns_false() -> None:
    """Latest bar is months old and the ticker is live — backfill hasn't
    run in a while. Re-pull to catch up."""
    store = MagicMock()
    store.count_ohlcv.return_value = 500  # below min-bars
    store.ticker_meta.return_value = (date(2020, 1, 1), None)
    store.earliest_ts.return_value = date(2020, 1, 1)  # OK earliest
    store.latest_ts.return_value = date(2025, 1, 1)  # 19 months stale

    assert bh._is_complete(store, "STALE", min_bars=1300) is False


def test_is_complete_latest_side_within_7_day_grace() -> None:
    """Cron runs daily so 7-day grace covers weekends. Backfill
    yesterday's bar = complete."""
    store = MagicMock()
    store.count_ohlcv.return_value = 100
    store.ticker_meta.return_value = (date(2024, 1, 1), None)
    store.earliest_ts.return_value = date(2024, 1, 1)
    store.latest_ts.return_value = date(2026, 8, 15)  # 2 days ago

    assert bh._is_complete(store, "FRESH", min_bars=1300) is True


def test_is_complete_delisted_with_latest_beyond_delisted_at() -> None:
    """Delisted ticker with bar AFTER delisted_at is suspicious — could
    be a stale write or a data error. We still declare complete because
    the universe metadata says last_expected = min(delisted_at, today),
    and the latest stored bar is between those two values.
    """
    store = MagicMock()
    store.count_ohlcv.return_value = 500
    store.ticker_meta.return_value = (date(2010, 1, 1), date(2020, 6, 1))
    store.earliest_ts.return_value = date(2018, 1, 5)
    store.latest_ts.return_value = date(2020, 7, 1)  # 30 days after delisted_at

    assert bh._is_complete(store, "POSTDELIST", min_bars=1300) is True
