"""Postgres-backed DataStore.

NOT WIRED IN TESTS
------------------
This module is ``import``-ed by the package but the tests do not
exercise it directly — Phase 1.1 CI runs on an environment without a
running Postgres. The contract is verified via ``InMemorySQLiteStore``
(see ``sqlite_store.py``).

To use locally:
    export ALPHARD_PG_DSN="host=localhost dbname=alphard user=alphard"
    psql -f src/data/schema.sql

PHASE 2 NOTES
-------------
- ``vector(384)`` column on ``news_embedding`` is reserved for pgvector.
  Phase 1.1 schema does NOT require pgvector — only Phase 3+.
- ON CONFLICT clauses use the column names from the index, not the PK
  name, so they survive PK renames in future migrations.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from .models import CorporateAction, OHLCVRow, TickerMeta
from .store import DataStore, StoreError

logger = logging.getLogger(__name__)


class PostgresDataStore(DataStore):
    """PostgreSQL implementation of the DataStore contract.

    Parameters
    ----------
    dsn:
        Standard libpq DSN. If omitted, ``$ALPHARD_PG_DSN`` is consulted.
    schema_sql_path:
        Path to the schema file. Defaults to ``schema.sql`` next to this
        module. Phase 2 will switch to a real migration framework.
    """

    def __init__(self, dsn: str | None = None, *, schema_sql_path: str | None = None) -> None:
        dsn = dsn or os.environ.get("ALPHARD_PG_DSN")
        if not dsn:
            raise StoreError("PostgresDataStore: no DSN — pass dsn= or set $ALPHARD_PG_DSN")
        self._dsn = dsn
        self._schema_sql_path = schema_sql_path or os.path.join(os.path.dirname(__file__), "schema.sql")
        # Imported lazily so the rest of the package works without psycopg.
        import psycopg

        self._psycopg = psycopg
        self._conn: Any = None  # lazy connect

    # ---------------------------------------------------------- connection

    def _connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg.connect(self._dsn, autocommit=False)

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def __enter__(self) -> "PostgresDataStore":
        self._connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------- schema

    def init_schema(self) -> None:
        self._connect()
        with open(self._schema_sql_path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        with self._conn.cursor() as cur:
            cur.execute(sql)
        self._conn.commit()

    # ---------------------------------------------------------- ticker

    def upsert_ticker(self, meta: TickerMeta) -> None:
        self.upsert_tickers([meta])

    def upsert_tickers(self, metas: list[TickerMeta]) -> None:
        if not metas:
            return
        self._connect()
        sql = """
            INSERT INTO ticker_universe
                (ticker, figi, name, lot, isin, currency, delisted,
                 delisted_at, listed_at, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker) DO UPDATE SET
                figi = EXCLUDED.figi,
                name = EXCLUDED.name,
                lot = EXCLUDED.lot,
                isin = EXCLUDED.isin,
                currency = EXCLUDED.currency,
                source = EXCLUDED.source,
                updated_at = NOW()
        """
        rows = [
            (
                m.ticker,
                m.figi,
                m.name,
                m.lot,
                m.isin,
                m.currency,
                m.delisted,
                m.delisted_at,
                m.listed_at,
                m.source,
            )
            for m in metas
        ]
        with self._conn.cursor() as cur:
            cur.executemany(sql, rows)
        self._conn.commit()

    def list_tickers(self, *, include_delisted: bool = True) -> list[TickerMeta]:
        self._connect()
        sql = (
            "SELECT ticker, figi, name, lot, isin, currency, delisted, "
            "delisted_at, listed_at, source FROM ticker_universe"
        )
        if not include_delisted:
            sql += " WHERE delisted = FALSE"
        sql += " ORDER BY ticker"
        with self._conn.cursor() as cur:
            cur.execute(sql)
            out = [_row_to_ticker(r) for r in cur.fetchall()]
        return out

    def mark_delisted(self, ticker: str, at: date, *, reason: str = "") -> None:
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE ticker_universe SET delisted = TRUE, delisted_at = %s, " "updated_at = NOW() WHERE ticker = %s",
                (at, ticker),
            )
            cur.execute(
                "INSERT INTO delisting_log (ticker, delisted_at, reason, source) " "VALUES (%s, %s, %s, 'manual')",
                (ticker, at, reason),
            )
        self._conn.commit()

    # ---------------------------------------------------------- OHLCV

    def upsert_ohlcv(self, rows: list[OHLCVRow]) -> int:
        if not rows:
            return 0
        self._connect()
        sql = """
            INSERT INTO ohlcv_daily
                (ticker, ts, open, high, low, close, volume, adj_close, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker, ts, source) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                adj_close = EXCLUDED.adj_close,
                updated_at = NOW()
        """
        params = [
            (
                r.ticker,
                r.ts,
                str(r.open),
                str(r.high),
                str(r.low),
                str(r.close),
                str(r.volume),
                str(r.adj_close),
                r.source,
            )
            for r in rows
        ]
        with self._conn.cursor() as cur:
            cur.executemany(sql, params)
        self._conn.commit()
        return len(rows)

    def query_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        source: str | None = None,
    ) -> list[OHLCVRow]:
        self._connect()
        sql = (
            "SELECT ticker, ts, open, high, low, close, volume, adj_close, source "
            "FROM ohlcv_daily WHERE ticker = %s AND ts BETWEEN %s AND %s"
        )
        params: list[Any] = [ticker.upper(), start, end]
        if source:
            sql += " AND source = %s"
            params.append(source)
        sql += " ORDER BY ts"
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_to_ohlcv(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- corp actions

    def upsert_corporate_actions(self, rows: list[CorporateAction]) -> int:
        if not rows:
            return 0
        self._connect()
        sql = """
            INSERT INTO corporate_actions
                (ticker, ts, kind, value, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker, ts, kind, source) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
        """
        params = [(r.ticker, r.ts, r.kind, str(r.value), r.source) for r in rows]
        with self._conn.cursor() as cur:
            cur.executemany(sql, params)
        self._conn.commit()
        return len(rows)

    def query_corporate_actions(self, ticker: str, start: date, end: date) -> list[CorporateAction]:
        self._connect()
        sql = (
            "SELECT ticker, ts, kind, value, source FROM corporate_actions "
            "WHERE ticker = %s AND ts BETWEEN %s AND %s ORDER BY ts"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (ticker.upper(), start, end))
            return [_row_to_action(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- diagnostics

    def count_ohlcv(self, ticker: str | None = None) -> int:
        self._connect()
        with self._conn.cursor() as cur:
            if ticker:
                cur.execute("SELECT COUNT(*) FROM ohlcv_daily WHERE ticker = %s", (ticker.upper(),))
            else:
                cur.execute("SELECT COUNT(*) FROM ohlcv_daily")
            return int(cur.fetchone()[0])


def _row_to_ticker(r: Any) -> TickerMeta:
    return TickerMeta(
        ticker=r[0],
        figi=r[1],
        name=r[2],
        lot=int(r[3]),
        isin=r[4],
        currency=r[5] or "RUB",
        delisted=bool(r[6]),
        delisted_at=r[7],
        listed_at=r[8],
        source=r[9],
    )


def _row_to_ohlcv(r: Any) -> OHLCVRow:
    from decimal import Decimal

    return OHLCVRow(
        ticker=r[0],
        ts=r[1],
        open=Decimal(str(r[2])),
        high=Decimal(str(r[3])),
        low=Decimal(str(r[4])),
        close=Decimal(str(r[5])),
        volume=Decimal(str(r[6])),
        adj_close=Decimal(str(r[7])),
        source=r[8],
    )


def _row_to_action(r: Any) -> CorporateAction:
    from decimal import Decimal

    return CorporateAction(
        ticker=r[0],
        ts=r[1],
        kind=r[2],
        value=Decimal(str(r[3])),
        source=r[4],
    )


__all__ = ["PostgresDataStore"]
