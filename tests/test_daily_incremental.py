"""Unit tests for scripts/daily_incremental.py (Issue #331, 2026-08-29).

The script's pure helpers (no DB, no HTTP) are testable without the
production stack. We cover:

    - _closed_bar_window: end-of-window is always yesterday, never
      today, and a present ``latest_db_ts`` shifts start to +1 day.
    - Defensive filter against today's bar in case the upstream
      returns a forming bar (covered via the upstream-bars list in
      main(); we do not retest the main loop here — it requires
      mocking broker gRPC + Postgres which is out of scope for
      these pure-Python tests).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# Make scripts/ importable for the ``from scripts import`` style
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_incremental  # noqa: E402


class TestClosedBarWindow:
    def test_returns_only_yesterday_when_no_prior_data(self) -> None:
        # Contract: even when latest_db_ts is None (defensive path), we
        # only fetch yesterday — never today, never future.
        end = daily_incremental.date.today() - timedelta(days=1)
        start, end_out = daily_incremental._closed_bar_window(None)
        assert start == end_out == end

    def test_uses_latest_plus_one_day(self) -> None:
        # When the DB has bars through 2026-08-28, today is 2026-08-29
        # and the next fetch should cover [2026-08-29, 2026-08-28].
        # end is fixed at yesterday (2026-08-28), so we expect
        # start > end (no new closed bar yet).
        latest = date(2026, 8, 28)
        with patch("daily_incremental.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 29)
            mock_date.side_effect = lambda *args, **kwargs: date(*args, **kwargs)
            start, end = daily_incremental._closed_bar_window(latest)
        assert end == date(2026, 8, 28)  # yesterday
        assert start == date(2026, 8, 29)  # latest + 1
        # start > end means "no new bar to fetch yet" — caller skips
        # the ticker. That is the correct behaviour for a same-day
        # refresh.
        assert start > end

    def test_window_of_one_day_when_db_is_one_day_behind(self) -> None:
        # latest = 2026-08-27, today = 2026-08-29 → fetch [08-28, 08-28]
        # which is a 1-day window.
        latest = date(2026, 8, 27)
        with patch("daily_incremental.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 29)
            mock_date.side_effect = lambda *args, **kwargs: date(*args, **kwargs)
            start, end = daily_incremental._closed_bar_window(latest)
        assert start == date(2026, 8, 28)
        assert end == date(2026, 8, 28)
        assert (end - start).days == 0  # 1-day window inclusive

    def test_window_handles_multi_day_gap(self) -> None:
        # If the daily_incremental has been off for a week, the next
        # run should pull the whole 6-day gap in one call. The script
        # does not cap this — the per-ticker safety cap
        # ``--max-bars-per-ticker`` is the operator's knob for that.
        latest = date(2026, 8, 22)
        with patch("daily_incremental.date") as mock_date:
            mock_date.today.return_value = date(2026, 8, 29)
            mock_date.side_effect = lambda *args, **kwargs: date(*args, **kwargs)
            start, end = daily_incremental._closed_bar_window(latest)
        assert start == date(2026, 8, 23)
        assert end == date(2026, 8, 28)
        assert (end - start).days == 5  # 6-day window inclusive

    def test_end_is_strictly_before_today(self) -> None:
        # The whole point of the "closed bars only" rule: we must
        # NEVER ask the broker for today's bar. The function
        # enforces this by clamping end = today - 1.
        for _ in range(5):
            with patch("daily_incremental.date") as mock_date:
                mock_date.today.return_value = date(2026, 8, 29)
                mock_date.side_effect = lambda *args, **kwargs: date(*args, **kwargs)
                _, end = daily_incremental._closed_bar_window(None)
            assert end < date(2026, 8, 29)  # strictly before today
            assert end == date(2026, 8, 28)


class TestFetchWithFallback:
    def test_uses_broker_first_then_moex_on_failure(self) -> None:
        # Stub the loaders so we don't hit any real Tinkoff/MOEX API.
        # The function should:
        #   1. try broker gRPC via FallbackDataLoader (which calls
        #      ``source.iter_ohlcv`` on tinkoff_grpc)
        #   2. on any exception, fall through to MOEX iter_ohlcv
        #   3. return whatever the second call produced
        class FakeBroker:
            def iter_ohlcv(self, ticker, start, end):
                raise RuntimeError("simulated broker failure")

        class FakeMoex:
            def iter_ohlcv(self, ticker, start, end):
                return [f"moex-bar-{ticker}-{start}-{end}"]

        with (
            patch("daily_incremental.TinkoffInvestDataLoader", return_value=FakeBroker()),
            patch("daily_incremental.MOEXDataLoader", return_value=FakeMoex()),
        ):
            result = daily_incremental._fetch_with_fallback("SBER", date(2026, 8, 28), date(2026, 8, 28))
        assert result == ["moex-bar-SBER-2026-08-28-2026-08-28"]

    def test_broker_success_skips_moex(self) -> None:
        # If the broker returns bars, the MOEX loader's iter_ohlcv is
        # NOT called.
        broker_calls = []

        class FakeBroker:
            def iter_ohlcv(self, ticker, start, end):
                broker_calls.append((ticker, start, end))
                return iter([f"broker-bar-{ticker}"])

        class FakeMoex:
            def iter_ohlcv(self, ticker, start, end):
                raise AssertionError("MOEX iter_ohlcv should not be called when broker succeeds")

        # ``_fetch_with_fallback`` now constructs both loaders eagerly
        # (via ``FallbackDataLoader``), so we allow construction but
        # must forbid the actual data call on the happy path. If the
        # test ever fails at ``FakeMoex.iter_ohlcv``, that means the
        # chain is not stopping on the first non-empty source — a bug
        # to fix.
        with (
            patch("daily_incremental.TinkoffInvestDataLoader", return_value=FakeBroker()),
            patch("daily_incremental.MOEXDataLoader", return_value=FakeMoex()),
        ):
            result = daily_incremental._fetch_with_fallback("GAZP", date(2026, 8, 28), date(2026, 8, 28))
        assert list(result) == ["broker-bar-GAZP"]
        assert broker_calls == [("GAZP", date(2026, 8, 28), date(2026, 8, 28))]


# ---------------------------------------------------------------------------
# Issue #350 (2026-08-30, m0rtal) — daily_incremental._fetch_with_fallback
# previously called ``MOEXDataLoader().iter_ohlcv(ticker, start, end)``
# directly with the full outer window when broker gRPC failed. MOEX
# enforces a 1825-day cap and any window longer than that raises
# ``LoaderError: range ... exceeds upstream max lookback 1825d``.
# For delisted tickers with stale ``latest_db_ts`` the window is
# often years long, so each daily-incremental run silently lost
# every such ticker's incremental update on the days broker
# happened to fail.
#
# Fix contract: _fetch_with_fallback must route through
# ``FallbackDataLoader.iter_ohlcv``, which inherits the
# lookback-aware chunking from PR #348 and applies it to moex_iss
# automatically.
# ---------------------------------------------------------------------------


def test_fetch_with_fallback_chunks_long_window_for_moex_cap() -> None:
    """Window > 1825d gets chunked at the MOEX fallback (issue #350).

    Regression: when broker gRPC raises, the fallback to MOEX must
    NOT pass the full window — it must chunk into <=1825d sub-ranges
    so the daily incremental refresh survives delisted tickers with
    stale ``latest_db_ts``. The chunking contract lives in the real
    ``FallbackDataLoader`` (PR #348); this test pins that the script
    actually delegates to it.
    """
    from src.data.moex_loader import MAX_LOOKBACK as MOEX_MAX_LOOKBACK

    class FakeBroker:
        def iter_ohlcv(self, ticker, start, end):
            raise RuntimeError("simulated broker failure")

        def iter_corporate_actions(self, ticker, start, end):
            return iter([])

        def list_tickers(self):
            return []

    class FakeMoex:
        def __init__(self):
            self.calls: list[tuple[str, date, date]] = []

        def iter_ohlcv(self, ticker, start, end):
            self.calls.append((ticker, start, end))
            return iter([f"moex-{ticker}-{start}-{end}"])

    moex = FakeMoex()
    cap_days = MOEX_MAX_LOOKBACK.days  # 1825
    # A 9-year window — same shape as the supervisor's backfill window
    # and large enough to definitely need chunking.
    start = date(2018, 1, 1)
    end = date(2026, 8, 28)
    assert (end - start).days > cap_days

    with (
        patch("daily_incremental.TinkoffInvestDataLoader", return_value=FakeBroker()),
        patch("daily_incremental.MOEXDataLoader", return_value=moex),
    ):
        result = daily_incremental._fetch_with_fallback("SBER", start, end)

    # Each chunk must fit under the cap.
    assert len(moex.calls) >= 2
    for _, c_start, c_end in moex.calls:
        assert (c_end - c_start).days <= cap_days, f"chunk {(c_start, c_end)} exceeds cap"
    # Chunks cover the whole window contiguously.
    assert moex.calls[0][1] == start
    assert moex.calls[-1][2] == end
    # Rows are concatenated from each chunk.
    assert len(result) == len(moex.calls)
    assert all(r.startswith("moex-SBER-") for r in result)


def test_fetch_with_fallback_uses_fallback_loader_chain() -> None:
    """Regression guard for issue #350 — _fetch_with_fallback must
    route through the FallbackDataLoader chain, not call the broker
    ``fetch_ohlcv`` directly.

    The script no longer inlines the chain (the inline copy was the
    bug — it bypassed PR #348's chunking). This test pins that the
    broker's ``fetch_ohlcv`` is NOT called; the chain is.
    """
    broker_fetch_calls: list[tuple[str, date, date]] = []
    fl_iter_calls: list[tuple[str, date, date]] = []

    class FakeBroker:
        def fetch_ohlcv(self, ticker, start, end):
            broker_fetch_calls.append((ticker, start, end))
            raise RuntimeError("simulated broker failure")

        def iter_ohlcv(self, ticker, start, end):
            raise RuntimeError("broker iter_ohlcv should be called via the chain, not directly")

        def list_tickers(self):
            return []

        def iter_corporate_actions(self, ticker, start, end):
            return iter([])

    class FakeMoex:
        def iter_ohlcv(self, ticker, start, end):
            return iter([f"moex-{ticker}"])

    # Patch FallbackDataLoader to a recorder that delegates to the
    # chain's contract (try broker, fall back to moex) so the test
    # asserts the wiring without re-implementing chunking.
    class FakeFl:
        def __init__(self, *, tinkoff_grpc, moex_iss):
            self.tinkoff_grpc = tinkoff_grpc
            self.moex_iss = moex_iss

        def iter_ohlcv(self, ticker, start, end):
            fl_iter_calls.append((ticker, start, end))
            try:
                list(self.tinkoff_grpc.iter_ohlcv(ticker, start, end))
            except Exception:
                pass
            yield from self.moex_iss.iter_ohlcv(ticker, start, end)

    with (
        patch("daily_incremental.FallbackDataLoader", FakeFl),
        patch("daily_incremental.TinkoffInvestDataLoader", return_value=FakeBroker()),
        patch("daily_incremental.MOEXDataLoader", return_value=FakeMoex()),
    ):
        result = daily_incremental._fetch_with_fallback("X", date(2026, 8, 28), date(2026, 8, 28))

    # The broker's direct fetch_ohlcv was never called — the chain
    # called iter_ohlcv on its behalf.
    assert broker_fetch_calls == []
    assert fl_iter_calls == [("X", date(2026, 8, 28), date(2026, 8, 28))]
    assert list(result) == ["moex-X"]
