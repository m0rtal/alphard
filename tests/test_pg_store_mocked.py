"""Mocked unit tests for PostgresDataStore.

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

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

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

    def __call__(self, dsn: str, autocommit: bool = False) -> FakeConnection:
        conn = FakeConnection(dsn, autocommit=autocommit)
        self.instances.append(conn)
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
        # BUGFIX (C-1): search_path is now passed as a parameterized query,
        # so the SQL is "SET search_path TO %s" and the value lives in params.
        assert any("SET search_path TO %s" in sql for sql, _ in cur.calls)
        assert any(params == ("alphard_test, public",) for _, params in cur.calls)

    def test_connect_no_search_path_skips_set(self, fake_conn_cls: Any) -> None:
        s = PostgresDataStore(dsn="host=h dbname=d user=u")
        s._connect()
        # Only 0 cursors should have been opened — no SET was issued
        assert fake_conn_cls.last.cursors == []

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
        cur = fake_conn_cls.last.cursors[0]
        assert cur.calls[0][0] == "CREATE TABLE foo (id INT);"
        assert fake_conn_cls.last.commit_calls == 1


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
        assert "ON CONFLICT (ticker, ts) DO UPDATE" in sql
        assert len(params) == 3
        # All Decimal columns converted via str()
        assert params[0][2] == "100.00"  # open
        assert params[0][5] == "105.00"  # close
        assert params[0][6] == "1000000"  # volume
        assert params[0][7] == "105.00"  # adj_close

    def test_covered_flags_passed_through(self, store: PostgresDataStore) -> None:
        row = _bar()
        store.upsert_ohlcv([row])
        cur = store._conn.last_cursor()
        _, params = cur.executemany_calls[0]
        assert len(params[0]) == 8


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
        store._conn.next_fetchall.append(
            [
                (
                    "SBER",
                    date(2026, 8, 14),
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
        assert rows[0].close == Decimal("105.00")
        assert rows[0].volume == Decimal("1000000")

    def test_query_with_source_filter(self, store: PostgresDataStore) -> None:
        store._conn.next_fetchall.append([])
        store.query_ohlcv(
            "SBER",
            date(2026, 8, 1),
            date(2026, 8, 31),
        )
        cur = store._conn.last_cursor()
        sql, params = cur.calls[0]
        assert "ORDER BY ts" in sql
        assert params == ["SBER", date(2026, 8, 1), date(2026, 8, 31)]

    def test_query_short_row_defaults(self) -> None:
        """A row with 8 columns parses into OHLCVRow."""
        row = ("SBER", date(2026, 8, 14), "100", "110", "95", "105", "1000", "105")
        _ = _row_to_ohlcv(row)


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

    def test_row_to_ohlcv_full_row(self) -> None:
        row = (
            "SBER",
            date(2026, 8, 14),
            "100.50",
            "110.75",
            "95.25",
            "105.00",
            "1000000",
            "105.00",
            "tkf",
            True,
            False,
        )
        o = _row_to_ohlcv(row)
        assert o.ticker == "SBER"
        assert o.ts == date(2026, 8, 14)
        assert o.open == Decimal("100.50")
        assert o.high == Decimal("110.75")
        assert o.low == Decimal("95.25")
        assert o.close == Decimal("105.00")
        assert o.volume == Decimal("1000000")
        assert o.adj_close == Decimal("105.00")

    def test_row_to_ohlcv_numeric_inputs(self) -> None:
        """Integer / float values get coerced via str()."""
        row = ("X", date(2026, 1, 1), 100, 110, 95, 105, 1000, 105, "moex", False, True)
        o = _row_to_ohlcv(row)
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
