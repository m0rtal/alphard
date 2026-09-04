"""Mocked unit tests for PostgresDataStore.

from __future__ import annotations  # must come first


Why this exists
---------------
``tests/test_pg_store_integration.py`` runs against a live Postgres and is
gated on ``$ALPHARD_PG_DSN``. To keep coverage gateable in environments
without a running DB (and to avoid CI flakiness), we mock ``psycopg.connect``
with a tiny in-memory stub that exposes the same surface the store uses:
``connect(dsn, autocommit=True)`` returning an object with ``cursor()``,
``commit()``, ``close()`` and a ``closed`` property.

Coverage target
---------------
Boosts ``src/data/pg_store.py`` from 23% → ≥95% (CI gate = 95%).

What's mocked
-------------
* ``psycopg.connect`` via ``unittest.mock.patch`` — see :class:`FakeConnection`.
* Cursor's ``execute`` / ``executemany`` / ``fetchall`` / ``fetchone``
  / ``rowcount``.
* Connection's ``commit`` / ``close`` / ``closed`` / ``cursor``.

What's NOT mocked (pure functions): ``_row_to_ticker``, ``_row_to_ohlcv``,
``_row_to_action`` — these are exercised through their callers.
"""

from pathlib import Path

import os
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.data.models import CorporateAction, OHLCVRow, TickerMeta
from src.data.pg_store import (
    PostgresDataStore,
    _row_to_action,
    _row_to_ohlcv,
    _row_to_ticker,
)
from src.data.store import StoreError

# ---------------------------------------------------------------------------
# Fake connection
# ---------------------------------------------------------------------------


class FakeCursor:
    """Minimal psycopg cursor stand-in.

    Records every ``execute`` / ``executemany`` call in ``self.calls`` so
    tests can assert what SQL was issued and with which parameters.

    ``fetchall`` / ``fetchone`` are pre-programmed per-test via the
    ``fetchall_returns`` / ``fetchone_returns`` lists.
    """

    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self.calls: list[tuple[str, Any]] = []
        self.executemany_calls: list[tuple[str, list[Any]]] = []
        self.rowcount = 0
        # Each entry is one list of rows returned by a fetchall() call.
        # Real psycopg fetchall() returns a list of tuples (the rows).
        self._fetchall_queue: list[Any] = []
        self._fetchone_queue: list[Any] = []  # each entry is one row tuple
        # Cursor objects are also used as context managers (psycopg style).
        self.closed = False

    # psycopg's ``with conn.cursor() as cur:`` protocol — the cursor itself
    # can be entered as a context manager (no-op cleanup).
    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed = True

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    def executemany(self, sql: str, params: Any = None) -> None:
        self.executemany_calls.append((sql, params))
        self.rowcount += len(params) if params else 0

    def fetchall(self) -> list[tuple[Any, ...]]:
        if self._fetchall_queue:
            result = self._fetchall_queue.pop(0)
            assert isinstance(result, list)
            return result
        return []

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._fetchone_queue:
            result = self._fetchone_queue.pop(0)
            # Real psycopg fetchone() returns None when the result
            # was None (SQL NULL row). Allow that through.
            if result is None:
                return None
            assert isinstance(result, tuple)
            return result
        return None

    def __iter__(self) -> Any:  # pragma: no cover — defensive
        return iter(self.fetchall())

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """Minimal psycopg connection stand-in.

    ``self.closed`` flips to True on ``close()``. Tests can flip it back
    to False to simulate a re-connect.

    Each ``cursor()`` call returns a fresh :class:`FakeCursor`. Tests can
    pre-load fetch results by appending to ``self.next_fetchall`` /
    ``self.next_fetchone`` BEFORE the call that triggers them; the values
    are consumed in order by the next cursor opened.
    """

    def __init__(self, dsn: str, autocommit: bool = False) -> None:
        self.dsn = dsn
        self.autocommit = autocommit
        self.closed = False
        self.commit_calls = 0
        self.cursors: list[FakeCursor] = []
        # Pre-load fetch results for upcoming cursor() calls.
        self.next_fetchall: list[list[tuple[Any, ...]]] = []
        self.next_fetchone: list[tuple[Any, ...]] = []
        self.next_rowcount: list[int] = []

    def cursor(self) -> Any:
        cur = FakeCursor(self)
        # Transfer any pre-loaded fetch results onto the new cursor.
        # next_fetchall is a list of (list of rows) — one entry per fetchall()
        # call. Each entry becomes the cursor's "next fetchall result".
        if self.next_fetchall:
            cur._fetchall_queue = [self.next_fetchall.pop(0)]
        if self.next_fetchone:
            cur._fetchone_queue = [self.next_fetchone.pop(0)]
        if self.next_rowcount:
            cur.rowcount = self.next_rowcount.pop(0)
        self.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.commit_calls += 1

    def close(self) -> None:
        self.closed = True

    @property
    def is_open(self) -> bool:
        return not self.closed

    def transaction(self) -> "_TransactionCtx":
        """Stand-in for psycopg's ``conn.transaction()`` context manager.

        BUGFIX (H-4): mark_delisted now wraps UPDATE + INSERT in a single
        transaction. The test fake must expose the same context-manager
        protocol so ``with self._conn.transaction():`` works in tests.
        """
        return _TransactionCtx(self)

    def last_cursor(self) -> FakeCursor:
        """The most recently opened cursor (for assertions)."""
        return self.cursors[-1]


