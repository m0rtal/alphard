"""DataStore ABC — Phase 1.1 persistence interface.

Design decisions
----------------
1. ABC, not concrete: we have one production impl (``PostgresDataStore``
   in ``pg_store.py``) and one test/dev impl (``InMemorySQLiteStore`` in
   ``sqlite_store.py``). Contract tests in ``tests/test_data_loader.py``
   parametrize over both via ``pytest.fixture`` indirection.
2. No raw SQL in the ABC. Methods take / return pydantic models so the
   schema lives in ``schema.sql`` (one place to edit), and ``ON CONFLICT``
   upserts are implemented identically in both backends.
3. ``pgvector-ready`` means we leave a ``vector(384)`` column on the
   ``news_embedding`` table that Phase 3 will populate. Phase 1.1 does
   NOT use vectors — pgvector is optional even when installed.

Survivorship contract
---------------------
``upsert_ohlcv`` MUST NOT delete rows for tickers that disappear from
the universe later. The delisting event goes into ``delisting_log`` and
the ticker stays in ``ticker_universe`` with ``delisted=True``. Backtests
that ignore delistings will overstate performance; we accept that risk
here (Phase 4 will add a survivorship filter helper).
"""

from __future__ import annotations

import abc
from datetime import date

from .models import CorporateAction, OHLCVRow, TickerMeta


class StoreError(Exception):
    """Base for all DataStore failures (connection, schema, integrity)."""


class DataStore(abc.ABC):
    """Abstract persistence layer for OHLCV / corporate actions / metadata.

    Implementations must be safe to call from multiple threads within a
    single process. Cross-process safety is not a Phase 1.1 concern
    (the bot is single-process); Phase 3 will revisit with WAL + a
    process-level lock.
    """

    # ---- schema ---------------------------------------------------------

    @abc.abstractmethod
    def init_schema(self) -> None:
        """Create tables / extensions if missing. Idempotent."""

    # ---- ticker universe ------------------------------------------------

    @abc.abstractmethod
    def upsert_ticker(self, meta: TickerMeta) -> None:
        """Insert-or-update a single ticker. No-op if unchanged."""

    @abc.abstractmethod
    def upsert_tickers(self, metas: list[TickerMeta]) -> None:
        """Batch upsert. Faster than N round-trips for large universes."""

    @abc.abstractmethod
    def list_tickers(self, *, include_delisted: bool = True) -> list[TickerMeta]:
        """Return the ticker universe. ``include_delisted=False`` filters."""

    @abc.abstractmethod
    def mark_delisted(self, ticker: str, at: date, *, reason: str = "") -> None:
        """Flag a ticker as delisted and append to ``delisting_log``."""

    # ---- OHLCV ----------------------------------------------------------

    @abc.abstractmethod
    def upsert_ohlcv(self, rows: list[OHLCVRow]) -> int:
        """Insert-or-update OHLCV bars. Returns the count of rows touched.

        Conflict resolution: ``(ticker, ts)`` is the primary key; if the
        row already exists we OVERWRITE columns but PRESERVE ``source``
        if the new source is the same; if the new source differs we
        keep BOTH rows tagged with their source (future multi-source
        reconciliation).
        """

    @abc.abstractmethod
    def query_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[OHLCVRow]:
        """Read OHLCV bars for ``ticker`` in ``[start, end]``."""

    # ---- corporate actions ----------------------------------------------

    @abc.abstractmethod
    def upsert_corporate_actions(self, rows: list[CorporateAction]) -> int:
        """Insert-or-update corporate actions."""

    @abc.abstractmethod
    def query_corporate_actions(self, ticker: str, start: date, end: date) -> list[CorporateAction]:
        """Read corporate actions for ``ticker`` in ``[start, end]``."""

    # ---- diagnostics ----------------------------------------------------

    @abc.abstractmethod
    def count_ohlcv(self, ticker: str | None = None) -> int:
        """Total OHLCV rows; if ``ticker`` is set, only for that ticker."""


__all__ = ["DataStore", "StoreError"]
