"""Tests for ``src/data/adv_provider.py`` (issue #230 regression suite).

The ``AdvProvider`` class is the canonical ADV source for
``OrderFlow.adv_provider``. It reads the last ``lookback_days`` of
``ohlcv_daily`` bars for ``(ticker, source='tkf')`` and returns
``sum(volume)``. ``CachingAdvProvider`` wraps any provider with a TTL
cache and memoises both positive and negative outcomes.

These tests use a ``MagicMock`` for ``DataStore`` — we are not testing
the database, only the wrapper logic. Integration with the live
``PostgresDataStore`` / ``SQLiteStore`` is exercised by their own
contract tests.
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.data.adv_provider import (
    AdvProvider,
    AdvProviderError,
    CachingAdvProvider,
)
from src.data.models import OHLCVRow
from src.data.store import StoreError

# ────────────────────────────────────────────
# AdvProvider
# ────────────────────────────────────────────


def _bar(ticker: str, days_ago: int, volume: int) -> OHLCVRow:
    """Build a minimal ``OHLCVRow`` for the given ticker/date/volume.

    The non-volume fields are placeholder — ``AdvProvider`` only reads
    ``row.volume`` per the contract documented in issue #225.
    """
    return OHLCVRow(
        ticker=ticker,
        ts=date.today() - timedelta(days=days_ago),
        source="tkf",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal(volume),
        adj_close=Decimal("100"),
    )


class TestAdvProvider:
    def test_sums_volume_across_lookback_window(self) -> None:
        store = MagicMock()
        store.query_ohlcv.return_value = [
            _bar("SBER", 1, 1000),
            _bar("SBER", 2, 2000),
            _bar("SBER", 3, 3000),
        ]
        adv = AdvProvider(store, lookback_days=20)
        result = adv("SBER")
        assert result == Decimal("6000")
        store.query_ohlcv.assert_called_once()
        call_args = store.query_ohlcv.call_args
        # Symbol is upper-cased (mirrors query_ohlcv's contract).
        assert call_args.args[0] == "SBER"
        # Default source filter is 'tkf' (MOEX volume in RUB units would
        # pollute the sum).
        assert call_args.kwargs.get("source") == "tkf" or (len(call_args.args) >= 4 and call_args.args[3] == "tkf")

    def test_empty_window_raises_adv_provider_error(self) -> None:
        store = MagicMock()
        store.query_ohlcv.return_value = []
        adv = AdvProvider(store, lookback_days=20)
        with pytest.raises(AdvProviderError) as exc:
            adv("SBER")
        assert "no ohlcv_daily rows" in str(exc.value)
        assert "SBER" in str(exc.value)

    def test_store_error_wrapped_as_adv_provider_error(self) -> None:
        store = MagicMock()
        store.query_ohlcv.side_effect = StoreError("conn refused")
        adv = AdvProvider(store, lookback_days=20)
        with pytest.raises(AdvProviderError) as exc:
            adv("SBER")
        assert "ohlcv_daily query failed" in str(exc.value)

    def test_zero_total_volume_raises_adv_provider_error(self) -> None:
        """All-zero volume is not a tradable ADV — must reject.

        ``OrderSlicer`` would otherwise divide by zero or saturate
        ``liq_scalar`` to ``MAX_LIQ_SCALAR`` (see issue #225). Better to
        surface the gap to ``OrderFlow`` which maps it to
        ``ADV_INVALID``.
        """
        store = MagicMock()
        store.query_ohlcv.return_value = [
            _bar("SBER", 1, 0),
            _bar("SBER", 2, 0),
        ]
        adv = AdvProvider(store, lookback_days=20)
        with pytest.raises(AdvProviderError) as exc:
            adv("SBER")
        assert "volume sum is zero" in str(exc.value)

    def test_lookback_days_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            AdvProvider(MagicMock(), lookback_days=0)

    def test_lookback_window_passed_to_store(self) -> None:
        """Window must span ``lookback_days`` ending today."""
        store = MagicMock()
        store.query_ohlcv.return_value = [_bar("SBER", 1, 1000)]
        adv = AdvProvider(store, lookback_days=20)
        adv("SBER")
        args, kwargs = store.query_ohlcv.call_args
        # ``query_ohlcv(ticker, start, end, source)``.
        start = args[1]
        end = args[2]
        assert end == date.today()
        assert start == date.today() - timedelta(days=20)

    def test_lookup_window_matches_lookback_days(self) -> None:
        store = MagicMock()
        store.query_ohlcv.return_value = [_bar("SBER", 1, 5000)]
        adv = AdvProvider(store, lookback_days=5)
        adv("SBER")
        start = store.query_ohlcv.call_args.args[1]
        assert start == date.today() - timedelta(days=5)

    def test_source_filter_passed_through(self) -> None:
        store = MagicMock()
        store.query_ohlcv.return_value = [_bar("SBER", 1, 1000)]
        adv = AdvProvider(store, lookback_days=20, source=None)
        adv("SBER")
        # When source=None, the call passes None through.
        call = store.query_ohlcv.call_args
        # Either keyword or positional 4th arg.
        if "source" in call.kwargs:
            assert call.kwargs["source"] is None
        else:
            assert call.args[3] is None

    def test_lowercase_input_is_normalised_at_boundary(self) -> None:
        """Issue #234: defense-in-depth — normalise ticker before query_ohlcv.

        ``CachingAdvProvider`` already upper-cases the cache key, but
        ``AdvProvider.__call__`` passed the raw ticker straight through.
        Today the stores re-normalise inside ``query_ohlcv`` (issue #185
        sister-bug class), so the bug is latent — but documenting the
        contract at the boundary protects against future wrappers that
        don't re-normalise (multi-source loader, CSV fallback, debug
        script). This test pins the contract.
        """
        store = MagicMock()
        store.query_ohlcv.return_value = [_bar("SBER", 1, 1000)]
        adv = AdvProvider(store, lookback_days=20)
        adv("sber")
        # The first positional arg to query_ohlcv must be the upper-cased ticker.
        ticker_passed = store.query_ohlcv.call_args.args[0]
        assert ticker_passed == "SBER"

    def test_strip_whitespace_at_boundary(self) -> None:
        """Issue #234: ``.strip()`` mirrors the pattern used by
        ``MOEXDataLoader.iter_ohlcv`` and other data loaders — external
        callers (CLI args, ad-hoc scripts) sometimes pass
        ``"  SBER  "`` with surrounding whitespace.
        """
        store = MagicMock()
        store.query_ohlcv.return_value = [_bar("SBER", 1, 1000)]
        adv = AdvProvider(store, lookback_days=20)
        adv("  SBER  ")
        ticker_passed = store.query_ohlcv.call_args.args[0]
        assert ticker_passed == "SBER"

    def test_adv_unavailable_message_uses_normalised_ticker(self) -> None:
        """Issue #234: the error message must reflect the normalised ticker,
        not the raw input — operators debugging ``ADV_UNAVAILABLE`` should
        see the same ticker the store query used.
        """
        store = MagicMock()
        store.query_ohlcv.return_value = []  # no rows
        adv = AdvProvider(store, lookback_days=20)
        with pytest.raises(AdvProviderError) as exc:
            adv("sber")
        assert "no ohlcv_daily rows for SBER" in str(exc.value)


# ────────────────────────────────────────────
# CachingAdvProvider
# ────────────────────────────────────────────


class TestCachingAdvProvider:
    def test_positive_outcome_is_memoised(self) -> None:
        calls = [0]

        def inner(_symbol: str) -> Decimal:
            calls[0] += 1
            return Decimal("5000")

        cache = CachingAdvProvider(inner, ttl_seconds=60)
        assert cache("SBER") == Decimal("5000")
        assert cache("SBER") == Decimal("5000")
        assert cache("SBER") == Decimal("5000")
        assert calls[0] == 1, f"expected 1 call after 3 invocations, got {calls[0]}"

    def test_symbol_is_normalised_before_cache_key(self) -> None:
        calls = [0]

        def inner(symbol: str) -> Decimal:
            calls[0] += 1
            return Decimal("1000")

        cache = CachingAdvProvider(inner, ttl_seconds=60)
        # Different cases must collapse to the same cache entry.
        assert cache("sber") == Decimal("1000")
        assert cache("Sber") == Decimal("1000")
        assert cache("SBER") == Decimal("1000")
        assert calls[0] == 1

    def test_negative_outcome_is_also_memoised(self) -> None:
        """A missing ticker must not hammer the store on every order."""
        calls = [0]

        def inner(_symbol: str) -> Decimal:
            calls[0] += 1
            raise AdvProviderError("no rows for XYZ")

        cache = CachingAdvProvider(inner, ttl_seconds=60)
        for _ in range(5):
            with pytest.raises(AdvProviderError):
                cache("XYZ")
        assert calls[0] == 1, f"expected 1 call after 5 invocations, got {calls[0]}"

    def test_ttl_expiry_recomputes(self) -> None:
        calls = [0]

        def inner(_symbol: str) -> Decimal:
            calls[0] += 1
            return Decimal("1234")

        cache = CachingAdvProvider(inner, ttl_seconds=0.05)
        cache("SBER")
        cache("SBER")
        assert calls[0] == 1
        time.sleep(0.1)
        cache("SBER")
        assert calls[0] == 2, f"expected recompute after TTL, got {calls[0]}"

    def test_invalidate_specific_ticker(self) -> None:
        calls = [0]

        def inner(_symbol: str) -> Decimal:
            calls[0] += 1
            return Decimal("1")

        cache = CachingAdvProvider(inner, ttl_seconds=60)
        cache("SBER")
        cache("SBER")
        assert calls[0] == 1
        cache.invalidate("SBER")
        cache("SBER")
        assert calls[0] == 2

    def test_invalidate_clears_all(self) -> None:
        calls = [0]

        def inner(_symbol: str) -> Decimal:
            calls[0] += 1
            return Decimal("1")

        cache = CachingAdvProvider(inner, ttl_seconds=60)
        cache("SBER")
        cache("GAZP")
        cache.invalidate()
        cache("SBER")
        cache("GAZP")
        assert calls[0] == 4

    def test_thread_safety_under_concurrent_misses(self) -> None:
        """Many threads hit a cold cache simultaneously; ``inner`` must
        be called at least once but never raise.

        Without the lock, two threads could race on cache write and the
        positive-outcome cache could store ``None, None`` instead of
        ``(ts, value, None)`` — the assertion below would fail.
        """
        barrier = threading.Barrier(8)
        calls = [0]
        lock = threading.Lock()

        def inner(_symbol: str) -> Decimal:
            with lock:
                calls[0] += 1
            # Simulate a small store round-trip so threads race.
            time.sleep(0.01)
            return Decimal("5000")

        cache = CachingAdvProvider(inner, ttl_seconds=60)
        results: list[Decimal] = []
        errors: list[Exception] = []

        def worker() -> None:
            barrier.wait()
            try:
                results.append(cache("SBER"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert all(r == Decimal("5000") for r in results)
        # The double-check pattern means we expect a small number of
        # inner calls (1 to N), but NEVER zero.
        assert calls[0] >= 1
        # And bounded — at most the number of threads (8).
        assert calls[0] <= 8