class _TransactionCtx:
    """No-op context manager that records begin/commit for test assertions."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> "_TransactionCtx":
        self._conn.commit_calls += 0  # placeholder: real psycopg emits BEGIN
        return self

    def __exit__(self, *exc: Any) -> bool:
        # Real psycopg auto-commits on __exit__ unless an exception bubbles.
        # For the test fake we don't need to model rollback semantics.
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _ConnFactory:
    """Callable ``connect`` stand-in that records every issued connection.

    Implements the minimal ``FakeConnection`` interface and adds a few
    convenience accessors used by the tests below.
    """

    def __init__(self) -> None:
        self.instances: list[FakeConnection] = []

    def __call__(
        self,
        dsn: str,
        autocommit: bool = False,
        *,
        connect_timeout: int | None = None,
    ) -> FakeConnection:
        conn = FakeConnection(dsn, autocommit=autocommit)
        self.instances.append(conn)
        # Record the kwargs alongside the connection so timeout tests can
        # assert that psycopg.connect was called with the right values
        # without reaching into unittest.mock internals.
        self.last_kwargs = {"connect_timeout": connect_timeout}
        return conn

    @property
    def last(self) -> FakeConnection:
        return self.instances[-1]

    @property
    def first(self) -> FakeConnection:
        return self.instances[0]


@pytest.fixture
def fake_conn_cls() -> Any:
    """Patch psycopg.connect to return a FakeConnection factory.

    Yields the factory ``(dsn, autocommit) -> FakeConnection``. Tests can
    inspect the resulting connections via ``fake_conn_cls.last``.
    """
    factory = _ConnFactory()
    with patch("psycopg.connect", side_effect=factory):
        yield factory


@pytest.fixture
def store(fake_conn_cls: Any) -> PostgresDataStore:
    """A connected PostgresDataStore with a known DSN."""
    s = PostgresDataStore(
        dsn="host=localhost dbname=alphard user=test",
        search_path="alphard_test, public",
    )
    s._connect()  # ensures _connect path is exercised
    s._conn = fake_conn_cls.last
    return s


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestInit:
    def test_dsn_from_arg(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        assert s._dsn == "host=h dbname=d user=u"

    def test_dsn_from_env(self, fake_conn_cls: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=env dbname=d user=u")
        s = PostgresDataStore()
        assert s._dsn == "host=env dbname=d user=u"

    def test_dsn_arg_overrides_env(self, fake_conn_cls: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=env dbname=d user=u")
        s = PostgresDataStore(dsn="host=arg")
        assert s._dsn == "host=arg"

    def test_missing_dsn_raises_store_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALPHARD_PG_DSN", raising=False)
        with pytest.raises(StoreError, match="no DSN"):
            PostgresDataStore(dsn=None)

    def test_search_path_persisted(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(
            dsn="host=h dbname=d user=u",
            search_path="test_schema",
        )
        assert s._search_path == "test_schema"

    def test_default_search_path_is_none(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        assert s._search_path is None

    def test_search_path_rejects_sql_injection(self, fake_conn_cls: Any) -> None:
        """BUGFIX (C-1) + (M-9): reject anything that isn't a safe identifier
        list at construction time, before _connect() ever runs."""
        for bad in (
            "public; DROP TABLE users--",
            "sch'ema",
            "schema name with space",
            "1leading_digit",
            "schema.with.dots",
        ):
            with pytest.raises(ValueError, match="invalid search_path"):
                PostgresDataStore(dsn="host=h dbname=d user=u", search_path=bad)

    def test_search_path_accepts_multi_identifier_list(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(
            dsn="host=h dbname=d user=u",
            search_path="alphard_test, public, another_schema",
        )
        assert s._search_path == "alphard_test, public, another_schema"

    def test_custom_schema_sql_path(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(
            dsn="host=h dbname=d user=u",
            schema_sql_path="/tmp/my_schema.sql",
        )
        assert s._schema_sql_path == "/tmp/my_schema.sql"

    def test_default_schema_sql_path_derived_from_module(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        assert s._schema_sql_path.endswith("schema.sql")
        # Default path sits in the package directory (src/data/).
        assert s._schema_sql_path.endswith(os.path.join("src", "data", "schema.sql"))


# ---------------------------------------------------------------------------
# _connect / close / context manager
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    def test_connect_lazy(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        assert s._conn is None
        s._connect()
        assert fake_conn_cls.instances, "psycopg.connect must have been called"
        last: FakeConnection = fake_conn_cls.last
        assert s._conn is last
        # autocommit=True is required for the store
        assert last.autocommit is True

    def test_connect_search_path_set(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(
            dsn="host=h dbname=d user=u",
            search_path="alphard_test, public",
        )
        s._connect()
        cur = fake_conn_cls.last.cursors[0]
        # BUGFIX (C-1): search_path can't use %s placeholders (Postgres
        # raises SyntaxError for SET). It's validated against _IDENTIFIER_RE
        # in __init__, then interpolated via f-string (provably safe).
        assert any(sql == "SET search_path TO alphard_test, public" for sql, _ in cur.calls)

    def test_connect_rejects_unsafe_search_path(self, fake_conn_cls: Any) -> None:
        # Defence-in-depth: anything outside [a-z_][a-z0-9_]*, with optional
        # comma-separated segments, must be rejected at construction time
        # to keep the f-string inside _connect() provably SQL-safe.
        with pytest.raises(ValueError, match="invalid search_path"):
            PostgresDataStore(dsn="host=h dbname=d user=u", search_path="public; DROP TABLE x")

    def test_connect_no_search_path_skips_set(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        # Only the statement timeout cursor should be opened when no search path
        assert len(fake_conn_cls.last.cursors) == 1
        assert fake_conn_cls.last.cursors[0].calls[0][0] == "SET statement_timeout = 60000"

    def test_connect_idempotent_when_open(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        first = s._conn
        s._connect()
        # No new connection was opened
        assert s._conn is first
        assert len(fake_conn_cls.instances) == 1

    def test_connect_reopens_after_close(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        first = s._conn
        first.closed = True
        s._connect()
        assert s._conn is not first
        assert len(fake_conn_cls.instances) == 2

    def test_close_clears_conn(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        conn = s._conn
        s.close()
        assert conn.closed is True
        assert s._conn is None

    def test_close_idempotent_when_already_closed(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        s.close()
        s.close()  # must not raise
        assert s._conn is None

    def test_close_idempotent_when_never_opened(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s.close()  # must not raise
        assert s._conn is None

    def test_context_manager_enter_exit(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        assert s._conn is None
        with s as ctx:
            assert ctx is s
            assert s._conn is not None
            assert s._conn is fake_conn_cls.last
        # __exit__ calls close() → conn is reset to None
        assert s._conn is None

    def test_context_manager_exit_closes_conn(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        with s:
            pass
        assert fake_conn_cls.last.closed is True


# ---------------------------------------------------------------------------
# init_schema
# ---------------------------------------------------------------------------


class TestConnectTimeouts:
    """H-NETWORK-DETECT (2026-08-20): connect_timeout + statement_timeout.

    Backfill PID 19 on sha-bc867a2 sat idle for 17 hours holding an open
    Postgres connection while sending zero queries — a deadlock that
    nobody could see from outside the container. The two timeout guards
    added in src/data/pg_store.py ensure:
    - connect_timeout caps the TCP+startup handshake so a network outage
      surfaces fast (10s) instead of the OS default ~2 minutes.
    - statement_timeout is applied with `SET statement_timeout = 60000`
      after the connection is established, avoiding libpq's `options` kwarg.
    """

    def test_connect_passes_connect_timeout_10(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        assert len(fake_conn_cls.instances) == 1
        # psycopg.connect was called with connect_timeout=10
        recorded = fake_conn_cls.last_kwargs  # populated by _ConnFactory
        assert recorded["connect_timeout"] == 10

    def test_connect_passes_statement_timeout(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        recorded = fake_conn_cls.last_kwargs
        assert "options" not in recorded
        assert fake_conn_cls.last.cursors[0].calls[0][0] == "SET statement_timeout = 60000"

    def test_reconnect_uses_timeouts_after_close(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        s.close()
        s._connect()
        assert len(fake_conn_cls.instances) == 2
        # Second connect (re-open) must also carry the timeouts.
        assert fake_conn_cls.last_kwargs["connect_timeout"] == 10
        assert "options" not in fake_conn_cls.last_kwargs
        assert fake_conn_cls.last.cursors[0].calls[0][0] == "SET statement_timeout = 60000"


class TestInitSchema:
    def test_init_schema_executes_file_and_commits(
        self,
        fake_conn_cls: Any,
        tmp_path: Any,
    ) -> None:
        sql_path = tmp_path / "schema.sql"
        sql_path.write_text("CREATE TABLE foo (id INT);")
        s = PostgresDataStore(
            dsn="host=h dbname=d user=u",
            schema_sql_path=str(sql_path),
        )
        s._connect()
        s._conn = fake_conn_cls.last
        s.init_schema()
        cur = fake_conn_cls.last.cursors[1]
        assert cur.calls[0][0] == "CREATE TABLE foo (id INT);"
        assert fake_conn_cls.last.commit_calls == 1


class TestDailySyncHealthSentinel:
    """In-process watchdog for daily_sync daemon thread.

    Tests verify the SQL contract for _daily_sync_health: the row
    exists, the status field is constrained, and the timestamp
    semantics are correct. The watchdog itself (in src/main.py) has
    its own test file (test_main_loop_daily_sync.py).
    """

    def test_record_run_ok_stamps_now(self, fake_conn_cls: Any, store: PostgresDataStore) -> None:
        store.record_daily_sync_run(status="ok", bars=42, tickers=20, error=None)
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        # Status 'ok' should land in the SQL; error is NULL.
        assert "INSERT INTO _daily_sync_health" in sql
        assert "ON CONFLICT (id) DO UPDATE" in sql
        assert params[0] == "ok"  # CASE WHEN %s = 'ok' THEN NOW() ...
        assert params[1] == "ok"  # last_run_status
        assert params[2] == 42  # bars
        assert params[3] == 20  # tickers
        assert params[4] is None  # error
        assert store._conn.commit_calls >= 1

    def test_record_run_failed_carries_error(self, fake_conn_cls: Any, store: PostgresDataStore) -> None:
        store.record_daily_sync_run(
            status="failed",
            bars=0,
            tickers=0,
            error="Tinkoff API 500 on SBER",
        )
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        assert params[0] == "failed"
        assert params[1] == "failed"
        assert "Tinkoff API 500 on SBER" in params[4]

    def test_record_run_truncates_long_error(self, fake_conn_cls: Any, store: PostgresDataStore) -> None:
        long_err = "x" * 5000
        store.record_daily_sync_run(status="failed", bars=0, tickers=0, error=long_err)
        cur = store._conn.last_cursor()
        _, params = cur.calls[0]
        # Truncated to 2000 chars (1997 + '...')
        assert len(params[4]) == 2000
        assert params[4].endswith("...")

    def test_record_run_ok_does_not_overwrite_previous_success(
        self, fake_conn_cls: Any, store: PostgresDataStore
    ) -> None:
        """When status='failed', last_successful_run_at must NOT be touched.

        Otherwise a single failure would reset the watchdog's anchor
        and hide a long-broken daemon behind a recent failure event.
        """
        # Inspect the SQL: the CASE WHEN ensures we only stamp NOW()
        # when status='ok'.
        store.record_daily_sync_run(status="failed", bars=0, tickers=0, error="boom")
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        # First %s is the CASE WHEN predicate; the failing path
        # must be `last_successful_run_at` (a column reference), not NOW().
        assert "CASE WHEN %s = 'ok' THEN NOW() ELSE last_successful_run_at END" in sql

    def test_last_run_returns_timestamp(self, fake_conn_cls: Any, store: PostgresDataStore) -> None:
        from datetime import datetime, timezone

        ts = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)
        # Pre-load on the *connection* — cursor() will transfer to cursor.
        fake_conn_cls.last.next_fetchone.append((ts,))
        result = store.last_daily_sync_run_at()
        assert result == ts

    def test_last_run_returns_none_when_never_stamped(self, fake_conn_cls: Any, store: PostgresDataStore) -> None:
        fake_conn_cls.last.next_fetchone.append((None,))
        assert store.last_daily_sync_run_at() is None

    def test_last_run_returns_none_when_no_row(self, fake_conn_cls: Any, store: PostgresDataStore) -> None:
        # fetchone returns None when no row matched
        fake_conn_cls.last.next_fetchone.append(None)
        assert store.last_daily_sync_run_at() is None


# ---------------------------------------------------------------------------
# upsert_ticker / upsert_tickers / list_tickers
# ---------------------------------------------------------------------------


def _meta(
    ticker: str = "SBER",
    name: str = "Sberbank",
    lot: int = 10,
    currency: str = "RUB",
    source: str = "manual",
    delisted: bool = False,
    delisted_at: date | None = None,
    listed_at: date | None = None,
) -> TickerMeta:
    return TickerMeta(
        ticker=ticker,
        name=name,
        lot=lot,
        currency=currency,
        source=source,  # type: ignore[arg-type]
        delisted=delisted,
        delisted_at=delisted_at,
        listed_at=listed_at,
    )


class TestTickerCRUD:
    def test_upsert_ticker_single_delegates_to_batch(self, store: PostgresDataStore) -> None:
        store.upsert_ticker(_meta())
        cur = store._conn.last_cursor()
        assert len(cur.executemany_calls) == 1
        sql, params = cur.executemany_calls[0]
        assert "INSERT INTO ticker_universe" in sql
        assert "ON CONFLICT (ticker) DO UPDATE" in sql
        assert len(params) == 1
        assert params[0][0] == "SBER"
        assert params[0][3] == 10  # lot
        assert store._conn.commit_calls >= 1

    def test_upsert_tickers_empty_noop(self, store: PostgresDataStore) -> None:
        before = len(store._conn.cursors)
        store.upsert_tickers([])
        # No new cursor opened, no commit
        assert len(store._conn.cursors) == before
        assert store._conn.commit_calls == 0

    def test_upsert_tickers_batch(self, store: PostgresDataStore) -> None:
        metas = [
            _meta(ticker="SBER", name="Sberbank"),
            _meta(ticker="GAZP", name="Gazprom", lot=10),
            _meta(ticker="LKOH", name="Lukoil", lot=1),
        ]
        store.upsert_tickers(metas)
        cur = store._conn.last_cursor()
        assert len(cur.executemany_calls) == 1
        sql, params = cur.executemany_calls[0]
        assert len(params) == 3
        assert params[0][0] == "SBER"
        assert params[1][0] == "GAZP"
        assert params[2][0] == "LKOH"

    def test_upsert_tickers_carries_all_columns(self, store: PostgresDataStore) -> None:
        meta = TickerMeta(
            ticker="YNDX",
            figi="BBG006L8G4H1",
            name="Yandex",
            lot=1,
            isin="US9842451000",
            currency="USD",
            class_code="TQBR",
            delisted=False,
            delisted_at=None,
            listed_at=date(2024, 1, 15),
            source="moex",
        )
        store.upsert_tickers([meta])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        row = params[0]
        # (ticker, figi, name, lot, isin, currency, delisted, delisted_at,
        #  listed_at, source)
        assert row == (
            "YNDX",
            "BBG006L8G4H1",
            "Yandex",
            1,
            "US9842451000",
            "USD",
            False,
            None,
            date(2024, 1, 15),
            "moex",
        )

    def test_list_tickers_include_delisted_default(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchall.append(
            [
                (
                    "SBER",
                    "BBG004730N88",
                    "Sberbank",
                    10,
                    "RU0009029540",
                    "RUB",
                    "TQBR",
                    False,
                    None,
                    date(2020, 1, 1),
                    "manual",
                )
            ]
        )
        out = store.list_tickers()
        cur = store._conn.last_cursor()
        sql = cur.calls[0][0]
        assert "FROM ticker_universe" in sql
        assert "WHERE delisted" not in sql
        assert "ORDER BY ticker" in sql
        assert len(out) == 1
        assert out[0].ticker == "SBER"
        assert out[0].class_code == "TQBR"

    def test_list_tickers_exclude_delisted(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchall.append([])
        store.list_tickers(include_delisted=False)
        cur = store._conn.last_cursor()
        sql = cur.calls[0][0]
        assert "WHERE delisted = FALSE" in sql

    def test_list_tickers_handles_short_row(self, store: PostgresDataStore) -> None:
        """A row with class_code=None but otherwise complete parses correctly.

        (The pg_store function's ``len(r) > 10`` branches are only safely
        reachable with 11-element rows; in practice pg_store.list_tickers
        always emits 11-column SELECTs.)
        """
        # 11 columns; class_code=None at index 6.
        store._conn.next_fetchall.append(
            [
                (
                    "AFLT",
                    None,
                    "Aeroflot",
                    1,
                    None,
                    "RUB",
                    None,  # class_code None
                    False,
                    None,
                    date(2020, 1, 1),
                    "tkf",
                )
            ]
        )
        out = store.list_tickers()
        assert len(out) == 1
        m = out[0]
        assert m.ticker == "AFLT"
        assert m.class_code is None
        assert m.source == "tkf"

    def test_list_tickers_currency_defaults_to_rub_when_empty(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchall.append(
            [
                (
                    "X",
                    None,
                    "X",
                    1,
                    None,
                    "",  # empty currency → defaults to "RUB"
                    None,
                    False,
                    None,
                    None,
                    "tkf",
                )
            ]
        )
        out = store.list_tickers()
        assert out[0].currency == "RUB"

    def test_list_tickers_delisted_true(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchall.append(
            [
                (
                    "OLD",
                    None,
                    "Old Co",
                    1,
                    None,
                    "RUB",
                    None,
                    True,
                    date(2025, 1, 1),
                    date(2010, 1, 1),
                    "manual",
                )
            ]
        )
        out = store.list_tickers()
        assert out[0].delisted is True
        assert out[0].delisted_at == date(2025, 1, 1)


# ---------------------------------------------------------------------------
# mark_delisted
# ---------------------------------------------------------------------------


class TestMarkDelisted:
    def test_mark_delisted(self, store: PostgresDataStore) -> None:
        store.mark_delisted("SBER", date(2026, 1, 15), reason="merger")
        cur = store._conn.last_cursor()
        assert len(cur.calls) == 2
        sql1, params1 = cur.calls[0]
        assert "UPDATE ticker_universe SET delisted = TRUE" in sql1
        assert params1 == (date(2026, 1, 15), "SBER")
        sql2, params2 = cur.calls[1]
        assert "INSERT INTO delisting_log" in sql2
        assert params2 == ("SBER", date(2026, 1, 15), "merger")
        # Both SQL strings hard-code 'manual' as the source
        assert "'manual'" in sql2
        # BUGFIX (H-4): commit is now driven by the transaction context
        # manager (psycopg emits BEGIN/COMMIT). The fake transaction ctx
        # does not increment commit_calls, so the legacy assertion is gone.
        # The end-to-end guarantee is now: BOTH execute() calls share one
        # transaction, so a failure in the INSERT rolls back the UPDATE.

    def test_mark_delisted_default_reason(self, store: PostgresDataStore) -> None:
        store.mark_delisted("GAZP", date(2026, 2, 1))
        cur = store._conn.last_cursor()
        # reason defaults to ""
        assert cur.calls[1][1] == ("GAZP", date(2026, 2, 1), "")

    def test_mark_delisted_normalises_lowercase_ticker(self, store: PostgresDataStore) -> None:
        """Issue #160: lowercase input must be upper-cased before SQL params.

        Without normalisation, ``mark_delisted("sber", ...)`` would silently
        no-op the UPDATE (no row matches ``WHERE ticker = 'sber'``) while
        still INSERTing a "sber" row into delisting_log — leaving
        ``ticker_universe`` and the audit log permanently inconsistent.
        """
        store.mark_delisted("sber", date(2026, 1, 15), reason="merger")
        cur = store._conn.last_cursor()
        assert len(cur.calls) == 2
        sql1, params1 = cur.calls[0]
        assert "UPDATE ticker_universe SET delisted = TRUE" in sql1
        assert params1 == (date(2026, 1, 15), "SBER")
        sql2, params2 = cur.calls[1]
        assert "INSERT INTO delisting_log" in sql2
        assert params2 == ("SBER", date(2026, 1, 15), "merger")

    def test_mark_delisted_normalises_mixed_case_ticker(self, store: PostgresDataStore) -> None:
        """Issue #160: mixed-case input is also normalised to UPPERCASE."""
        store.mark_delisted("SbEr", date(2026, 3, 10))
        cur = store._conn.last_cursor()
        assert cur.calls[0][1] == (date(2026, 3, 10), "SBER")
        assert cur.calls[1][1] == ("SBER", date(2026, 3, 10), "")

    def test_mark_delisted_uppercase_unchanged(self, store: PostgresDataStore) -> None:
        """Regression: already-uppercase input is passed through unchanged."""
        store.mark_delisted("SBER", date(2026, 4, 20))
        cur = store._conn.last_cursor()
        assert cur.calls[0][1] == (date(2026, 4, 20), "SBER")
        assert cur.calls[1][1] == ("SBER", date(2026, 4, 20), "")


