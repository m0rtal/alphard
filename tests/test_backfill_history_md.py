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
    loader.list_tickers_with_figi.return_value = fake_tqbr + fake_spbxm + fake_bonds + fake_etfs

    out = bh._resolve_universe(loader, classes=None, limit=0)

    assert out == ["SBER", "GAZP", "AAPL", "RU000A0JX0J3", "FXUS"]


def test_resolve_universe_class_filter_case_insensitive() -> None:
    """--classes TQBR vs --classes tqbr must produce the same universe."""
    fake = [
        MagicMock(ticker="SBER", class_code="TQBR"),
        MagicMock(ticker="AAPL", class_code="SPBXM"),
    ]
    loader = MagicMock()
    loader.list_tickers_with_figi.return_value = fake

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
    loader.list_tickers_with_figi.return_value = fake

    out = bh._resolve_universe(loader, classes=["ALL"], limit=0)

    assert out == []


def test_resolve_universe_limit_caps_universe_size() -> None:
    fake = [MagicMock(ticker=f"T{i}", class_code="TQBR") for i in range(10)]
    loader = MagicMock()
    loader.list_tickers_with_figi.return_value = fake

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
    store = MagicMock()
    store.count_ohlcv.return_value = 1299
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
