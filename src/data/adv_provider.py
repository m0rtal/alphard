"""AdvProvider — Average Daily Volume source for OrderFlow (issue #230).

Pre-#230 ``OrderFlow.submit_market`` built the OrderSlicer's ``adv_shares``
as ``max(qty * 20, 100)`` — a hardcoded placeholder unrelated to real
ADV — which made the slicer's 5%-ADV-chunk policy collapse to exactly
one chunk for every realistic production quantity. The whole TWAP /
rate-limit / 5%-ADV participation policy shipped as dead code on the
OrderFlow path.

This module provides the canonical ADV source. The data agent (Phase 2.6
``ohlcv_daily`` reads) already exposes per-ticker daily volume; we wrap
that into a thin ``Callable[[str], Decimal]`` interface that
``OrderFlow`` consumes via its new ``adv_provider`` constructor argument.

Two flavours are provided:

* :class:`AdvProvider` — production class backed by any
  :class:`src.data.store.DataStore`. Reads the last ``lookback_days`` of
  ``ohlcv_daily`` bars for ``(ticker, source='tkf')`` and sums the
  ``volume`` column to compute a robust ADV in shares. Missing data
  (no bars in window) raises :class:`AdvProviderError` so ``OrderFlow``
  can surface ``ADV_UNAVAILABLE`` rather than fall back to a placeholder
  (issue #230 fail-safe contract, mirrors ``quote_provider`` from
  issue #166).
* :class:`CachingAdvProvider` — wraps any provider and memoises
  per-ticker results for ``ttl_seconds``. Caches the *negative* outcome
  (ticker not found) too so a flapping data feed doesn't hammer the
  store on every order.

Usage:

    store: DataStore = PostgresDataStore(dsn=os.environ["ALPHARD_PG_DSN"])
    adv = CachingAdvProvider(AdvProvider(store, lookback_days=20), ttl_seconds=300)
    OrderFlow(broker=..., risk_gate=..., quote_provider=..., adv_provider=adv)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, MutableMapping

from .store import DataStore, StoreError

logger = logging.getLogger("alphard.adv_provider")


class AdvProviderError(RuntimeError):
    """Raised when ADV cannot be computed for a ticker.

    ``OrderFlow`` maps this to ``decision_violations=("ADV_UNAVAILABLE",)``
    so the order is rejected up-front instead of falling back to a
    placeholder (issue #230 fail-safe contract).
    """


@dataclass(frozen=True)
class AdvProvider:
    """Production ADV source backed by ``ohlcv_daily`` rows.

    Reads the last ``lookback_days`` of bars for ``(ticker, source='tkf')``
    and returns ``sum(volume)`` as the ADV in shares. ``lookback_days``
    defaults to 20 (~1 trading month) which matches the same window the
    sizing formula uses for ATR (see ``src/broker/sizing.py``).

    The ``source='tkf'`` filter prevents the result from being polluted
    by ``source='moex'`` rows that have a different ``volume`` convention
    (MOEX returns turnover in RUB, not share count). Operators that need
    blended ADV across sources can pass ``source=None`` and accept the
    unit-mismatch risk.

    Raises :class:`AdvProviderError` on StoreError or empty-window — the
    caller MUST NOT swallow this and substitute a placeholder; that is
    exactly the bug class #230 was opened for.
    """

    store: DataStore
    lookback_days: int = 20
    source: str | None = "tkf"

    def __post_init__(self) -> None:  # noqa: D401 — dataclass hook
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be > 0")

    def __call__(self, ticker: str) -> Decimal:
        # Issue #234: normalise at the boundary so the input contract is
        # symmetric with ``CachingAdvProvider.__call__`` (which already
        # upper-cases the cache key) and with the row-uppercase behaviour
        # of ``PostgresDataStore.query_ohlcv`` / ``SQLiteStore.query_ohlcv``.
        # Today the stores re-normalise inside the query, so this is a
        # pure defense-in-depth fix — but it documents the contract
        # explicitly and protects against future wrappers that don't
        # (sister-bug class of issues #183/#185/#224). Sister-bug class
        # matters because if a future caller (multi-source loader, CSV
        # fallback, debug script) reaches ``query_ohlcv`` through a path
        # that DOESN'T normalise, ``AdvProvider`` would silently report
        # ``ADV_UNAVAILABLE`` for a ticker that does have rows.
        ticker = ticker.upper().strip()
        # ``ohlcv_daily`` PK is (ticker, ts); ``query_ohlcv`` is the
        # canonical read path (PostgresDataStore + SQLiteStore both
        # implement it).
        end = date.today()
        start = end - timedelta(days=self.lookback_days)
        try:
            rows = self.store.query_ohlcv(ticker, start, end, source=self.source)
        except StoreError as exc:
            raise AdvProviderError(f"ohlcv_daily query failed for {ticker}: {exc}") from exc

        if not rows:
            raise AdvProviderError(
                f"no ohlcv_daily rows for {ticker} in last {self.lookback_days} "
                f"days (window={start}..{end}, source={self.source!r})"
            )

        # ``OHLCVRow.volume`` is the contract — issue #225 fixed the
        # sizing formula to read this field (not ``high - low``); the
        # ADV provider uses the same source of truth.
        total = sum((Decimal(r.volume) for r in rows), Decimal("0"))
        if total <= 0:
            # Volume must be positive for a tradable instrument. An
            # order book of zero shares is not a tradable market — the
            # caller should treat this as "ADV unavailable" rather than
            # "ADV=0", since ``OrderSlicer`` would divide by zero or
            # saturate ``liq_scalar`` to ``MAX_LIQ_SCALAR`` (issue #225).
            raise AdvProviderError(f"ohlcv_daily volume sum is zero for {ticker} in last " f"{self.lookback_days} days")
        return total


@dataclass
class CachingAdvProvider:
    """Memoise ADV results to bound store load on hot tickers.

    Both positive (``Decimal``) and negative (``AdvProviderError``)
    outcomes are cached for ``ttl_seconds`` so a flapping data feed
    doesn't hammer the store on every order. The cache is guarded by a
    single ``threading.Lock`` — fine for the millisecond-scale
    ``query_ohlcv`` calls we wrap.

    Thread-safety: cache reads/writes are serialised under a single
    ``threading.Lock``. The underlying ``AdvProvider`` is read-only.
    """

    inner: Callable[[str], Decimal]
    ttl_seconds: float = 300.0
    _cache: MutableMapping[str, tuple[float, Decimal | None, str | None]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, ticker: str) -> Decimal:
        key = ticker.upper()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                ts, value, err = cached
                if (now - ts) < self.ttl_seconds:
                    if err is not None:
                        # Re-raise the cached failure. We keep the
                        # original message for diagnostics.
                        raise AdvProviderError(err)
                    assert value is not None  # invariant: (value, err) is XOR
                    return value
                # Stale — drop and recompute below.
                self._cache.pop(key, None)

        # Compute outside the lock so a slow store doesn't block other
        # callers. The double-check pattern is good enough — two
        # concurrent misses may both hit the store, but the cache key
        # is per-ticker so the duplicate work is bounded.
        try:
            value = self.inner(key)
        except AdvProviderError as exc:
            with self._lock:
                self._cache[key] = (now, None, str(exc))
            raise

        with self._lock:
            self._cache[key] = (now, value, None)
        return value

    def invalidate(self, ticker: str | None = None) -> None:
        """Drop cached entries. ``None`` clears the whole cache."""
        with self._lock:
            if ticker is None:
                self._cache.clear()
            else:
                self._cache.pop(ticker.upper(), None)


__all__ = [
    "AdvProvider",
    "AdvProviderError",
    "CachingAdvProvider",
]