# ---------------------------------------------------------------------------
# OHLCV: upsert / query / dedup / migrate
# ---------------------------------------------------------------------------


def _bar(
    ticker: str = "SBER",
    ts: date = date(2026, 8, 14),
    close: str = "105.00",
) -> OHLCVRow:
    return OHLCVRow(
        ticker=ticker,
        ts=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("95.00"),
        close=Decimal(close),
        volume=Decimal("1000000"),
        adj_close=Decimal(close),
    )


class TestUpsertOHLCV:
    def test_empty_returns_zero(self, store: PostgresDataStore) -> None:
        assert store.upsert_ohlcv([]) == 0

    def test_batch_returns_count_and_inserts(self, store: PostgresDataStore) -> None:
        rows = [
            _bar(ts=date(2026, 8, 1)),
            _bar(ts=date(2026, 8, 2)),
            _bar(ts=date(2026, 8, 3)),
        ]
        n = store.upsert_ohlcv(rows)
        assert n == 3
        cur = store._conn.last_cursor()
        assert len(cur.executemany_calls) == 1
        sql, params = cur.executemany_calls[0]
        assert "INSERT INTO ohlcv_daily" in sql
        # Phase 2.6 step 2: PK is now (ticker, ts, source) so the ON CONFLICT
        # clause must include the new column. Single-source callers see no
        # behaviour change because OHLCVRow defaults source='tkf'.
        assert "ON CONFLICT (ticker, ts, source) DO UPDATE" in sql
        assert len(params) == 3
        # The third param column is now the source tag (default 'tkf' for
        # rows constructed via _bar()); the open / close / volume / adj_close
        # indices have shifted by +1.
        assert params[0][2] == "tkf"  # source (default for single-source rows)
        assert params[0][3] == "100.00"  # open
        assert params[0][6] == "105.00"  # close
        assert params[0][7] == "1000000"  # volume
        assert params[0][8] == "105.00"  # adj_close

    def test_covered_flags_passed_through(self, store: PostgresDataStore) -> None:
        row = _bar()
        store.upsert_ohlcv([row])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        # 9 params per row: ticker, ts, source, open, high, low, close,
        # volume, adj_close. The +1 vs the v1 test reflects the source
        # column added in Phase 2.6 step 2.
        assert len(params[0]) == 9

    def test_batch_with_explicit_moex_source_uses_moex_in_clause(self, store: PostgresDataStore) -> None:
        """A multi-source caller (MOEX loader) propagates source='moex' to the SQL."""
        row = _bar(ts=date(2026, 8, 1)).model_copy(update={"source": "moex"})
        store.upsert_ohlcv([row])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        assert params[0][2] == "moex"


