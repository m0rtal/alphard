"""SQLite-backed DataStore — implements the same contract as PostgresDataStore.

Used by the test suite and by Phase 0 dev runs. We intentionally use
sqlite3 stdlib (NOT sqlalchemy) to honour the Phase 1.1 dependency
budget: stdlib + requests + pydantic.

Differences vs PostgresDataStore
--------------------------------
- NUMERIC columns become TEXT (sqlite has no native decimal). The store
  re-parses on read. Tests assert on Decimal, not str.
- ON CONFLICT clauses use SQLite's ``ON CONFLICT(...) DO UPDATE`` form.
- Vector column on ``news_embedding`` is omitted (sqlite has no vector
  type — Phase 3 swaps it in for pgvector-only).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from .models import CorporateAction, OHLCVRow, TickerMeta
from .store import DataStore, StoreError


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticker_universe (
    ticker         TEXT PRIMARY KEY,
    figi           TEXT,
    name           TEXT NOT NULL,
    lot            INTEGER NOT NULL CHECK (lot > 0),
    isin           TEXT,
    currency       TEXT NOT NULL DEFAULT 'RUB',
    delisted       INTEGER NOT NULL DEFAULT 0,
    delisted_at    TEXT,
    listed_at      TEXT,
    source         TEXT NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ticker_universe_delisted
    ON ticker_universe (delisted);

CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ticker     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    open       TEXT NOT NULL,
    high       TEXT NOT NULL,
    low        TEXT NOT NULL,
    close      TEXT NOT NULL,
    volume     TEXT NOT NULL,
    adj_close  TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, ts),
    FOREIGN KEY (ticker) REFERENCES ticker_universe(ticker)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_daily_ticker_ts
    ON ohlcv_daily (ticker, ts);

CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, ts, kind, source)
);

CREATE TABLE IF NOT EXISTS delisting_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    delisted_at TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'manual'
);
"""


