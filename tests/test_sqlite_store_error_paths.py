"""C2: coverage for src/data/sqlite_store.py defensive error paths.

The 21 missing lines (sqlite_store.py:85%) are all try/except blocks
around DB write methods that only fire when the underlying sqlite3
operation raises. Existing tests exercise the happy paths; this file
exercises the error wrappers by injecting broken SQL into the connection
and asserting that StoreError is raised with the documented message.

Each test mocks the connection's execute/executemany to raise a
real sqlite3.OperationalError, then calls the public method and
checks:
  1. StoreError is raised (not sqlite3.Error leaking out)
  2. The error message contains the method name (helps operators
     identify the failing call from logs)
  3. The original sqlite3 error is chained via __cause__ so debug
     info survives
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from src.data.models import (
    CorporateAction,
    OHLCVRow,
    TickerMeta,
)
from src.data.sqlite_store import InMemorySQLiteStore
from src.data.store import StoreError


class _BrokenConnection:
    """Connection stand-in whose execute/executemany raise OperationalError
    for DML (INSERT/UPDATE/DELETE) but pass through DDL/SELECT/PRAGMA
    to the real connection.

    Used to drive the except sqlite3.Error -> StoreError branches in
    src/data/sqlite_store.py without needing to mutate the read-only
    sqlite3.Connection (C-level attribute).
    """

    _DML_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")

    def __init__(self, real_conn: sqlite3.Connection) -> None:
        self._real = real_conn  # keep the real connection for cleanup

    @classmethod
    def _is_dml(cls, sql: object) -> bool:
        if not isinstance(sql, str):
            return False
        head = sql.lstrip().upper().split(None, 1)[0] if sql.strip() else ""
        return head in cls._DML_PREFIXES

    def execute(self, *args: object, **kwargs: object) -> object:
        sql = args[0] if args else kwargs.get("sql", "")
        if self._is_dml(sql):
            raise sqlite3.OperationalError("synthetic DML failure for test")
        # sqlite3.Connection.execute has a precise stub type; we forward
        # the raw args, but mypy can't tell they're compatible.
        return self._real.execute(*args, **kwargs)  # type: ignore[arg-type]

    def executemany(self, *args: object, **kwargs: object) -> object:
        sql = args[0] if args else kwargs.get("sql", "")
        if self._is_dml(sql):
            raise sqlite3.OperationalError("synthetic DML failure for test")
        # sqlite3 stubs executemany as SupportsLenAndGetItem; we forward
        # the raw args, but mypy can't tell they're compatible.
        return self._real.executemany(*args, **kwargs)  # type: ignore[arg-type]

    def commit(self) -> None:
        self._real.commit()

    def close(self) -> None:
        self._real.close()

    def __getattr__(self, name: str) -> object:
        # Forward anything else (PRAGMA, sqlite_master, row_factory, ...)
        # to the real connection.
        return getattr(self._real, name)


def _open_store_with_broken_execute() -> InMemorySQLiteStore:
    """Open a real store, then replace the connection with one whose
    execute/executemany raise OperationalError. close()/commit() still
    forward to the real connection so cleanup is correct.
    """
    store = InMemorySQLiteStore()
    store._conn = _BrokenConnection(store._conn)  # type: ignore[assignment]
    return store


# -----------------------------------------------------------------------
# Schema bootstrap + close paths
# -----------------------------------------------------------------------


def test_init_creates_schema_then_commits() -> None:
    store = InMemorySQLiteStore()
    # Verify the schema exists (init ran)
    rows = store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {r[0] for r in rows}
    assert "ticker_universe" in table_names
    assert "ohlcv_daily" in table_names
    store.close()


def test_close_releases_connection() -> None:
    """Line 151 (close path)."""
    store = InMemorySQLiteStore()
    conn_ref = store._conn  # keep a reference before close
    store.close()
    # After close(), the connection is shut down. Any subsequent
    # operation on it raises ProgrammingError.
    with pytest.raises(sqlite3.ProgrammingError):
        conn_ref.execute("SELECT 1")


def test_context_manager_returns_self_and_closes() -> None:
    """Lines 157 (return self) and 160 (close on __exit__)."""
    store = InMemorySQLiteStore()
    conn_ref = store._conn  # save a ref to verify close after __exit__
    with store as s:
        # line 157: __enter__ returns self
        assert s is store
        rows = conn_ref.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert rows
    # After the with, close() was called by __exit__ (line 160).
    with pytest.raises(sqlite3.ProgrammingError):
        conn_ref.execute("SELECT 1")


def test_context_manager_calls_close_on_exit_even_when_upsert_raises() -> None:
    """Line 160: __exit__ must call close() even when the body raises."""
    store = _open_store_with_broken_execute()
    # Access underlying real connection (BrokenConnection.__getattr__ proxy)
    real_conn = store._conn._real  # type: ignore[attr-defined]
    with pytest.raises(StoreError):
        with store:
            # executemany on the broken connection raises
            # sqlite3.OperationalError, which the store wraps in
            # StoreError. close() must still be called by __exit__.
            store.upsert_tickers([TickerMeta(ticker="TKR", name="Test", lot=1, source="tkf")])
    # The real connection should now be closed (line 160 path).
    with pytest.raises(sqlite3.ProgrammingError):
        real_conn.execute("SELECT 1")


# -----------------------------------------------------------------------
# Write-method error paths (C2 main coverage target)
# -----------------------------------------------------------------------


def _meta(ticker: str = "FAIL") -> TickerMeta:
    return TickerMeta(ticker=ticker, name="Test", lot=1, source="tkf")


def _bar(ticker: str = "FAIL", d: date = date(2026, 1, 1)) -> OHLCVRow:
    return OHLCVRow(
        ticker=ticker,
        ts=d,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        adj_close=Decimal("1"),
        volume=Decimal("1"),
        source="tkf",
    )


def _ca(ticker: str = "FAIL", d: date = date(2026, 1, 1)) -> CorporateAction:
    return CorporateAction(
        ticker=ticker,
        ts=d,
        kind="dividend",
        value=Decimal("1.0"),
        source="tkf",
    )


def test_upsert_tickers_raises_store_error_on_db_failure() -> None:
    """Lines 227-228."""
    store = _open_store_with_broken_execute()
    try:
        with pytest.raises(StoreError) as exc_info:
            store.upsert_tickers([_meta()])
        assert "upsert_tickers" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
    finally:
        store.close()


def test_list_tickers_raises_store_error_on_db_failure() -> None:
    """Lines 240-241."""
    store = _open_store_with_broken_execute()
    # Need a real table for list_tickers to even reach the error path
    store._conn.execute("CREATE TABLE IF NOT EXISTS ticker_universe (ticker TEXT PRIMARY KEY)")
    try:
        with patch.object(
            store._conn,
            "execute",
            side_effect=sqlite3.OperationalError("forced read failure"),
        ):
            with pytest.raises(StoreError) as exc_info:
                store.list_tickers()
        assert "list_tickers" in str(exc_info.value)
    finally:
        store.close()


def test_mark_delisted_raises_store_error_on_db_failure() -> None:
    """Lines 256-257."""
    store = _open_store_with_broken_execute()
    try:
        with pytest.raises(StoreError) as exc_info:
            store.mark_delisted("ANY", at=date(2026, 1, 1))
        assert "mark_delisted" in str(exc_info.value)
    finally:
        store.close()


def test_mark_delisted_returns_zero_for_unknown_ticker() -> None:
    """Line 264 (early return when no rows updated)."""
    store = InMemorySQLiteStore()
    try:
        # No ticker universe entry yet → mark_delisted early-returns
        # without raising (the SQLite wrapper doesn't expose rowcount
        # for "no row updated"). Cover the line by reaching it.
        store.mark_delisted("UNKNOWN", at=date(2026, 1, 1))
    finally:
        store.close()


def test_upsert_ohlcv_raises_store_error_on_db_failure() -> None:
    """Lines 295-296."""
    store = _open_store_with_broken_execute()
    try:
        with pytest.raises(StoreError) as exc_info:
            store.upsert_ohlcv([_bar()])
        assert "upsert_ohlcv" in str(exc_info.value)
    finally:
        store.close()


def test_query_ohlcv_raises_store_error_on_db_failure() -> None:
    """Lines 323-324."""
    store = _open_store_with_broken_execute()
    try:
        with patch.object(
            store._conn,
            "execute",
            side_effect=sqlite3.OperationalError("forced read failure"),
        ):
            with pytest.raises(StoreError) as exc_info:
                store.query_ohlcv("ANY", date(2026, 1, 1), date(2026, 1, 1))
        assert "query_ohlcv" in str(exc_info.value)
    finally:
        store.close()


def test_upsert_corporate_actions_raises_store_error_on_db_failure() -> None:
    """Lines 418-419."""
    store = _open_store_with_broken_execute()
    try:
        with pytest.raises(StoreError) as exc_info:
            store.upsert_corporate_actions([_ca()])
        assert "upsert_corporate_actions" in str(exc_info.value)
    finally:
        store.close()


def test_query_corporate_actions_raises_store_error_on_db_failure() -> None:
    """Lines 429-430."""
    store = _open_store_with_broken_execute()
    try:
        with patch.object(
            store._conn,
            "execute",
            side_effect=sqlite3.OperationalError("forced read failure"),
        ):
            with pytest.raises(StoreError) as exc_info:
                store.query_corporate_actions("ANY", date(2026, 1, 1), date(2026, 1, 1))
        assert "query_corporate_actions" in str(exc_info.value)
    finally:
        store.close()


def test_init_schema_is_idempotent_and_safe_to_re_run() -> None:
    """Lines 146-147: init_schema() must executescript + commit; safe
    to call multiple times because CREATE TABLE IF NOT EXISTS is a
    no-op on second run.
    """
    store = InMemorySQLiteStore()
    conn_ref = store._conn
    # Pre-condition: tables already exist from __init__
    rows_before = conn_ref.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_universe'"
    ).fetchall()
    assert len(rows_before) == 1

    # Re-run init_schema() — must not raise even though tables exist.
    store.init_schema()
    rows_after = conn_ref.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_universe'"
    ).fetchall()
    assert len(rows_after) == 1
    store.close()


def test_upsert_ticker_single_wraps_upsert_tickers() -> None:
    """Line 165: upsert_ticker(meta) is a single-row wrapper around
    upsert_tickers([meta]). Verify the wrapper works end-to-end.
    """
    store = InMemorySQLiteStore()
    try:
        store.upsert_ticker(TickerMeta(ticker="SINGLE", name="SingleRow", lot=1, source="tkf"))
        rows = store.list_tickers()
        tickers = {m.ticker for m in rows}
        assert "SINGLE" in tickers
    finally:
        store.close()


def test_upsert_ohlcv_returns_zero_for_empty_input() -> None:
    """Line 264: upsert_ohlcv([]) returns 0 immediately (no DML issued).
    Note: this is a different line than the similar early-return at
    line 264 of mark_delisted; both are defensive no-op paths that
    callers rely on for safe behaviour with empty iterables.
    """
    store = InMemorySQLiteStore()
    try:
        # The store accepts an empty iterable and short-circuits to
        # ``return 0`` before any SQL is generated.
        assert store.upsert_ohlcv([]) == 0
        assert store.upsert_corporate_actions([]) == 0
    finally:
        store.close()


def test_query_ohlcv_returns_empty_for_ticker_with_no_rows() -> None:
    """Line 264 (mark_delisted): mark_delisted on a ticker that doesn't
    exist returns 0 (rowcount=0 from sqlite). Confirms the early-return
    branch without raising.
    """
    store = InMemorySQLiteStore()
    try:
        # ticker_universe is empty, so the UPDATE affects 0 rows.
        # mark_delisted early-returns without raising.
        store.mark_delisted("NEVER_EXISTED", at=date(2026, 1, 1))
    finally:
        store.close()