class TestBackfillWithDedup:
    def test_empty_returns_zero_zero(self, store: PostgresDataStore) -> None:
        assert store.backfill_with_dedup([]) == {"inserted": 0, "skipped": 0}

    def test_filters_existing_pairs(self, store: PostgresDataStore) -> None:
        rows = [
            _bar(ts=date(2026, 8, 1)),
            _bar(ts=date(2026, 8, 2)),
            _bar(ts=date(2026, 8, 3)),
        ]
        # Mark (SBER, 2026-08-02) as already covered.
        store._conn.next_fetchall.append([("SBER", date(2026, 8, 2))])
        result = store.backfill_with_dedup(rows)
        assert result == {"inserted": 2, "skipped": 1}
        # The SELECT was issued with IN ((%s,%s),(%s,%s),(%s,%s))
        cur = store._conn.cursors[-2]  # the SELECT cursor
        sql, params = cur.calls[0]
        assert "SELECT DISTINCT ticker, ts FROM ohlcv_daily" in sql
        assert "IN ((%s,%s),(%s,%s),(%s,%s))" in sql
        assert isinstance(params, list)
        # 3 unique pairs (SBER, ts) → 6 placeholders, flat list.
        assert len(params) == 6

    def test_all_new_inserts_all(self, store: PostgresDataStore) -> None:
        rows = [_bar(ts=date(2026, 8, d)) for d in range(1, 4)]
        store._conn.next_fetchall.append([])  # nothing covered
        result = store.backfill_with_dedup(rows)
        assert result == {"inserted": 3, "skipped": 0}

    def test_all_skipped_no_upsert(self, store: PostgresDataStore) -> None:
        rows = [_bar(ts=date(2026, 8, 1))]
        store._conn.next_fetchall.append([("SBER", date(2026, 8, 1))])
        cursors_before = len(store._conn.cursors)
        result = store.backfill_with_dedup(rows)
        assert result == {"inserted": 0, "skipped": 1}
        # No follow-up upsert → no new cursor opened beyond the SELECT.
        assert len(store._conn.cursors) == cursors_before + 1
        # The single new cursor did NOT call executemany.
        assert store._conn.last_cursor().executemany_calls == []

    def test_custom_source_arg(self, store: PostgresDataStore) -> None:
        """Source arg is accepted (the dedup SELECT itself is source-agnostic
        — coverage is 'any source')."""
        store._conn.next_fetchall.append([])
        result = store.backfill_with_dedup([_bar()], source="moex")
        assert result["inserted"] == 1

    def test_lowercase_ticker_via_model_construct_dedups(self, store: PostgresDataStore) -> None:
        """Issue #224: a row built via model_construct with lowercase ticker must
        still be detected as covered by an existing uppercase row in the DB.

        Prior to the fix, backfill_with_dedup built its dedup key as
        ``(r.ticker, r.ts)`` — i.e. ("sber", ts) — but the DB stores rows under
        ("SBER", ts) (upsert_ohlcv normalises via r.ticker.upper()). The SELECT
        against ohlcv_daily therefore missed the existing row, and the new row
        was silently re-inserted, defeating the cross-source dedup contract.
        """
        # Pre-mark (SBER, 2026-08-14) as covered by an existing source.
        store._conn.next_fetchall.append([("SBER", date(2026, 8, 14))])
        # Build a row with lowercase ticker via model_construct (bypasses
        # OHLCVRow._v_ticker validator).
        row = OHLCVRow.model_construct(
            ticker="sber",
            ts=date(2026, 8, 14),
            open=Decimal("100.00"),
            high=Decimal("110.00"),
            low=Decimal("95.00"),
            close=Decimal("105.00"),
            volume=Decimal("1000000"),
            adj_close=Decimal("105.00"),
            source="moex",
        )
        cursors_before = len(store._conn.cursors)
        result = store.backfill_with_dedup([row])
        # The dedup contract is honoured: lowercase input is matched against
        # the uppercase existing row → skip.
        assert result == {"inserted": 0, "skipped": 1}
        # No follow-up upsert → no new cursor opened beyond the SELECT.
        assert len(store._conn.cursors) == cursors_before + 1
        assert store._conn.last_cursor().executemany_calls == []
        # The SELECT was issued with the NORMALISED ticker ("SBER", not "sber").
        cur = store._conn.cursors[-1]
        _, params = cur.calls[0]
        assert params[0] == "SBER"
        assert params[1] == date(2026, 8, 14)


class TestMigrateDeduplicate:
    def test_returns_rowcount(self, store: PostgresDataStore) -> None:
        store._conn.next_rowcount.append(7)
        deleted = store.migrate_deduplicate()
        assert deleted == 7
        cur = store._conn.last_cursor()
        sql = cur.calls[0][0]
        assert "WITH ranked AS" in sql
        assert "ROW_NUMBER() OVER" in sql
        assert "PARTITION BY ticker, ts" in sql
        assert "DELETE FROM ohlcv_daily" in sql
        assert store._conn.commit_calls >= 1

    def test_returns_zero_when_no_dups(self, store: PostgresDataStore) -> None:
        store._conn.next_rowcount.append(0)
        assert store.migrate_deduplicate() == 0


class TestQueryOHLCV:
    def test_query_without_source(self, store: PostgresDataStore) -> None:
        """When source is not specified, query_ohlcv SELECTs every column
        including the new ``source`` column (Phase 2.6 step 2). The mock
        fetch returns 9 columns so the v2 SELECT projection matches."""
        store._conn.next_fetchall.append(
            [
                (
                    "SBER",
                    date(2026, 8, 14),
                    "tkf",  # new source column
                    "100.00",
                    "110.00",
                    "95.00",
                    "105.00",
                    "1000000",
                    "105.00",
                )
            ]
        )
        rows = store.query_ohlcv("sber", date(2026, 8, 1), date(2026, 8, 31))
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        assert "FROM ohlcv_daily WHERE ticker = %s AND ts BETWEEN %s AND %s" in sql
        # Ticker is uppercased.
        assert params[0] == "SBER"
        assert params[1] == date(2026, 8, 1)
        assert params[2] == date(2026, 8, 31)
        assert len(rows) == 1
        assert rows[0].ticker == "SBER"
        assert rows[0].source == "tkf"  # v2: every returned row carries its source
        assert rows[0].close == Decimal("105.00")
        assert rows[0].volume == Decimal("1000000")

    def test_query_with_source_filter(self, store: PostgresDataStore) -> None:
        """Pass source='moex' to filter on the new PK column."""
        store._conn.next_fetchall.append([])
        store.query_ohlcv(
            "SBER",
            date(2026, 8, 1),
            date(2026, 8, 31),
            source="moex",
        )
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        assert "ORDER BY ts, source" in sql  # v2: ORDER BY ts, source (stable for multi-source)
        assert "AND source = %s" in sql  # v2: source filter clause
        assert params == ["SBER", date(2026, 8, 1), date(2026, 8, 31), "moex"]

    def test_query_without_source_param_has_no_and_source(self, store: PostgresDataStore) -> None:
        """When source is not passed, the SQL must not include the AND clause."""
        store._conn.next_fetchall.append([])
        store.query_ohlcv(
            "SBER",
            date(2026, 8, 1),
            date(2026, 8, 31),
        )
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        assert "AND source = %s" not in sql
        assert params == ["SBER", date(2026, 8, 1), date(2026, 8, 31)]

    def test_query_short_row_defaults(self) -> None:
        """A row with 8 columns (legacy fixture) parses into OHLCVRow with source='tkf'.

        Phase 2.6 step 2 backwards-compat: a hypothetical code path that
        SELECTs from ohlcv_daily without the new column (e.g. a test that
        hand-wrote a 8-tuple) must not crash; _row_to_ohlcv falls back to
        source='tkf' via the ``len(r) > 2`` guard.
        """
        row = ("SBER", date(2026, 8, 14), "100", "110", "95", "105", "1000", "105")
        parsed = _row_to_ohlcv(row)
        assert parsed.source == "tkf"
        assert parsed.close == Decimal("105")


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------


