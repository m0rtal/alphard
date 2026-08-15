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

    def __init__(
        self,
        dsn: str | None = None,
        *,
        schema_sql_path: str | None = None,
        search_path: str | None = None,
    ) -> None:
        dsn = dsn or os.environ.get("ALPHARD_PG_DSN")
        if not dsn:
            raise StoreError("PostgresDataStore: no DSN — pass dsn= or set $ALPHARD_PG_DSN")
        self._dsn = dsn
        self._schema_sql_path = schema_sql_path or os.path.join(os.path.dirname(__file__), "schema.sql")  # noqa: E501
        # Optional: keep a custom search_path on every (re)connect.
        # Used by tests to isolate against an alphard_test schema.
        self._search_path = search_path
        # Imported lazily so the rest of the package works without psycopg.
        import psycopg

        self._psycopg = psycopg
        self._conn: Any = None  # lazy connect

    # ---------------------------------------------------------- connection

    def _connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg.connect(self._dsn, autocommit=True)
            if self._search_path:
                with self._conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {self._search_path}")

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
            "SELECT ticker, figi, name, lot, isin, currency, class_code, "
            "delisted, delisted_at, listed_at, source FROM ticker_universe"
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
                "UPDATE ticker_universe SET delisted = TRUE, delisted_at = %s, "
                "updated_at = NOW() WHERE ticker = %s",  # noqa: E501
                (at, ticker),
            )
            cur.execute(
                "INSERT INTO delisting_log (ticker, delisted_at, reason, source) "
                "VALUES (%s, %s, %s, 'manual')",  # noqa: E501
                (ticker, at, reason),
            )
        self._conn.commit()

    # ---------------------------------------------------------- OHLCV

    def upsert_ohlcv(self, rows: list[OHLCVRow]) -> int:
        """Upsert OHLCV bars. PK = (ticker, ts). Source flags updated via ON CONFLICT.

        Behaviour: writes each (ticker, ts) row. If the row exists, the
        existing OHLCV values are KEPT (preserves whichever source arrived
        first); only the covered_by_* flags are OR'd in to reflect all
        sources that have confirmed this bar.
        """
        if not rows:
            return 0
        self._connect()
        sql = """
            INSERT INTO ohlcv_daily
                (ticker, ts, open, high, low, close, volume, adj_close,
                 covered_by_tkf, covered_by_moex, primary_source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, NOW())
            ON CONFLICT (ticker, ts) DO UPDATE SET
                covered_by_tkf = ohlcv_daily.covered_by_tkf OR EXCLUDED.covered_by_tkf,
                covered_by_moex = ohlcv_daily.covered_by_moex OR EXCLUDED.covered_by_moex,
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
                r.covered_by_tkf,
                r.covered_by_moex,
                r.primary_source,
            )
            for r in rows
        ]
        with self._conn.cursor() as cur:
            cur.executemany(sql, params)
        self._conn.commit()
        return len(rows)

    def backfill_with_dedup(
        self,
        new_bars: list[OHLCVRow],
        source: str = "moex",
    ) -> dict[str, int]:
        """Insert bars but ONLY if (ticker, ts) is not yet covered by ANY source.

        Used by MOEX backfill script: skip dates already covered by Tinkoff
        or any other source. Updates covered_by_<source> flag on insert.
        Returns dict with stats: {'inserted': N, 'skipped': M}.
        """
        if not new_bars:
            return {"inserted": 0, "skipped": 0}

        pairs = list({(r.ticker, r.ts) for r in new_bars})
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ticker, ts FROM ohlcv_daily
                WHERE (ticker, ts) IN ({','.join(['(%s,%s)'] * len(pairs))})
                """,
                [v for pair in pairs for v in pair],
            )
            covered = {(row[0], row[1]) for row in cur.fetchall()}

        filtered = [r for r in new_bars if (r.ticker, r.ts) not in covered]
        skipped = len(new_bars) - len(filtered)
        if filtered:
            self.upsert_ohlcv(filtered)
        return {"inserted": len(filtered), "skipped": skipped}

    def migrate_deduplicate(self) -> int:
        """One-time migration: collapse duplicate (ticker, ts) rows.

        The current schema (Phase 1.1) has PK (ticker, ts), so no duplicates
        can exist. This function is a safety net for legacy states where
        the PK was dropped (e.g. partial migration from old versioned
        schema). It deletes duplicates keeping the row with the lowest
        ctid (effectively whichever row was inserted first).

        Note: this doesn't rewrite the covered_by_* flags because the
        current schema already covers source provenance via the
        primary_source column on insert.

        Returns count of rows deleted.
        """
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                WITH ranked AS (
                    SELECT ctid,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker, ts
                               ORDER BY ctid
                           ) AS rn
                    FROM ohlcv_daily
                ),
                to_delete AS (
                    SELECT ctid FROM ranked WHERE rn > 1
                )
                DELETE FROM ohlcv_daily
                WHERE ctid IN (SELECT ctid FROM to_delete)
            """
            )
            deleted = int(cur.rowcount)
        self._conn.commit()
        return deleted

    def query_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        primary_source: str | None = None,
    ) -> list[OHLCVRow]:
        self._connect()
        sql = (
            "SELECT ticker, ts, open, high, low, close, volume, adj_close, "
            "primary_source, covered_by_tkf, covered_by_moex "
            "FROM ohlcv_daily WHERE ticker = %s AND ts BETWEEN %s AND %s"
        )
        params: list[Any] = [ticker.upper(), start, end]
        if primary_source:
            sql += " AND primary_source = %s"
            params.append(primary_source)
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
        class_code=r[6] if len(r) > 10 else None,
        delisted=bool(r[7] if len(r) > 7 else False),
        delisted_at=r[8] if len(r) > 8 else None,
        listed_at=r[9] if len(r) > 9 else None,
        source=r[10] if len(r) > 10 else r[9],
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
        primary_source=r[8] if len(r) > 8 else "tkf",
        covered_by_tkf=bool(r[9]) if len(r) > 9 else False,
        covered_by_moex=bool(r[10]) if len(r) > 10 else False,
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
