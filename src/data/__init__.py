"""Alphard Data Agent — Phase 1.1.

PURPOSE
-------
Fetch OHLCV (1d) + corporate-action history for MOEX / Tinkoff tickers,
persist to Postgres, and serve downstream agents (Quant, Macro, Risk).

DESIGN DECISIONS
----------------
1. Abstract ``DataLoader`` ABC + concrete ``TinkoffDataLoader`` /
   ``MOEXDataLoader`` — swap providers without rewriting the orchestrator.
2. Pure stdlib + ``requests`` + ``pydantic``. NO pandas/numpy/sqlalchemy —
   Phase 2 will introduce an ORM and vectorized frame ops.
3. ``DataStore`` is its own ABC with a Postgres-backed implementation that
   is pgvector-ready (no extension used yet — Phase 2/3 will enable it).
   For tests / local dev an ``InMemorySQLiteStore`` (sqlite3 stdlib)
   implements the same contract — keeps CI hermetic.
4. ``TokenBucket`` rate limiter is shared by both loaders (Tinkoff 60 rps,
   MOEX ISS 100 rpm) so back-pressure is uniform across providers.
5. OHLCV rows are *split-adjusted* via corporate actions. We store both
   raw OHLCV and ``adj_close`` so backtests can pick either.
6. Survivorship-aware: delisted tickers are NOT filtered from cold-start.
   They are returned with a ``delisted=True`` flag and logged in
   ``delisting_log`` table for forensic audit.
7. OHLCV columns: open, high, low, close, volume, adj_close. Numeric
   precision NUMERIC(18,8) — fits MOEX lot prices (3-4 decimals) and
   typical equity prices (~10⁶) with 8 fractional digits.

WHAT IS NOT HERE (intentional gaps, deferred to later phases)
-------------------------------------------------------------
- 5m / 1m bars and tick data (Phase 3).
- AlgoPack derived metrics (Phase 2).
- pgvector / embeddings for news similarity (Phase 3).
- Async / aiohttp (the loaders are synchronous — sufficient for EOD).
- Caching layer (Phase 1.2 introduces a daily snapshot cache).
"""

from __future__ import annotations

from .loader import (
    DataLoader,
    LoaderAuthError,
    LoaderError,
    LoaderNotFoundError,
    LoaderRateLimitError,
)
from .moex_loader import MOEXDataLoader
from .models import CorporateAction, OHLCVRow, TickerMeta
from .store import DataStore, StoreError
from .tinkoff_loader import TinkoffInvestDataLoader as TinkoffDataLoader
from .token_bucket import RateLimitError, TokenBucket

__all__ = [
    "CorporateAction",
    "DataLoader",
    "DataStore",
    "InMemorySQLiteStore",
    "LoaderAuthError",
    "LoaderError",
    "LoaderNotFoundError",
    "LoaderRateLimitError",
    "MOEXDataLoader",
    "OHLCVRow",
    "PostgresDataStore",
    "RateLimitError",
    "StoreError",
    "TickerMeta",
    "TinkoffDataLoader",
    "TokenBucket",
]

__version__ = "0.1.0"


def __getattr__(name: str) -> object:
    """Lazy-import optional backends so psycopg absence doesn't break imports."""
    if name == "PostgresDataStore":
        from .pg_store import PostgresDataStore

        return PostgresDataStore
    if name == "InMemorySQLiteStore":
        from .sqlite_store import InMemorySQLiteStore

        return InMemorySQLiteStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