class InMemorySQLiteStore(DataStore):
    """In-memory SQLite implementing the DataStore contract.

    Parameters
    ----------
    shared_connection:
        Pass an existing ``sqlite3.Connection`` (e.g. from ``pytest
        fixture``) to share state across multiple stores in the same
        test. Defaults to a private connection.
    """

    def __init__(self, shared_connection: sqlite3.Connection | None = None) -> None:
        self._owns_conn = shared_connection is None
        # Initialised here and never re-bound to None — close() resets the
        # attribute but raises if used after close, so mypy is happy and
        # callers get an AttributeError if they reach for a stale conn.
        conn: sqlite3.Connection = shared_connection or sqlite3.connect(":memory:")
        self._conn: sqlite3.Connection = conn
        # FK enforcement is off by default in sqlite; turn it on.
        self._conn.execute("PRAGMA foreign_keys = ON")
        # We synthesise the schema once per construction. ``IF NOT
        # EXISTS`` makes it idempotent.
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def init_schema(self) -> None:
        # Schema is created in __init__ for SQLite (in-memory). Re-running
        # ``CREATE TABLE IF NOT EXISTS`` is a no-op, but we still call it
        # so the contract is satisfied identically for both backends.
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()
        # Mark as closed by deleting the attribute; subsequent calls will
        # raise AttributeError, which is the correct signal to the caller.
        del self._conn

    def __enter__(self) -> "InMemorySQLiteStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------- ticker

    def upsert_ticker(self, meta: TickerMeta) -> None:
        self.upsert_tickers([meta])

    def upsert_tickers(self, metas: Iterable[TickerMeta]) -> None:
        rows = list(metas)
        if not rows:
            return
        params = [
            (
                m.ticker,
                m.figi,
                m.name,
                m.lot,
                m.isin,
                m.currency,
                int(m.delisted),
                m.delisted_at.isoformat() if m.delisted_at else None,
                m.listed_at.isoformat() if m.listed_at else None,
                m.source,
            )
            for m in rows
        ]
        sql = """
            INSERT INTO ticker_universe
                (ticker, figi, name, lot, isin, currency, delisted,
                 delisted_at, listed_at, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (ticker) DO UPDATE SET
                figi = excluded.figi,
                name = excluded.name,
                lot = excluded.lot,
                isin = excluded.isin,
                currency = excluded.currency,
                source = excluded.source,
                updated_at = datetime('now')
        """
        try:
            self._conn.executemany(sql, params)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"upsert_tickers failed: {exc}") from exc

    def list_tickers(self, *, include_delisted: bool = True) -> list[TickerMeta]:
        sql = (
            "SELECT ticker, figi, name, lot, isin, currency, delisted, "
            "delisted_at, listed_at, source FROM ticker_universe"
        )
        if not include_delisted:
            sql += " WHERE delisted = 0"
        sql += " ORDER BY ticker"
        try:
            cur = self._conn.execute(sql)
        except sqlite3.Error as exc:
            raise StoreError(f"list_tickers failed: {exc}") from exc
        return [_row_to_ticker(r) for r in cur.fetchall()]

    def mark_delisted(self, ticker: str, at: date, *, reason: str = "") -> None:
        try:
            self._conn.execute(
                "UPDATE ticker_universe SET delisted = 1, delisted_at = ?, "
                "updated_at = datetime('now') WHERE ticker = ?",
                (at.isoformat(), ticker.upper()),
            )
            self._conn.execute(
                "INSERT INTO delisting_log (ticker, delisted_at, reason, source) VALUES (?, ?, ?, 'manual')",  # noqa: E501
                (ticker.upper(), at.isoformat(), reason),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"mark_delisted failed: {exc}") from exc

    # ---------------------------------------------------------- OHLCV

    def upsert_ohlcv(self, rows: Iterable[OHLCVRow]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        params = [
            (
                r.ticker,
                r.ts.isoformat(),
                str(r.open),
                str(r.high),
                str(r.low),
                str(r.close),
                str(r.volume),
                str(r.adj_close),
            )
            for r in rows
        ]
        sql = """
            INSERT INTO ohlcv_daily
                (ticker, ts, open, high, low, close, volume, adj_close, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (ticker, ts) DO UPDATE SET
                updated_at = datetime('now')
        """
        try:
            self._conn.executemany(sql, params)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"upsert_ohlcv failed: {exc}") from exc
        return len(rows)

    def query_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[OHLCVRow]:
        sql = (
            "SELECT ticker, ts, open, high, low, close, volume, adj_close "
            "FROM ohlcv_daily WHERE ticker = ? AND ts BETWEEN ? AND ?"
        )
        params: list[Any] = [ticker.upper(), start.isoformat(), end.isoformat()]
        sql += " ORDER BY ts"
        try:
            cur = self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise StoreError(f"query_ohlcv failed: {exc}") from exc
        return [_row_to_ohlcv(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- corp actions

    def upsert_corporate_actions(self, rows: Iterable[CorporateAction]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        params = [(r.ticker, r.ts.isoformat(), r.kind, str(r.value), r.source) for r in rows]
        sql = """
            INSERT INTO corporate_actions
                (ticker, ts, kind, value, source, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (ticker, ts, kind, source) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
        """
        try:
            self._conn.executemany(sql, params)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"upsert_corporate_actions failed: {exc}") from exc
        return len(rows)

    def query_corporate_actions(self, ticker: str, start: date, end: date) -> list[CorporateAction]:
        sql = (
            "SELECT ticker, ts, kind, value, source FROM corporate_actions "
            "WHERE ticker = ? AND ts BETWEEN ? AND ? ORDER BY ts"
        )
        try:
            cur = self._conn.execute(sql, (ticker.upper(), start.isoformat(), end.isoformat()))
        except sqlite3.Error as exc:
            raise StoreError(f"query_corporate_actions failed: {exc}") from exc
        return [_row_to_action(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- diagnostics

    def count_ohlcv(self, ticker: str | None = None) -> int:
        if ticker:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM ohlcv_daily WHERE ticker = ?",
                (ticker.upper(),),
            )
        else:
            cur = self._conn.execute("SELECT COUNT(*) FROM ohlcv_daily")
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
        delisted_at=date.fromisoformat(r[7]) if r[7] else None,
        listed_at=date.fromisoformat(r[8]) if r[8] else None,
        source=r[9],
    )


def _row_to_ohlcv(r: Any) -> OHLCVRow:
    return OHLCVRow(
        ticker=r[0],
        ts=date.fromisoformat(r[1]),
        open=Decimal(str(r[2])),
        high=Decimal(str(r[3])),
        low=Decimal(str(r[4])),
        close=Decimal(str(r[5])),
        volume=Decimal(str(r[6])),
        adj_close=Decimal(str(r[7])),
    )


def _row_to_action(r: Any) -> CorporateAction:
    return CorporateAction(
        ticker=r[0],
        ts=date.fromisoformat(r[1]),
        kind=r[2],
        value=Decimal(str(r[3])),
        source=r[4],
    )


__all__ = ["InMemorySQLiteStore", "SCHEMA_SQL"]
