"""Regression test for cycle146 backfill speedup.

The supervisor was observed (2026-09-01) to spend ~5s/ticker even for
foreign SPBXM tickers that always return LoaderNotFoundError from both
sources. The cause: ``FallbackDataLoader.iter_ohlcv`` retries every
source for every ticker with no per-process memoisation of the
"this ticker is unknown to this source" decision.

Fix: add a per-loader-instance cache ``_source_skip[ticker][source]`` that
short-circuits subsequent calls when a ticker raises LoaderNotFoundError
or returns 0 bars from a given source. Cache is process-local; if the
supervisor restarts, the cache is rebuilt (the supervisor is
resume-safe and re-fetches missing tickers from scratch).

Test asserts:
- First call attempts every source in order (records source attempts).
- Second call to the same ticker SKIPS sources that previously returned
  0 bars / LoaderNotFoundError (does NOT re-attempt them).
- Cache does not affect tickers whose first call succeeded.
"""

from __future__ import annotations

from datetime import date

from src.data.fallback_loader import FallbackDataLoader
from src.data.models import OHLCVRow, TickerMeta
from src.data.moex_loader import LoaderNotFoundError


def _meta(ticker: str) -> TickerMeta:
    return TickerMeta(
        ticker=ticker,
        figi="figi-" + ticker,
        name=ticker,
        class_code="SPBXM",
        currency="USD",
        lot=1,
    )


def _row(ticker: str) -> OHLCVRow:
    return OHLCVRow(
        ticker=ticker,
        ts=date(2024, 1, 1),
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        adj_close=1.5,
        volume=10,
    )


class _FakeSource:
    """Records calls and returns rows or raises on demand."""

    def __init__(self, name: str, ticker_outcomes: dict[str, object]) -> None:
        self.name = name
        self.outcomes = ticker_outcomes
        self.calls: list[str] = []

    def list_tickers(self):
        return [_meta(t) for t in self.outcomes]

    def get_ticker(self, ticker):
        return _meta(ticker)

    def iter_ohlcv(self, ticker, start, end):
        self.calls.append(ticker)
        outcome = self.outcomes[ticker]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return iter([])
        return iter(outcome)


def _build_loader(tinkoff: _FakeSource, moex: _FakeSource) -> FallbackDataLoader:
    return FallbackDataLoader(tinkoff_grpc=tinkoff, moex_iss=moex, order=("tinkoff_grpc", "moex_iss"))


class TestSourceSkipCache:
    """Cycle146 regression guard: per-source skip cache for unknown tickers."""

    def test_known_ticker_skips_fallback_chain_after_first_success(self) -> None:
        # First source has data, second is irrelevant
        tinkoff = _FakeSource(
            "tinkoff_grpc",
            {"AAPL": [_row("AAPL"), _row("AAPL")], "OTHER": [_row("OTHER")]},
        )
        moex = _FakeSource("moex_iss", {"AAPL": None, "OTHER": None})
        loader = _build_loader(tinkoff, moex)

        # First call: tinkoff returns rows, moex not consulted
        rows = list(loader.iter_ohlcv("AAPL", date(2024, 1, 1), date(2024, 1, 31)))
        assert len(rows) == 2
        assert tinkoff.calls == ["AAPL"]
        assert moex.calls == []  # not consulted — found on first source

        # Second call: same ticker — should still NOT call moex (cached as
        # 'satisfied by tinkoff')
        rows = list(loader.iter_ohlcv("AAPL", date(2024, 1, 1), date(2024, 1, 31)))
        assert len(rows) == 2
        assert tinkoff.calls == ["AAPL", "AAPL"]
        assert moex.calls == []  # still not consulted

    def test_loader_not_found_is_cached_and_skipped_on_next_call(self) -> None:
        tinkoff = _FakeSource(
            "tinkoff_grpc",
            {"TICK": LoaderNotFoundError("not found")},
        )
        moex = _FakeSource("moex_iss", {"TICK": None})
        loader = _build_loader(tinkoff, moex)

        # First call: tinkoff raises, moex returns 0
        rows = list(loader.iter_ohlcv("TICK", date(2024, 1, 1), date(2024, 1, 31)))
        assert rows == []
        assert tinkoff.calls == ["TICK"]
        assert moex.calls == ["TICK"]

        # Second call: BOTH sources should be skipped based on cached skip
        # entry from the first call.
        rows = list(loader.iter_ohlcv("TICK", date(2024, 1, 1), date(2024, 1, 31)))
        assert rows == []
        assert tinkoff.calls == ["TICK"]  # no second call
        assert moex.calls == ["TICK"]  # no second call

    def test_zero_bars_is_cached_and_skipped_on_next_call(self) -> None:
        # First source returns 0 rows, second also 0 — both should be
        # cached as "skip" so subsequent calls short-circuit.
        tinkoff = _FakeSource("tinkoff_grpc", {"X": None})
        moex = _FakeSource("moex_iss", {"X": None})
        loader = _build_loader(tinkoff, moex)

        rows = list(loader.iter_ohlcv("X", date(2024, 1, 1), date(2024, 1, 31)))
        assert rows == []
        assert tinkoff.calls == ["X"]
        assert moex.calls == ["X"]

        rows = list(loader.iter_ohlcv("X", date(2024, 1, 1), date(2024, 1, 31)))
        assert rows == []
        assert tinkoff.calls == ["X"]  # no retry
        assert moex.calls == ["X"]  # no retry

    def test_cache_is_per_ticker_not_global(self) -> None:
        # Cache for AAPL does not affect BARS for BOB.
        tinkoff = _FakeSource(
            "tinkoff_grpc",
            {"AAPL": [_row("AAPL")], "BOB": [_row("BOB")]},
        )
        moex = _FakeSource("moex_iss", {"AAPL": None, "BOB": None})
        loader = _build_loader(tinkoff, moex)

        # Hit AAPL first — only tinkoff called
        rows = list(loader.iter_ohlcv("AAPL", date(2024, 1, 1), date(2024, 1, 31)))
        assert len(rows) == 1
        assert tinkoff.calls == ["AAPL"]

        # BOB still gets its own fresh attempt
        rows = list(loader.iter_ohlcv("BOB", date(2024, 1, 1), date(2024, 1, 31)))
        assert len(rows) == 1
        assert tinkoff.calls == ["AAPL", "BOB"]