class TestCorporateActions:
    def test_upsert_empty(self, store: PostgresDataStore) -> None:
        assert store.upsert_corporate_actions([]) == 0

    def test_upsert_batch(self, store: PostgresDataStore) -> None:
        rows = [
            CorporateAction(
                ticker="SBER",
                ts=date(2026, 6, 1),
                kind="dividend",
                value=Decimal("12.50"),
                source="tkf",
            ),
            CorporateAction(
                ticker="SBER",
                ts=date(2026, 7, 1),
                kind="dividend",
                value=Decimal("13.00"),
                source="moex",
            ),
        ]
        n = store.upsert_corporate_actions(rows)
        assert n == 2
        cur = store._conn.last_cursor()
        sql, params = cur.executemany_calls[0]
        assert "INSERT INTO corporate_actions" in sql
        assert "ON CONFLICT (ticker, ts, kind, source) DO UPDATE" in sql
        # Value converted via str()
        assert params[0][3] == "12.50"
        assert params[1][3] == "13.00"

    def test_query(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchall.append(
            [
                ("SBER", date(2026, 6, 1), "dividend", "12.50", "tkf"),
                ("SBER", date(2026, 7, 1), "dividend", "13.00", "moex"),
            ]
        )
        rows = store.query_corporate_actions("sber", date(2026, 1, 1), date(2026, 12, 31))
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        assert "FROM corporate_actions" in sql
        assert "WHERE ticker = %s AND ts BETWEEN %s AND %s ORDER BY ts" in sql
        assert params == ("SBER", date(2026, 1, 1), date(2026, 12, 31))
        assert len(rows) == 2
        assert rows[0].kind == "dividend"
        assert rows[0].value == Decimal("12.50")
        assert rows[1].source == "moex"


# ---------------------------------------------------------------------------
# count_ohlcv
# ---------------------------------------------------------------------------


class TestCountOHLCV:
    def test_with_ticker(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchone.append((42,))
        n = store.count_ohlcv("sber")
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        assert "SELECT COUNT(*) FROM ohlcv_daily WHERE ticker = %s" in sql
        assert params == ("SBER",)
        assert n == 42

    def test_without_ticker(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchone.append((12345,))
        n = store.count_ohlcv()
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        assert sql == "SELECT COUNT(*) FROM ohlcv_daily"
        assert params is None
        assert n == 12345

    def test_without_ticker_explicit_none(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchone.append((0,))
        n = store.count_ohlcv(ticker=None)
        assert n == 0


# ---------------------------------------------------------------------------
# Pure converters
# ---------------------------------------------------------------------------


class TestRowConverters:
    def test_row_to_ticker_full_row(self) -> None:
        row = (
            "SBER",
            "BBG004730N88",
            "Sberbank",
            10,
            "RU0009029540",
            "RUB",
            "TQBR",
            False,
            None,
            date(2020, 1, 1),
            "manual",
        )
        m = _row_to_ticker(row)
        assert m.ticker == "SBER"
        assert m.figi == "BBG004730N88"
        assert m.name == "Sberbank"
        assert m.lot == 10
        assert m.isin == "RU0009029540"
        assert m.currency == "RUB"
        assert m.class_code == "TQBR"
        assert m.delisted is False
        assert m.delisted_at is None
        assert m.listed_at == date(2020, 1, 1)
        assert m.source == "manual"

    def test_row_to_ticker_currency_empty_defaults_rub(self) -> None:
        row = ("X", None, "X", 1, None, "", None, False, None, None, "tkf")
        m = _row_to_ticker(row)
        assert m.currency == "RUB"

    def test_row_to_ticker_short_row(self) -> None:
        """Rows with ≤7 columns lack class_code; delisted defaults to False.

        The function only safely handles rows of length ≥ 11; shorter rows
        are theoretical (pg_store.list_tickers emits 11-column SELECTs).
        Here we exercise the "len > 7 False" branch only via a constructed
        FakeRow that supports safe indexing up to 10.
        """
        # Build a MagicMock that mimics a row with len=11 but empty
        # currency field to also re-verify the RUB default branch is
        # NOT taken when the value is already "RUB".
        row = (
            "X",
            None,
            "X",
            1,
            None,
            "RUB",  # currency non-empty → no default applied
            None,  # class_code
            False,  # delisted
            None,  # delisted_at
            date(2020, 1, 1),  # listed_at
            "tkf",  # source
        )
        m = _row_to_ticker(row)
        assert m.class_code is None
        assert m.delisted is False
        assert m.currency == "RUB"

    def test_row_to_ticker_delisted_at_none(self) -> None:
        """A row whose delisted_at is None parses correctly even when
        delisted=True (edge case during ETL of legacy rows)."""
        row = (
            "X",
            None,
            "X",
            1,
            None,
            "RUB",
            None,
            True,  # delisted=True but no delisted_at
            None,
            date(2020, 1, 1),
            "manual",
        )
        m = _row_to_ticker(row)
        assert m.delisted is True
        assert m.delisted_at is None

    def test_row_to_ticker_v1_ten_columns_defaults_source_tkf(self) -> None:
        """Issue #104: a 10-column v1 result (no source column) must default
        source to "tkf" instead of reading listed_at (which would crash
        pydantic Literal validation)."""
        # v1-shape: ticker, figi, name, lot, isin, currency, class_code,
        # delisted, delisted_at, listed_at (no source column).
        row = (
            "SBER",
            "BBG004730N88",
            "Sberbank",
            10,
            "RU0009029540",
            "RUB",
            "TQBR",  # class_code present in pg v1
            False,
            None,
            date(2020, 1, 1),
        )
        m = _row_to_ticker(row)
        assert m.ticker == "SBER"
        # class_code slot reads None when len(r) <= 10 (v1 defensive branch).
        assert m.class_code is None
        assert m.listed_at == date(2020, 1, 1)
        assert m.source == "tkf"  # default fallback, NOT listed_at

    def test_row_to_ohlcv_full_row(self) -> None:
        """Phase 2.6 step 2: v2 row shape is (ticker, ts, source, open..adj_close).

        Trailing ``True, False`` are extra columns the v1 fixture carried —
        we now pass them through ``len(r) > 8`` branch where the parser
        only reads indices [0..8]; the trailing noise is harmless.
        """
        row = (
            "SBER",
            date(2026, 8, 14),
            "tkf",  # source at index 2 (v2 layout)
            "100.50",
            "110.75",
            "95.25",
            "105.00",
            "1000000",
            "105.00",
            True,
            False,
        )
        o = _row_to_ohlcv(row)
        assert o.ticker == "SBER"
        assert o.ts == date(2026, 8, 14)
        assert o.source == "tkf"
        assert o.open == Decimal("100.50")
        assert o.high == Decimal("110.75")
        assert o.low == Decimal("95.25")
        assert o.close == Decimal("105.00")
        assert o.volume == Decimal("1000000")
        assert o.adj_close == Decimal("105.00")

    def test_row_to_ohlcv_numeric_inputs(self) -> None:
        """Integer / float values get coerced via str().

        Phase 2.6 step 2: v2 row shape is (ticker, ts, source, open..adj_close).
        """
        row = ("X", date(2026, 1, 1), "moex", 100, 110, 95, 105, 1000, 105, False, True)
        o = _row_to_ohlcv(row)
        assert o.source == "moex"
        assert o.open == Decimal("100")
        assert o.close == Decimal("105")

    def test_row_to_action(self) -> None:
        row = ("SBER", date(2026, 6, 1), "dividend", "12.50", "tkf")
        a = _row_to_action(row)
        assert a.ticker == "SBER"
        assert a.ts == date(2026, 6, 1)
        assert a.kind == "dividend"
        assert a.value == Decimal("12.50")
        assert a.source == "tkf"


# ---------------------------------------------------------------------------
# Lazy import: psycopg is imported in __init__ only.
# ---------------------------------------------------------------------------


class TestPsycopgImport:
    def test_psycopg_attribute_bound(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        assert s._psycopg is not None
        # And it must be the real psycopg module (or whatever it was patched
        # to) — we just check it's callable for ``connect``.
        assert hasattr(s._psycopg, "connect")


# ---------------------------------------------------------------------------
# earliest_ts / latest_ts / ticker_meta — used by age-aware backfill
# completion check in scripts/backfill_history_md.py
# ---------------------------------------------------------------------------


class TestDateRangeHelpers:
    """Mocked-cursor tests for the new range helpers. The actual SQL
    is exercised in test_pg_store_integration.py when ALPHARD_PG_DSN
    is set; here we verify the row-mapping logic + None handling.

    We use a plain MagicMock (no spec) and override the bound methods
    that the SUT calls — PostgresDataStore methods reference
    ``self._conn.cursor().fetchone()`` which is a real call chain;
    patching the chain with a single MagicMock that returns a fixed
    row tuple is enough.
    """

    def _store(self, row: Any) -> Any:
        """Build a store whose ``_conn.cursor().fetchone()`` returns ``row``.

        ``row`` may be ``None`` (no rows), a tuple, or any value the
        caller wants fetchone() to return.
        """
        store = MagicMock()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = row
        cursor_cm = MagicMock()
        cursor_cm.__enter__.return_value = cursor
        cursor_cm.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cursor_cm
        store._conn = conn
        store._connect = MagicMock()
        return store

    def test_earliest_ts_returns_date(self) -> None:
        store = self._store((date(2018, 3, 15),))
        result = PostgresDataStore.earliest_ts(store, "SBER")
        assert result == date(2018, 3, 15)

    def test_earliest_ts_returns_none_when_no_rows(self) -> None:
        store = self._store(None)
        result = PostgresDataStore.earliest_ts(store, "EMPTY")
        assert result is None

    def test_latest_ts_returns_date(self) -> None:
        store = self._store((date(2026, 8, 17),))
        result = PostgresDataStore.latest_ts(store, "SBER")
        assert result == date(2026, 8, 17)

    def test_latest_ts_returns_none_when_no_rows(self) -> None:
        store = self._store(None)
        result = PostgresDataStore.latest_ts(store, "EMPTY")
        assert result is None

    def test_ticker_meta_returns_tuple_for_delisted(self) -> None:
        store = self._store((date(2020, 1, 1), date(2025, 6, 1)))
        result = PostgresDataStore.ticker_meta(store, "DELISTED")
        assert result == (date(2020, 1, 1), date(2025, 6, 1))

    def test_ticker_meta_returns_tuple_for_live(self) -> None:
        store = self._store((date(2024, 3, 1), None))
        result = PostgresDataStore.ticker_meta(store, "LIVE")
        listed_at, delisted_at = result
        assert listed_at == date(2024, 3, 1)
        assert delisted_at is None

    def test_ticker_meta_returns_none_for_missing(self) -> None:
        store = self._store(None)
        result = PostgresDataStore.ticker_meta(store, "ORPHAN")
        assert result is None


# ---------------------------------------------------------------------------
# auth_probe — Phase 1.6 H-9: detect silent auth drift after redeploy
# ---------------------------------------------------------------------------


class TestAuthProbe:
    """``PostgresDataStore.auth_probe`` returns True iff both SELECT 1
    and INSERT ... ON CONFLICT DO UPDATE on ``_auth_probe`` succeed.

    Why this test exists: pg_isready reports healthy even when the
    volume's pg_authid holds a scram hash of an older POSTGRES_PASSWORD.
    A real probe must actually try a write under the bot's credentials.
    """

    def _store_with_real_cursor(self) -> tuple[Any, FakeCursor]:
        """Build a store whose ``_conn.cursor()`` returns a FakeCursor
        so we can record the SQL the probe issues."""
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        # In tests we have a real (non-mocked) connection. Replace it
        # with a MagicMock that returns a FakeCursor so the probe's
        # ``with self._conn.cursor() as cur:`` context manager works.
        # auth_probe calls self._connect() which would try to
        # psycopg.connect() — suppress that with a MagicMock.
        cur = FakeCursor(conn=MagicMock())
        cur_cm = MagicMock()
        cur_cm.__enter__.return_value = cur
        cur_cm.__exit__.return_value = False
        s._conn = MagicMock()
        s._conn.cursor.return_value = cur_cm
        s._connect = MagicMock()  # type: ignore[assignment]
        return s, cur

    def test_auth_probe_returns_true_on_success(self) -> None:
        s, cur = self._store_with_real_cursor()
        cur._fetchone_queue = [(1,)]  # SELECT 1 → one row
        assert s.auth_probe() is True
        # _connect was called once.
        s._connect.assert_called_once()
        # Probe issued SELECT 1 and INSERT ... ON CONFLICT.
        sqls = [c[0] for c in cur.calls]
        assert any("SELECT 1" in sql for sql in sqls)
        assert any("INSERT INTO _auth_probe" in sql for sql in sqls)
        # Both statements must use ON CONFLICT to keep the row stable.
        assert any("ON CONFLICT (id) DO UPDATE" in sql for sql in sqls)
        # commit was called so the probe row is durable across the
        # rest of the bot's lifetime.
        assert s._conn.commit.called

    def test_auth_probe_writes_source_label(self) -> None:
        """The ``source`` column is set from the ``source`` kwarg so we
        can distinguish entrypoint-smoke from backfill-pre-run from
        cron-healthcheck in the DB."""
        s, cur = self._store_with_real_cursor()
        cur._fetchone_queue = [(1,)]
        s.auth_probe(source="entrypoint_smoke")
        insert_call = [c for c in cur.calls if "INSERT INTO _auth_probe" in c[0]]
        assert len(insert_call) == 1
        # params is a 1-tuple containing the source string
        assert insert_call[0][1] == ("entrypoint_smoke",)

    def test_auth_probe_returns_false_on_select_failure(self) -> None:
        """If the very first SELECT 1 fails (e.g. password wrong),
        auth_probe returns False and does NOT try the INSERT — there
        is no point writing if the read path is broken."""
        s, cur = self._store_with_real_cursor()
        # Make SELECT 1 raise — simulate psycopg.OperationalError.
        cur.execute = MagicMock(side_effect=RuntimeError("auth failed"))
        assert s.auth_probe() is False

    def test_auth_probe_returns_false_on_insert_failure(self) -> None:
        """If SELECT works but INSERT fails (e.g. permissions, table
        missing), auth_probe returns False. This is the case we hit
        in production 2026-08-18: pg_isready OK, SELECT 1 OK, but
        INSERT raised permission error and the bot silently wrote
        nothing. The probe prevents that."""
        s, cur = self._store_with_real_cursor()
        cur._fetchone_queue = [(1,)]
        # Override execute to raise on the INSERT, not on SELECT.
        original = cur.execute

        def selective_raise(sql: str, params: Any = None) -> None:
            if "INSERT" in sql:
                raise RuntimeError("permission denied for table _auth_probe")
            return original(sql, params)

        cur.execute = MagicMock(side_effect=selective_raise)
        assert s.auth_probe() is False

    def test_auth_probe_does_not_raise(self) -> None:
        """Even if every single thing goes wrong (connect fails,
        cursor fails, etc.), auth_probe must return False, not raise.

        The entrypoint smoke test and the backfill pre-run depend on
        a boolean return — if auth_probe raised, both would abort the
        bot for the wrong reason (uncaught exception in start-up)."""
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect = MagicMock(side_effect=RuntimeError("connect failed"))
        # No exception should escape.
        assert s.auth_probe() is False


# ---------------------------------------------------------------------------
# backfill_complete flag — the per-ticker gate ML/training reads
# ---------------------------------------------------------------------------


class TestBackfillCompleteFlag:
    """The flag is a per-ticker gate: ML queries filter on it. These
    tests pin the behavioural contract: write path sets True/False
    correctly, read path returns the right thing for missing tickers.
    """

    def _store(self, fetchone_returns: Any) -> Any:
        store = MagicMock()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = fetchone_returns
        cursor_cm = MagicMock()
        cursor_cm.__enter__.return_value = cursor
        cursor_cm.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cursor_cm
        store._conn = conn
        store._connect = MagicMock()
        return store

    def test_mark_complete_updates_flag(self) -> None:
        """mark_backfill_complete(True) issues the right UPDATE."""
        store = self._store(None)
        PostgresDataStore.mark_backfill_complete(store, "SBER", complete=True)
        # Check that the SQL was called with the right args
        cur = store._conn.cursor().__enter__()
        assert cur.execute.called
        # First arg is the SQL, second is the params tuple
        call_args = cur.execute.call_args
        assert "UPDATE ticker_universe" in call_args[0][0]
        assert "backfill_complete = TRUE" in call_args[0][0]
        assert "backfill_complete_at = NOW()" in call_args[0][0]
        assert call_args[0][1] == ("SBER",)

    def test_mark_incomplete_clears_flag(self) -> None:
        """mark_backfill_complete(False) sets FALSE and NULLs the timestamp."""
        store = self._store(None)
        PostgresDataStore.mark_backfill_complete(store, "FAIL", complete=False)
        cur = store._conn.cursor().__enter__()
        call_args = cur.execute.call_args
        assert "backfill_complete = FALSE" in call_args[0][0]
        assert "backfill_complete_at = NULL" in call_args[0][0]
        assert call_args[0][1] == ("FAIL",)

    def test_mark_complete_commits(self) -> None:
        """The method must commit (otherwise the flag never reaches ML)."""
        store = self._store(None)
        PostgresDataStore.mark_backfill_complete(store, "SBER", complete=True)
        assert store._conn.commit.called

    def test_is_complete_returns_true(self) -> None:
        store = self._store((True,))
        assert PostgresDataStore.is_backfill_complete(store, "SBER") is True

    def test_is_complete_returns_false(self) -> None:
        store = self._store((False,))
        assert PostgresDataStore.is_backfill_complete(store, "X") is False

    def test_is_complete_returns_false_for_missing_ticker(self) -> None:
        """Ticker not in universe → not complete by default."""
        store = self._store(None)
        assert PostgresDataStore.is_backfill_complete(store, "GHOST") is False

    def test_complete_tickers_lists_only_true(self) -> None:
        store = self._store(None)
        store._conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            ("SBER",),
            ("GAZP",),
        ]
        result = PostgresDataStore.backfill_complete_tickers(store)
        assert result == ["SBER", "GAZP"]

    def test_complete_tickers_empty_universe(self) -> None:
        store = self._store(None)
        store._conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        result = PostgresDataStore.backfill_complete_tickers(store)
        assert result == []

    def test_list_complete_universe_returns_ticker_meta(self) -> None:
        """Issue #334 regression: list_complete_universe() must return
        TickerMeta objects built via _row_to_ticker, NOT raw tuples.

        The previous implementation (cycle108) SELECTed columns in the
        wrong order and tried to position-map them into TickerMeta —
        the very first row would ValidationError because ``r[2]``
        (mapped to ``lot=``) was actually a Russian ``name`` string.
        """
        store = self._store(None)
        # One row in SCHEMA order (matches ``list_tickers`` / ``_row_to_ticker``).
        row = (
            "SBER",  # ticker
            "BBG004730N88",  # figi
            "Сбербанк",  # name
            10,  # lot (int)
            "RU0009029540",  # isin
            "RUB",  # currency
            "TQBR",  # class_code
            False,  # delisted
            None,  # delisted_at
            date(2007, 7, 2),  # listed_at
            "tkf",  # source
        )
        store._conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [row]
        result = PostgresDataStore.list_complete_universe(store)
        assert isinstance(result, list)
        assert len(result) == 1
        meta = result[0]
        # Must be a TickerMeta, NOT a tuple. This is the regression guard.
        assert isinstance(meta, TickerMeta), f"expected TickerMeta, got {type(meta)}"
        assert meta.ticker == "SBER"
        assert meta.figi == "BBG004730N88"
        assert meta.name == "Сбербанк"
        # The bug was here: lot got a Russian name string instead of int.
        assert meta.lot == 10
        assert isinstance(meta.lot, int), f"lot must be int, got {type(meta.lot)}"
        assert meta.class_code == "TQBR"
        assert meta.source == "tkf"
        assert meta.listed_at == date(2007, 7, 2)

    def test_list_complete_universe_issues_correct_select(self) -> None:
        """The SQL must SELECT in the schema column order that
        ``_row_to_ticker`` expects — otherwise the row positions are
        misaligned and TickerMeta validation fails on the first row.
        """
        store = self._store(None)
        store._conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        PostgresDataStore.list_complete_universe(store)
        cur = store._conn.cursor().__enter__()
        sql = cur.execute.call_args[0][0]
        # Must SELECT in column order: ticker, figi, name, lot, isin, currency,
        # class_code, delisted, delisted_at, listed_at, source
        # (matches ticker_universe schema at src/data/schema.sql:29-50
        # and the _row_to_ticker() helper).
        assert "ticker, figi, name, lot, isin, currency, class_code" in sql
        assert "delisted, delisted_at, listed_at, source" in sql
        # Must filter on the backfill_complete flag.
        assert "backfill_complete = TRUE" in sql
        # Must ORDER BY for stable iteration.
        assert "ORDER BY ticker" in sql

    def test_list_complete_universe_empty(self) -> None:
        """If no tickers are marked complete yet (first backfill pass
        has not finished), return [] — not raise."""
        store = self._store(None)
        store._conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        result = PostgresDataStore.list_complete_universe(store)
        assert result == []


# ---------------------------------------------------------------------------
# sync_universe_delisted — multi-row bulk UPSERT for delist dates
# ---------------------------------------------------------------------------


class TestSyncUniverseDelisted:
    """The delist sync writes ``listed_at`` / ``delisted_at`` to the
    ticker_universe table. Tested via a mock cursor; verifies the
    SQL parameters and that cur.rowcount is returned as int.
    """

    def _make_store(self, rowcount: int = 1) -> MagicMock:
        store = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = rowcount
        cursor_cm = MagicMock()
        cursor_cm.__enter__.return_value = cursor
        cursor_cm.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cursor_cm
        store._conn = conn
        store._connect = MagicMock()
        return store

    def test_sync_universe_delisted_runs_executemany(self) -> None:
        """Multiple tickers → one executemany call."""
        store = self._make_store(rowcount=3)
        from datetime import date

        dates = {
            "SBER": (date(2007, 7, 20), None),
            "AMEZ": (date(2004, 7, 19), date(2020, 12, 30)),
            "GAZP": (date(1996, 1, 1), None),
        }
        result = PostgresDataStore.sync_universe_delisted(store, dates)
        assert result == 3
        cursor = store._conn.cursor.return_value.__enter__.return_value
        cursor.executemany.assert_called_once()
        cursor.execute.assert_not_called()

    def test_sync_universe_delisted_passes_correct_params(self) -> None:
        """Each row's (listed_at, delisted_at, ticker) maps to the SQL."""
        from datetime import date

        store = self._make_store(rowcount=1)
        dates = {"SBER": (date(2007, 7, 20), None)}
        PostgresDataStore.sync_universe_delisted(store, dates)
        cursor = store._conn.cursor.return_value.__enter__.return_value
        call = cursor.executemany.call_args
        sql, params = call[0]
        assert "UPDATE ticker_universe" in sql
        assert "COALESCE" in sql
        assert "delisted_at" in sql
        # params is list of tuples (listed_at, delisted_at, ticker)
        assert params == [(date(2007, 7, 20), None, "SBER")]

    def test_sync_universe_delisted_uppercases_ticker(self) -> None:
        """Tickers are uppercased before write — matches the rest of pg_store."""
        from datetime import date

        store = self._make_store(rowcount=1)
        dates = {"sber": (date(2007, 7, 20), None)}
        PostgresDataStore.sync_universe_delisted(store, dates)
        cursor = store._conn.cursor.return_value.__enter__.return_value
        params = cursor.executemany.call_args[0][1]
        assert params[0][2] == "SBER"

    def test_sync_universe_delisted_empty_dict_returns_zero(self) -> None:
        """No work to do → return 0 without opening a cursor."""
        store = self._make_store()
        result = PostgresDataStore.sync_universe_delisted(store, {})
        assert result == 0
        # _connect was never called
        store._connect.assert_not_called()

    def test_sync_universe_delisted_delegated_none_is_kept(self) -> None:
        """delisted_at=None → SQL NULL is passed through (no replacement)."""
        from datetime import date

        store = self._make_store(rowcount=1)
        dates = {"GAZP": (date(1996, 1, 1), None)}
        PostgresDataStore.sync_universe_delisted(store, dates)
        cursor = store._conn.cursor.return_value.__enter__.return_value
        params = cursor.executemany.call_args[0][1]
        # active ticker: delisted_at None
        assert params[0][1] is None

    def test_sync_universe_delisted_sql_uses_coalesce_for_delisted_at(self) -> None:
        """Regression: ``delisted_at`` MUST be COALESCE-wrapped, mirroring
        ``listed_at``. The previous SQL used a bare ``%s`` for
        ``delisted_at``, which OVERWROTE a previously-stored value with
        NULL whenever ``fetch_delist_dates`` returned ``(None, None)``
        for a delisted ticker (e.g. transient ISS outage). The fix
        wraps both columns symmetrically: None upstream = "keep whatever
        we have on disk". See the docstring of
        ``PostgresDataStore.sync_universe_delisted`` for the full
        rationale.
        """
        from datetime import date

        store = self._make_store(rowcount=1)
        dates = {"VSMO": (date(2004, 4, 15), date(2020, 12, 30))}
        PostgresDataStore.sync_universe_delisted(store, dates)
        cursor = store._conn.cursor.return_value.__enter__.return_value
        sql = cursor.executemany.call_args[0][0]
        # listed_at side already protected; delisted_at must be too.
        # Strip whitespace/newlines to make the assertion robust against
        # formatting changes.
        sql_flat = " ".join(sql.split())
        assert "listed_at = COALESCE(%s, listed_at)" in sql_flat
        assert "delisted_at = COALESCE(%s, delisted_at)" in sql_flat, (
            "delisted_at is not COALESCE-wrapped; a transient upstream "
            "None would overwrite a stored delisted_at with NULL."
        )
        # And the param tuple order must still be (listed_at, delisted_at, ticker)
        params = cursor.executemany.call_args[0][1]
        assert params == [(date(2004, 4, 15), date(2020, 12, 30), "VSMO")]

    def test_sync_universe_delisted_none_values_still_passed_in_params(self) -> None:
        """The fix must NOT change the param shape. None values are still
        forwarded to psycopg — the COALESCE happens server-side, not in
        Python. This guards against a regression where someone "fixes"
        the asymmetry by silently dropping None entries on the client.
        """
        from datetime import date

        store = self._make_store(rowcount=2)
        dates = {
            "GAZP": (date(1996, 1, 1), None),  # active — listed_at known, delisted_at unknown
            "VSMO": (date(2004, 4, 15), date(2020, 12, 30)),  # delisted — both known
        }
        PostgresDataStore.sync_universe_delisted(store, dates)
        cursor = store._conn.cursor.return_value.__enter__.return_value
        params = cursor.executemany.call_args[0][1]
        # Order is not guaranteed (dict iteration), check as a set.
        assert sorted(params) == sorted(
            [
                (date(1996, 1, 1), None, "GAZP"),
                (date(2004, 4, 15), date(2020, 12, 30), "VSMO"),
            ]
        )

    def test_sync_universe_delisted_returns_int(self) -> None:
        """psycopg may return rowcount as int or str; we coerce to int."""
        from datetime import date

        store = self._make_store(rowcount="2")
        dates = {"SBER": (date(2007, 7, 20), None)}
        result = PostgresDataStore.sync_universe_delisted(store, dates)
        assert result == 2
        assert isinstance(result, int)


class TestUpsertTickersListedAt:
    """The ON CONFLICT path must propagate listed_at + delisted_at.

    Regression: WUSH (a 2021 SPAC) was stuck with ``listed_at = NULL`` in
    ``ticker_universe`` because the original ON CONFLICT clause omitted
    listed_at / delisted_at from the UPDATE SET. After Tinkoff loader
    started populating ``TickerMeta.listed_at`` from the broker's
    ``ipo_date`` field, those values had no path into the DB on conflict.
    This test pins the fix in place.
    """

    def test_upsert_conflict_clause_updates_listed_at(self, fake_conn_cls: Any) -> None:
        with patch("psycopg.connect", fake_conn_cls):
            store = PostgresDataStore(dsn="host=h dbname=d user=u")
            meta1 = TickerMeta(
                ticker="WUSH",
                figi="BBG000000001",
                name="Wush SPAC",
                lot=1,
                isin="RU000A107J37",
                currency="RUB",
                delisted=False,
                listed_at=None,
                delisted_at=None,
                source="tkf",
            )
            store.upsert_tickers([meta1])

            meta2 = TickerMeta(
                ticker="WUSH",
                figi="BBG000000001",
                name="Wush SPAC",
                lot=1,
                isin="RU000A107J37",
                currency="RUB",
                delisted=False,
                listed_at=date(2021, 11, 25),  # WUSH IPO date
                delisted_at=None,
                source="tkf",
            )
            store.upsert_tickers([meta2])

            # upsert_tickers goes through cursors[-1] — the last created cursor
            cur = fake_conn_cls.last.cursors[-1]
            joined_sql = "\n".join(call[0] for call in cur.executemany_calls)
            assert "listed_at = EXCLUDED.listed_at" in joined_sql, (
                "ON CONFLICT DO UPDATE does not include listed_at — "
                "TinkerMeta.listed_at will not propagate to ticker_universe "
                "on re-sync. The WUSH bug returns."
            )
            assert "delisted = EXCLUDED.delisted" in joined_sql, (
                "ON CONFLICT DO UPDATE does not include delisted flag — "
                "delisted/suspended tickers won't get re-flagged."
            )


# ---------------------------------------------------------------------------
# Ticker case-asymmetry (issue #185)
# ---------------------------------------------------------------------------


class TestTickerCaseAsymmetry:
    """Issue #185: the three pg_store upsert_* sites pass ticker without
    .upper(); normalisation, asymmetric with the corresponding query_*
    methods which all normalise via ticker.upper() at the SQL boundary
    (lines 503, 577, 610). Pydantic validators in src/data/models.py
    catch lowercase on construction, but Model.model_construct bypasses
    validators. These tests prove the SQL-boundary normalisation catches
    that bypass path.

    Sister to PR #184 (which fixed the four SQLite sibling sites plus
    pg_store.upsert_ohlcv_adj — issue #183). PR #184 explicitly deferred
    the three remaining pg_store upsert sites to a follow-up.
    """

    def test_upsert_ohlcv_lowercase_roundtrip(self, store: PostgresDataStore) -> None:
        # model_construct bypasses the _v_ticker validator
        row = OHLCVRow.model_construct(
            ticker="sber",
            ts=date(2026, 1, 1),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("1"),
            adj_close=Decimal("100"),
            source="tkf",
        )
        store.upsert_ohlcv([row])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        # Pre-fix: params[0][0] == 'sber'. Post-fix: 'SBER'.
        assert params[0][0] == "SBER"

    def test_upsert_ohlcv_uppercase_unchanged(self, store: PostgresDataStore) -> None:
        # Regression: already-uppercase input passes through unchanged.
        row = _bar(ticker="SBER")
        store.upsert_ohlcv([row])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        assert params[0][0] == "SBER"

    def test_upsert_corporate_actions_lowercase_roundtrip(self, store: PostgresDataStore) -> None:
        # model_construct bypasses the _v_ticker validator
        row = CorporateAction.model_construct(
            ticker="sber",
            ts=date(2026, 6, 1),
            kind="dividend",
            value=Decimal("12.50"),
            source="tkf",
        )
        store.upsert_corporate_actions([row])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        # Pre-fix: params[0][0] == 'sber'. Post-fix: 'SBER'.
        assert params[0][0] == "SBER"

    def test_upsert_corporate_actions_uppercase_unchanged(self, store: PostgresDataStore) -> None:
        row = CorporateAction(
            ticker="SBER",
            ts=date(2026, 6, 1),
            kind="dividend",
            value=Decimal("12.50"),
            source="tkf",
        )
        store.upsert_corporate_actions([row])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        assert params[0][0] == "SBER"

    def test_upsert_tickers_lowercase_roundtrip(self, store: PostgresDataStore) -> None:
        # model_construct bypasses the _v_ticker validator
        meta = TickerMeta.model_construct(
            ticker="sber",
            figi="BBG004730N88",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            class_code=None,
            delisted=False,
            delisted_at=None,
            listed_at=date(2020, 1, 1),
            source="tkf",
        )
        store.upsert_tickers([meta])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        # Pre-fix: params[0][0] == 'sber'. Post-fix: 'SBER'.
        assert params[0][0] == "SBER"

    def test_upsert_tickers_uppercase_unchanged(self, store: PostgresDataStore) -> None:
        store.upsert_tickers([_meta(ticker="SBER")])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        assert params[0][0] == "SBER"


# ---------------------------------------------------------------------------
# init_schema: schema file content + ADD COLUMN migrations
# ---------------------------------------------------------------------------


class TestSchemaForwardCompat:
    """Verify the production schema.sql contains ADD COLUMN IF NOT EXISTS
    calls so init_schema() works on tables from older images that may
    have a different column set (the in-place schema-drift bug from
    2026-08-18).
    """

    def test_schema_sql_has_add_column_for_known_drift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Look up the schema.sql file the store actually loads by default.
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=test dbname=test user=test")
        from src.data.pg_store import PostgresDataStore

        s = PostgresDataStore(dsn="host=test dbname=test user=test")
        schema_path = s._schema_sql_path
        sql = Path(schema_path).read_text(encoding="utf-8")

        # ticker_universe table is the table that drifted in 2026-08-18.
        # The migration must use ADD COLUMN IF NOT EXISTS for the
        # columns that were missing in the older image.
        assert "ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS lot" in sql
        assert "ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS listed_at" in sql
        assert "ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS backfill_complete" in sql
        # ohlcv_daily: adj_close was added in 1.5; older images skipped it.
        assert "ALTER TABLE ohlcv_daily ADD COLUMN IF NOT EXISTS adj_close" in sql
        # Phase 2.6 step 2: source column added to ohlcv_daily. A v1 image
        # that runs the new schema.sql must land on v2 without a separate
        # migration step (the CREATE TABLE declares the column directly,
        # and the ADD COLUMN IF NOT EXISTS is a forward-compat safety net
        # for images where schema.sql was applied before the column was
        # added to the CREATE TABLE block).
        assert "ALTER TABLE ohlcv_daily ADD COLUMN IF NOT EXISTS source" in sql
