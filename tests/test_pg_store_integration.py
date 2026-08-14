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
    """Skip if no DSN. Otherwise create isolated test schema."""
    if not DSN:
        pytest.skip(SKIP_REASON)
    store = PostgresDataStore(DSN)
    try:
        store._connect()
        # Use a separate test schema namespace to avoid colliding with prod
        with store._conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS alphard_test")
            cur.execute("SET search_path TO alphard_test, public")
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
            source="manual",
        )
        n = pg_store.upsert_ohlcv([row])
        assert n == 1

        rows = pg_store.query_ohlcv("PG_OHLCV", date(2026, 8, 1), date(2026, 8, 31))
        assert len(rows) == 1
        assert rows[0].close == Decimal("105.00")
        assert rows[0].volume == Decimal("1000000")

    def test_upsert_replaces_existing(self, pg_store):
        meta = TickerMeta(
            ticker="PG_REPL",
            name="Replace Co",
            lot=1,
            currency="RUB",
            source="manual",
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
            source="manual",
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
            source="manual",
        )
        pg_store.upsert_ohlcv([row1])
        pg_store.upsert_ohlcv([row2])
        rows = pg_store.query_ohlcv("PG_REPL", date(2026, 8, 1), date(2026, 8, 31))
        assert len(rows) == 1  # replaced, not duplicated
        assert rows[0].close == Decimal("210")

    def test_query_outside_range_empty(self, pg_store):
        rows = pg_store.query_ohlcv("PG_OHLCV", date(2099, 1, 1), date(2099, 12, 31))
        assert rows == []

    def test_count_ohlcv(self, pg_store):
        meta = TickerMeta(
            ticker="PG_COUNT",
            name="Count Co",
            lot=1,
            currency="RUB",
            source="manual",
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
                source="manual",
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
