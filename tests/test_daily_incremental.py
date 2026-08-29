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

import pytest

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
        #   1. try broker gRPC via fetch_ohlcv
        #   2. on any exception, fall through to MOEX iter_ohlcv
        #   3. return whatever the second call produced
        class FakeBroker:
            def fetch_ohlcv(self, ticker, start, end):
                raise RuntimeError("simulated broker failure")

        class FakeMoex:
            def iter_ohlcv(self, ticker, start, end):
                return [f"moex-bar-{ticker}-{start}-{end}"]

        with patch("daily_incremental.TinkoffInvestDataLoader", return_value=FakeBroker()), \
             patch("daily_incremental.MOEXDataLoader", return_value=FakeMoex()):
            result = daily_incremental._fetch_with_fallback(
                "SBER", date(2026, 8, 28), date(2026, 8, 28)
            )
        assert result == ["moex-bar-SBER-2026-08-28-2026-08-28"]

    def test_broker_success_skips_moex(self) -> None:
        # If the broker returns bars, the MOEX loader is NOT touched.
        broker_calls = []

        class FakeBroker:
            def fetch_ohlcv(self, ticker, start, end):
                broker_calls.append((ticker, start, end))
                return [f"broker-bar-{ticker}"]

        class FakeMoex:
            def __init__(self):
                raise AssertionError("MOEX loader should not be called when broker succeeds")

        # NB: we wrap FakeMoex in a lambda so the eager ``FakeMoex()``
        # construction only happens if MOEXDataLoader() is actually
        # called at runtime — which it must NOT be when broker
        # succeeds. If the test ever fails at FakeMoex.__init__,
        # that means our _fetch_with_fallback is constructing the MOEX
        # loader even on the happy path, which means we have a bug
        # to fix.
        with patch("daily_incremental.TinkoffInvestDataLoader", return_value=FakeBroker()), \
             patch("daily_incremental.MOEXDataLoader", side_effect=lambda *a, **kw: FakeMoex()):
            result = daily_incremental._fetch_with_fallback(
                "GAZP", date(2026, 8, 28), date(2026, 8, 28)
            )
        assert result == ["broker-bar-GAZP"]
        assert broker_calls == [("GAZP", date(2026, 8, 28), date(2026, 8, 28))]
