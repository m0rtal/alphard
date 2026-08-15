"""Integration tests for PostgresDataStore.

These tests run against a live Postgres instance. Set
ALPHARD_PG_DSN environment variable to enable. Without it, tests
skip — this keeps CI fast and doesn't require Docker.

Local test:
    export ALPHARD_PG_DSN="host=192.168.48.3 port=5432 dbname=alphard user=alphard password=***"
    pytest tests/test_pg_store_integration.py -v
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest

from src.data.models import OHLCVRow, TickerMeta
from src.data.pg_store import PostgresDataStore


DSN = os.environ.get("ALPHARD_PG_DSN")
SKIP_REASON = "ALPHARD_PG_DSN not set; skipping integration test"


@pytest.fixture(scope="module")
def pg_store():
    """Skip if no DSN. Otherwise create isolated test schema.

    The ``search_path`` is passed to PostgresDataStore so it survives
    connection re-opens (e.g. test_close_idempotent followed by another
    test triggers _connect which recreates the conn with default
    search_path=public otherwise).
    """
    if not DSN:
        pytest.skip(SKIP_REASON)
    store = PostgresDataStore(DSN, search_path="alphard_test, public")
    try:
        store._connect()
        # Use a separate test schema namespace to avoid colliding with prod
        with store._conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS alphard_test")
        store._conn.commit()
        store.init_schema()
        yield store
    finally:
        # Cleanup
        try:
            with store._conn.cursor() as cur:
                cur.execute("DROP SCHEMA IF EXISTS alphard_test CASCADE")
            store._conn.commit()
        except Exception:
            pass
        store.close()


class TestPostgresDataStoreInit:
    def test_connection_succeeds(self, pg_store):
        assert pg_store._conn is not None
        assert not pg_store._conn.closed

    def test_init_schema_creates_tables(self, pg_store):
        # Should already have schema from fixture
        with pg_store._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'alphard_test' ORDER BY table_name"
            )
            tables = {row[0] for row in cur.fetchall()}
        # Phase 1: required tables
        assert "ticker_universe" in tables
        assert "ohlcv_daily" in tables
        assert "corporate_actions" in tables
        # news_embedding skipped (Phase 3+)


class TestTickerCRUD:
    def test_upsert_and_list_roundtrip(self, pg_store):
        meta = TickerMeta(
            ticker="PG_TEST",
            name="Postgres Test Co",
            lot=10,
            currency="RUB",
            source="manual",
        )
        pg_store.upsert_ticker(meta)
        listed = pg_store.list_tickers(include_delisted=True)
        assert any(m.ticker == "PG_TEST" for m in listed)

    def test_upsert_idempotent(self, pg_store):
        meta1 = TickerMeta(
            ticker="PG_IDEM",
            name="First",
            lot=1,
            currency="RUB",
            source="manual",
        )
        pg_store.upsert_ticker(meta1)
        # Re-upsert with same ticker but different name
        meta2 = TickerMeta(
            ticker="PG_IDEM",
            name="Second",
            lot=2,
            currency="USD",
            source="tkf",
        )
        pg_store.upsert_ticker(meta2)
        listed = pg_store.list_tickers()
        match = [m for m in listed if m.ticker == "PG_IDEM"]
        assert len(match) == 1
        assert match[0].name == "Second"
        assert match[0].lot == 2

    def test_list_excludes_delisted_by_default(self, pg_store):
        # Insert one delisted ticker
        meta = TickerMeta(
            ticker="PG_DELISTED",
            name="Delisted Co",
            lot=1,
            currency="RUB",
            source="manual",
            delisted=True,
        )
        pg_store.upsert_ticker(meta)
        listed = pg_store.list_tickers(include_delisted=False)
        assert not any(m.ticker == "PG_DELISTED" for m in listed)
        listed_all = pg_store.list_tickers(include_delisted=True)
        assert any(m.ticker == "PG_DELISTED" for m in listed_all)

    def test_mark_delisted(self, pg_store):
        meta = TickerMeta(
            ticker="PG_MARK",
            name="Mark",
            lot=1,
            currency="RUB",
            source="manual",
        )
        pg_store.upsert_ticker(meta)
        pg_store.mark_delisted("PG_MARK", date(2026, 8, 14), reason="test")
        listed = pg_store.list_tickers(include_delisted=False)
        assert not any(m.ticker == "PG_MARK" for m in listed)


class TestOHLCVCRUD:
    def test_upsert_and_query_roundtrip(self, pg_store):
        # Need ticker first
        meta = TickerMeta(
            ticker="PG_OHLCV",
            name="OHLCV Co",
            lot=1,
            currency="RUB",
            source="manual",
        )
        pg_store.upsert_ticker(meta)
        row = OHLCVRow(
            ticker="PG_OHLCV",
            ts=date(2026, 8, 14),
            open=Decimal("100.50"),
            high=Decimal("110.75"),
            low=Decimal("95.25"),
            close=Decimal("105.00"),
            volume=Decimal("1000000"),
            adj_close=Decimal("105.00"),
            primary_source="manual",
            covered_by_tkf=False,
            covered_by_moex=False,
        )
        n = pg_store.upsert_ohlcv([row])
        assert n == 1

        rows = pg_store.query_ohlcv("PG_OHLCV", date(2026, 8, 1), date(2026, 8, 31))
        assert len(rows) == 1
        assert rows[0].close == Decimal("105.00")
        assert rows[0].volume == Decimal("1000000")

    def test_upsert_preserves_first_value(self, pg_store):
        """Upsert on (ticker, ts) does NOT overwrite OHLCV — first source wins.

        Documented behaviour in pg_store.upsert_ohlcv: when a (ticker, ts)
        row already exists, the existing OHLCV values are preserved (first
        source wins); only covered_by_* flags are OR'd in.

        To force a NEW bar value, the caller must delete the row first.
        This test pins that intentional invariant.
        """
        meta = TickerMeta(
            ticker="PG_REPL",
            name="Replace Co",
            lot=1,
            currency="RUB",
            source="tkf",
        )
        pg_store.upsert_ticker(meta)
        row1 = OHLCVRow(
            ticker="PG_REPL",
            ts=date(2026, 8, 14),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
            adj_close=Decimal("105"),
            primary_source="tkf",
            covered_by_tkf=True,
            covered_by_moex=False,
        )
        row2 = OHLCVRow(
            ticker="PG_REPL",
            ts=date(2026, 8, 14),  # same date
            open=Decimal("200"),
            high=Decimal("220"),
            low=Decimal("190"),
            close=Decimal("210"),
            volume=Decimal("2000"),
            adj_close=Decimal("210"),
            primary_source="moex",
            covered_by_tkf=False,
            covered_by_moex=True,
        )
        pg_store.upsert_ohlcv([row1])
        pg_store.upsert_ohlcv([row2])
        rows = pg_store.query_ohlcv("PG_REPL", date(2026, 8, 1), date(2026, 8, 31))
        assert len(rows) == 1  # one row per (ticker, ts), not duplicated
        # First-source-wins: row1's OHLCV preserved
        assert rows[0].close == Decimal("105")
        assert rows[0].volume == Decimal("1000")
        # BUT covered_by_* flags OR'd — both sources now confirmed
        assert rows[0].covered_by_tkf is True
        assert rows[0].covered_by_moex is True

    def test_query_outside_range_empty(self, pg_store):
        rows = pg_store.query_ohlcv("PG_OHLCV", date(2099, 1, 1), date(2099, 12, 31))
        assert rows == []

    def test_count_ohlcv(self, pg_store):
        meta = TickerMeta(
            ticker="PG_COUNT",
            name="Count Co",
            lot=1,
            currency="RUB",
            source="tkf",
        )
        pg_store.upsert_ticker(meta)
        rows = [
            OHLCVRow(
                ticker="PG_COUNT",
                ts=date(2026, 8, d),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("95"),
                close=Decimal("105"),
                volume=Decimal("1000"),
                adj_close=Decimal("105"),
                primary_source="manual",
                covered_by_tkf=False,
                covered_by_moex=False,
            )
            for d in range(1, 6)
        ]
        pg_store.upsert_ohlcv(rows)
        assert pg_store.count_ohlcv("PG_COUNT") == 5


class TestErrorPaths:
    def test_invalid_dsn_raises(self, monkeypatch):
        from src.data.store import StoreError

        # CI sets ALPHARD_PG_DSN globally — must clear it for this test
        monkeypatch.delenv("ALPHARD_PG_DSN", raising=False)

        with pytest.raises(StoreError):
            # dsn=None explicitly + env cleared so it actually raises
            PostgresDataStore(dsn=None)

    def test_close_idempotent(self, pg_store):
        pg_store.close()
        pg_store.close()  # should not raise


class TestContextManager:
    def test_context_manager_returns_store(self, pg_store):
        """__enter__ / __exit__ exercise the lazy-connect + close paths."""
        with pg_store as s:
            assert s is pg_store
            assert s._conn is not None
        # __exit__ calls close() → _conn reset to None
        assert pg_store._conn is None


class TestCorporateActions:
    def test_upsert_and_query_roundtrip(self, pg_store):
        from src.data.models import CorporateAction

        meta = TickerMeta(
            ticker="PG_CORP",
            name="Corp Co",
            lot=1,
            currency="RUB",
            source="manual",
        )
        pg_store.upsert_ticker(meta)
        action = CorporateAction(
            ticker="PG_CORP",
            ts=date(2026, 8, 1),
            kind="dividend",
            value=Decimal("12.50"),
            source="tkf",
        )
        n = pg_store.upsert_corporate_actions([action])
        assert n == 1

        rows = pg_store.query_corporate_actions("PG_CORP", date(2026, 1, 1), date(2026, 12, 31))
        assert len(rows) == 1
        assert rows[0].kind == "dividend"
        assert rows[0].value == Decimal("12.50")

    def test_upsert_replaces_existing_action(self, pg_store):
        from src.data.models import CorporateAction

        meta = TickerMeta(
            ticker="PG_SPLIT",
            name="Split Co",
            lot=1,
            currency="RUB",
            source="manual",
        )
        pg_store.upsert_ticker(meta)
        action1 = CorporateAction(
            ticker="PG_SPLIT",
            ts=date(2026, 8, 1),
            kind="split",
            value=Decimal("2"),
            source="tkf",
        )
        action2 = CorporateAction(
            ticker="PG_SPLIT",
            ts=date(2026, 8, 1),
            kind="split",
            value=Decimal("3"),
            source="tkf",
        )
        pg_store.upsert_corporate_actions([action1])
        pg_store.upsert_corporate_actions([action2])
        rows = pg_store.query_corporate_actions("PG_SPLIT", date(2026, 1, 1), date(2026, 12, 31))
        assert len(rows) == 1
        assert rows[0].value == Decimal("3")  # latest write wins


class TestMigrateDeduplicate:
    def test_deduplicate_no_op_when_no_duplicates(self, pg_store):
        """migrate_deduplicate returns 0 when no duplicates exist (steady-state).

        On the current schema with PK (ticker, ts), no duplicates can exist
        in the first place, so this is the realistic path.
        """
        meta = TickerMeta(
            ticker="PG_DEDUP_CL",
            name="Clean Co",
            lot=1,
            currency="RUB",
            source="tkf",
        )
        pg_store.upsert_ticker(meta)
        row = OHLCVRow(
            ticker="PG_DEDUP_CL",
            ts=date(2026, 8, 1),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
            adj_close=Decimal("105"),
            primary_source="tkf",
            covered_by_tkf=True,
            covered_by_moex=False,
        )
        pg_store.upsert_ohlcv([row])
        deleted = pg_store.migrate_deduplicate()
        assert deleted == 0
        assert pg_store.count_ohlcv("PG_DEDUP_CL") == 1


class TestOHLCVQueryVariants:
    def test_query_ohlcv_filters_by_source(self, pg_store):
        meta = TickerMeta(
            ticker="PG_SRC",
            name="Source Co",
            lot=1,
            currency="RUB",
            source="tkf",
        )
        pg_store.upsert_ticker(meta)
        row = OHLCVRow(
            ticker="PG_SRC",
            ts=date(2026, 8, 14),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
            adj_close=Decimal("105"),
            primary_source="tkf",
            covered_by_tkf=True,
            covered_by_moex=False,
        )
        pg_store.upsert_ohlcv([row])

        # No source filter → returns the row
        rows = pg_store.query_ohlcv("PG_SRC", date(2026, 8, 1), date(2026, 8, 31))
        assert len(rows) == 1

        # Match source → returns the row
        rows = pg_store.query_ohlcv("PG_SRC", date(2026, 8, 1), date(2026, 8, 31), primary_source="tkf")
        assert len(rows) == 1

        # Non-matching source → empty
        rows = pg_store.query_ohlcv("PG_SRC", date(2026, 8, 1), date(2026, 8, 31), primary_source="moex")
        assert rows == []

    def test_count_ohlcv_all(self, pg_store):
        # Insert rows for multiple tickers
        for tk in ("PG_CA1", "PG_CA2"):
            meta = TickerMeta(
                ticker=tk,
                name=f"Co {tk}",
                lot=1,
                currency="RUB",
                source="tkf",
            )
            pg_store.upsert_ticker(meta)
            row = OHLCVRow(
                ticker=tk,
                ts=date(2026, 8, 14),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("95"),
                close=Decimal("105"),
                volume=Decimal("1000"),
                adj_close=Decimal("105"),
                primary_source="tkf",
                covered_by_tkf=True,
                covered_by_moex=False,
            )
            pg_store.upsert_ohlcv([row])

        # Per-ticker count
        assert pg_store.count_ohlcv("PG_CA1") == 1
        assert pg_store.count_ohlcv("PG_CA2") == 1
        # Total count (no ticker)
        assert pg_store.count_ohlcv() >= 2

    def test_query_ohlcv_uppercases_ticker(self, pg_store):
        meta = TickerMeta(
            ticker="PG_UPPER",
            name="Upper Co",
            lot=1,
            currency="RUB",
            source="tkf",
        )
        pg_store.upsert_ticker(meta)
        row = OHLCVRow(
            ticker="PG_UPPER",
            ts=date(2026, 8, 14),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
            adj_close=Decimal("105"),
            primary_source="tkf",
            covered_by_tkf=True,
            covered_by_moex=False,
        )
        pg_store.upsert_ohlcv([row])

        # Lowercase query should still match (SQL does UPPER)
        rows = pg_store.query_ohlcv("pg_upper", date(2026, 8, 1), date(2026, 8, 31))
        assert len(rows) == 1


class TestConnectionLifecycle:
    def test_connect_idempotent(self, pg_store):
        """Calling _connect twice shouldn't break."""
        pg_store._connect()
        first_conn = pg_store._conn
        pg_store._connect()  # no-op: conn is not None and not closed
        assert pg_store._conn is first_conn

    def test_close_clears_conn(self, pg_store):
        pg_store._connect()
        assert pg_store._conn is not None
        pg_store.close()
        assert pg_store._conn is None

    def test_reconnect_after_close(self, pg_store):
        pg_store._connect()
        pg_store.close()
        # Re-connect lazy on next operation
        pg_store._connect()
        assert pg_store._conn is not None
