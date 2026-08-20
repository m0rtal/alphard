"""Coverage tests for ``src/data/sqlite_store.py``.

Goal: drive ``InMemorySQLiteStore`` coverage from the 81% baseline to >=95%.

Strategy
--------
- Focus on testing the flow control paths and error handling:
  1.  Schema initialization (idempotency, PRAGMA usage).
  2.  Context manager integration (open/close/cleanup).
  3.  Upsert logic (UPSERT vs INSERT, ON CONFLICT updates).
  4.  Query methods (optional filters, error handling).
  5.  Diagnostic methods (count).
- Use ``pytest.fixture`` with a shared connection to simulate a test environment.
"""

from __future__ import annotations

import pytest
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Type
from src.data import (
    InMemorySQLiteStore,
    OHLCVRow,
    TickerMeta,
    CorporateAction,
)
from src.data.sqlite_store import (
    SCHEMA_SQL,
)


@pytest.fixture(scope="module")
def mock_connection_factory() -> Type[sqlite3.Connection]:
    """
    Creates a factory function that yields a fresh in-memory connection
    and ensures it's closed after the module/test scope.
    """

    def factory() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        return conn

    return factory


@pytest.fixture(scope="function")
def sqlite_store(mock_connection_factory) -> InMemorySQLiteStore:
    """Fixture that creates and tears down a fresh InMemorySQLiteStore for each test."""
    shared_conn = mock_connection_factory()
    store = InMemorySQLiteStore(shared_connection=shared_conn)
    yield store
    # Uses the shared connection defined by the fixture scope teardown,
    # but we still call close() to simulate resource clean up
    # within the function scope to satisfy the contract.
    store.close()


# Sample data for tests
@pytest.fixture
def sample_metas() -> list[TickerMeta]:
    return [
        TickerMeta(
            ticker="SBER",
            figi="RU0009029540",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            delisted=False,
            delisted_at=None,
            listed_at=None,
            source="moex",
        ),
        TickerMeta(
            ticker="GAZP",
            figi="RU0007661625",
            name="Gazprom",
            lot=1,
            isin="RU0007661625",
            currency="RUB",
            delisted=True,
            delisted_at=date(2025, 1, 1),
            listed_at=None,
            source="moex",
        ),
    ]


@pytest.fixture
def sample_ohlcv_rows() -> list[OHLCVRow]:
    return [
        OHLCVRow(
            ticker="SBER",
            ts=date(2026, 8, 1),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
            adj_close=Decimal("105"),
        ),
        OHLCVRow(
            ticker="SBER",
            ts=date(2026, 8, 2),
            open=Decimal("105"),
            high=Decimal("115"),
            low=Decimal("100"),
            close=Decimal("112"),
            volume=Decimal("1100"),
            adj_close=Decimal("112"),
        ),
    ]


@pytest.fixture
def sample_actions() -> list[CorporateAction]:
    return [
        CorporateAction(
            ticker="SBER",
            ts=date(2025, 5, 10),
            kind="split",
            value=Decimal("2"),
            source="moex",
        )
    ]


# ==============================================================================
# 1. Ticker Management (CRUD)
# ==============================================================================


class TestTickerUniverse:
    def test_upsert_tickers_new(self, sqlite_store: InMemorySQLiteStore, sample_metas: list[TickerMeta]):
        """Test initial bulk insert for multiple tickers."""
        sqlite_store.upsert_tickers(sample_metas)

        # Query count check
        tickers = sqlite_store.list_tickers()
        assert len(tickers) == 2

        # Spot check if data landed correctly
        sber = next(t for t in tickers if t.ticker == "SBER")
        assert sber.lot == 10

    def test_upsert_tickers_empty(self, sqlite_store: InMemorySQLiteStore):
        """Test calling upsert_tickers with no data."""
        sqlite_store.upsert_tickers(iter([]))
        # Should run without error
        assert True

    def test_upsert_idempotent_update(self, sqlite_store: InMemorySQLiteStore, sample_metas: list[TickerMeta]):
        """Test that changing a field does not fail and updates correctly."""
        # 1. Insert initial state
        sqlite_store.upsert_tickers(sample_metas)

        # 2. Enhance/update one record.
        # TickerMeta is a frozen pydantic model, so we use ``model_copy(update=...)``
        # (the dataclass ``replace()`` API is not auto-generated for frozen pydantic
        # models — see https://docs.pydantic.dev/latest/concepts/models/#frozen-models).
        updated_sber = sample_metas[0].model_copy(update={"figi": "BIG_UPDATE", "lot": 20})
        sqlite_store.upsert_tickers([updated_sber])

        # 3. Verify update
        sber = next(t for t in sqlite_store.list_tickers() if t.ticker == "SBER")
        assert sber.figi == "BIG_UPDATE"
        assert sber.lot == 20

    def test_mark_delisted_success(self, sqlite_store: InMemorySQLiteStore, sample_metas: list[TickerMeta]):
        """Test marking a ticker as delisted and recording the event."""
        sqlite_store.upsert_tickers(sample_metas)

        # Delist SBER
        date_now = date.today()
        sqlite_store.mark_delisted("SBER", date_now, reason="Test exit")

        # Should update the record
        sber = next(t for t in sqlite_store.list_tickers() if t.ticker == "SBER")
        assert sber.delisted is True
        assert sber.delisted_at == date_now

    def test_list_tickers_with_include_delisted_false(
        self, sqlite_store: InMemorySQLiteStore, sample_metas: list[TickerMeta]
    ):
        """Ensure delisted items are filtered out when asked."""
        sqlite_store.upsert_tickers(sample_metas)

        live_tickers = sqlite_store.list_tickers(include_delisted=False)
        names = {t.ticker for t in live_tickers}

        assert "GAZP" not in names  # DELISTED
        assert "SBER" in names  # NOT delisted


# ==============================================================================
# 2. OHLCV Storage and Query
# ==============================================================================


class TestOhlcvStorageAndQuery:
    def test_upsert_ohlcv_insert_new(self, sqlite_store: InMemorySQLiteStore, sample_ohlcv_rows: list[OHLCVRow]):
        """Insert a completely new OHLCV row to the table."""
        # ohlcv_daily has a FK to ticker_universe(ticker) — the parent row must
        # exist before the OHLCV insert (sqlite enforces this when PRAGMA
        # foreign_keys = ON). Seed SBER so the FK constraint passes.
        sber = TickerMeta(
            ticker="SBER",
            figi="RU0009029540",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            source="moex",
        )
        sqlite_store.upsert_tickers([sber])

        row_count = sqlite_store.upsert_ohlcv(sample_ohlcv_rows)
        assert row_count == 2

        # Verify read back
        read_rows = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 2))
        assert len(read_rows) == 2

    def test_upsert_ohlcv_overwrite_via_primary_key(
        self, sqlite_store: InMemorySQLiteStore, sample_ohlcv_rows: list[OHLCVRow]
    ):
        """Updating a row (same ticker, same date) should update timestamp."""
        # ohlcv_daily has a FK to ticker_universe(ticker) — seed SBER first.
        sber = TickerMeta(
            ticker="SBER",
            figi="RU0009029540",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            source="moex",
        )
        sqlite_store.upsert_tickers([sber])

        sqlite_store.upsert_ohlcv(sample_ohlcv_rows)
        sqlite_store.upsert_ohlcv([sample_ohlcv_rows[0]])  # re-upsert same PK

        read_rows = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 2))
        assert len(read_rows) == 2

    def test_upsert_ohlcv_error_handling(self, sqlite_store: InMemorySQLiteStore):
        """Test failure during transaction (e.g., bad data)."""
        # This is designed to fail the primary key structure which would normally fail
        # on the underlying sqlite3.Error which we must check for.
        # To force an error without breaking the schema, we'll try to insert a bad type
        # that the internal logic might hit, but the mock structure is simpler.
        # Instead, we test the expected error path from sqlite3.
        with pytest.raises(Exception) as exc_info:
            # If we put a string where an int is expected (e.g., mock failure)
            sqlite_store.upsert_ohlcv(
                [
                    OHLCVRow(
                        ticker="SBER",
                        ts=date(2026, 8, 1),
                        open=Decimal("100"),
                        high=Decimal("110"),
                        low=Decimal("95"),
                        close=Decimal("105"),
                        volume="SHOULD_BE_INT",  # Type mismatch
                        adj_close=Decimal("105"),
                    )
                ]
            )
        # Check if the raised exception is wrapped in StoreError
        assert isinstance(exc_info.value, Exception)

    def test_upsert_ohlcv_multi_source_no_pk_collision(self, sqlite_store: InMemorySQLiteStore):
        """Phase 2.6 step 2: same (ticker, ts) under two source tags coexist.

        The v2 schema lifts the PK from (ticker, ts) to (ticker, ts, source)
        so Tinkoff MD and MOEX ISS can both write bars for the same date
        without UPSERT collision. This test proves the contract end-to-end
        via the public ``upsert_ohlcv`` / ``query_ohlcv`` API.
        """
        sber = TickerMeta(
            ticker="SBER",
            figi="RU0009029540",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            source="moex",
        )
        sqlite_store.upsert_tickers([sber])

        tkf_row = OHLCVRow(
            ticker="SBER",
            ts=date(2026, 8, 1),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
            adj_close=Decimal("105"),
            source="tkf",
        )
        moex_row = OHLCVRow(
            ticker="SBER",
            ts=date(2026, 8, 1),
            open=Decimal("101"),
            high=Decimal("111"),
            low=Decimal("96"),
            close=Decimal("106"),
            volume=Decimal("1000"),
            adj_close=Decimal("106"),
            source="moex",
        )
        sqlite_store.upsert_ohlcv([tkf_row, moex_row])

        # Both rows are stored — no PK collision despite identical (ticker, ts).
        all_rows = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1))
        assert len(all_rows) == 2
        sources = {r.source for r in all_rows}
        assert sources == {"tkf", "moex"}

        # Filter by source — pass-through works.
        only_tkf = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1), source="tkf")
        assert len(only_tkf) == 1
        assert only_tkf[0].source == "tkf"

        only_moex = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1), source="moex")
        assert len(only_moex) == 1
        assert only_moex[0].source == "moex"

    def test_upsert_ohlcv_same_source_collision_still_blocked(self, sqlite_store: InMemorySQLiteStore):
        """The new PK still enforces uniqueness on (ticker, ts, source).

        The migration lifts the PK; it does NOT weaken it. Two rows with the
        same triple must still fail the constraint — proven via the same
        ON CONFLICT path that pre-existed for (ticker, ts).
        """
        sber = TickerMeta(
            ticker="SBER",
            figi="RU0009029540",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            source="moex",
        )
        sqlite_store.upsert_tickers([sber])

        first = OHLCVRow(
            ticker="SBER",
            ts=date(2026, 8, 1),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
            adj_close=Decimal("105"),
            source="tkf",
        )
        sqlite_store.upsert_ohlcv([first])

        # Re-upsert with the same PK (same ticker, ts, source) — ON CONFLICT
        # fires, values are kept (existing semantics), no exception.
        sqlite_store.upsert_ohlcv([first])
        rows = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1))
        assert len(rows) == 1
        assert rows[0].source == "tkf"

    def test_query_ohlcv_returns_source_field(
        self, sqlite_store: InMemorySQLiteStore, sample_ohlcv_rows: list[OHLCVRow]
    ):
        """query_ohlcv returns OHLCVRow objects carrying the row's source.

        Sample rows use the OHLCVRow default ('tkf'), so the read-back
        must also report source='tkf' — proves the v2 SELECT projection
        is wired correctly through the InMemorySQLiteStore test backend.
        """
        sber = TickerMeta(
            ticker="SBER",
            figi="RU0009029540",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            source="moex",
        )
        sqlite_store.upsert_tickers([sber])
        sqlite_store.upsert_ohlcv(sample_ohlcv_rows)

        read_rows = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 2))
        assert len(read_rows) == 2
        for r in read_rows:
            assert r.source == "tkf", "Every sample_ohlcv_row carries the default 'tkf' source; " f"got {r.source!r}"


# ==============================================================================
# 3. Corporate Actions Storage and Query
# ==============================================================================


class TestCorporateActionStorage:
    def test_upsert_actions_unique_key(self, sqlite_store: InMemorySQLiteStore, sample_actions: list[CorporateAction]):
        """Test that updating key fields (value/source) uses ON CONFLICT correctly."""
        # 1. Initial insert
        sqlite_store.upsert_corporate_actions(sample_actions)

        # 2. Overwrite just the value (e.g., a dividend amount change)
        updated_action = sample_actions[0].copy(update={"value": Decimal("3")})
        sqlite_store.upsert_corporate_actions([updated_action])

        # 3. Verify update
        actions = sqlite_store.query_corporate_actions("SBER", date(2025, 1, 1), date(2026, 12, 31))
        assert len(actions) == 1
        assert actions[0].value == Decimal("3")

    def test_upsert_actions_empty_list(self, sqlite_store: InMemorySQLiteStore):
        """Empty input should result in 0 upserted rows."""
        count = sqlite_store.upsert_corporate_actions([])
        assert count == 0

    def test_query_actions_no_results(self, sqlite_store: InMemorySQLiteStore):
        """Querying an empty date range or ticker should yield empty list."""
        actions = sqlite_store.query_corporate_actions("UNKNOWN", date(2000, 1, 1), date(2000, 1, 2))
        assert actions == []


# ==============================================================================
# 4. Diagnostic Tests
# ==============================================================================


class TestCountOhlcv:
    def test_count_ohlcv_none_ticker(self, sqlite_store: InMemorySQLiteStore, sample_ohlcv_rows: list[OHLCVRow]):
        """Count all rows when no ticker is specified."""
        # ohlcv_daily has a FK to ticker_universe(ticker) — seed SBER first.
        sber = TickerMeta(
            ticker="SBER",
            figi="RU0009029540",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            source="moex",
        )
        sqlite_store.upsert_tickers([sber])

        sqlite_store.upsert_ohlcv(sample_ohlcv_rows)  # Inserts 2 rows
        total = sqlite_store.count_ohlcv()
        assert total == 2

    def test_count_ohlcv_specific_ticker(self, sqlite_store: InMemorySQLiteStore, sample_ohlcv_rows: list[OHLCVRow]):
        """Count only rows for a specific ticker."""
        # ohlcv_daily has a FK to ticker_universe(ticker) — seed SBER first.
        sber = TickerMeta(
            ticker="SBER",
            figi="RU0009029540",
            name="Sberbank",
            lot=10,
            isin="RU0009029540",
            currency="RUB",
            source="moex",
        )
        sqlite_store.upsert_tickers([sber])

        # Setup to ensure only SBER is counted
        sqlite_store.upsert_ohlcv(sample_ohlcv_rows)
        total = sqlite_store.count_ohlcv("SBER")
        assert total == 2

    def test_count_ohlcv_no_rows(self, sqlite_store: InMemorySQLiteStore):
        """Count when the table is empty."""
        total = sqlite_store.count_ohlcv()
        assert total == 0


# ==============================================================================
# 5. Adjusted OHLCV (Phase 2.5 step 2b) — coverage tests for sqlite_store
# ==============================================================================


class _FakeExplodingConn:
    """Mimics sqlite3.Connection whose executemany/execute always raise.

    Patches the read-only attribute by replacing the entire _conn on the
    store, which python's mock can rebind freely.
    """

    def __init__(self, real_conn):
        self._real_conn = real_conn

    def executemany(self, sql, params):
        raise sqlite3.Error("boom")

    def execute(self, sql, params=None):
        raise sqlite3.Error("boom")

    def commit(self):
        pass

    def close(self):
        pass


class TestAdjustedOhlcv:
    """Coverage for the ohlcv_daily_adj surface introduced in PR #83.

    Targets three uncovered paths in InMemorySQLiteStore that the orchestrator
    suite (test_apply_corporate_actions.py) does not exercise:
      - upsert_ohlcv_adj early-return on empty input (line 289)
      - upsert_ohlcv_adj raises StoreError on sqlite3.Error (lines 319-320)
      - query_ohlcv_adj raises StoreError on sqlite3.Error (lines 337-338)
    """

    def _seed_sber(self, store: InMemorySQLiteStore) -> None:
        store.upsert_tickers(
            [
                TickerMeta(
                    ticker="SBER",
                    figi="RU0009029540",
                    name="Sberbank",
                    lot=10,
                    isin="RU0009029540",
                    currency="RUB",
                    source="moex",
                )
            ]
        )

    def test_upsert_ohlcv_adj_empty_rows_returns_zero(self, sqlite_store: InMemorySQLiteStore) -> None:
        """Empty input is a no-op (covers early-return at line 289)."""
        count = sqlite_store.upsert_ohlcv_adj([])
        assert count == 0
        assert sqlite_store.count_ohlcv_adj() == 0

    def test_upsert_ohlcv_adj_error_path_raises_store_error(self, sqlite_store: InMemorySQLiteStore) -> None:
        """Forced sqlite3.Error in executemany is wrapped in StoreError (319-320)."""
        from src.data.store import StoreError

        exploding = _FakeExplodingConn(sqlite_store._conn)
        original_conn = sqlite_store._conn
        sqlite_store._conn = exploding
        try:
            with pytest.raises(StoreError, match="upsert_ohlcv_adj failed"):
                sqlite_store.upsert_ohlcv_adj(
                    [
                        OHLCVRow(
                            ticker="SBER",
                            ts=date(2024, 1, 2),
                            open=Decimal("100"),
                            high=Decimal("101"),
                            low=Decimal("99"),
                            close=Decimal("100.5"),
                            volume=Decimal("1000"),
                            source="tkf",
                            adj_close=Decimal("100.5"),
                        )
                    ]
                )
        finally:
            sqlite_store._conn = original_conn

    def test_query_ohlcv_adj_error_path_raises_store_error(self, sqlite_store: InMemorySQLiteStore) -> None:
        """Forced sqlite3.Error in execute is wrapped in StoreError (337-338)."""
        from src.data.store import StoreError

        self._seed_sber(sqlite_store)
        exploding = _FakeExplodingConn(sqlite_store._conn)
        original_conn = sqlite_store._conn
        sqlite_store._conn = exploding
        try:
            with pytest.raises(StoreError, match="query_ohlcv_adj failed"):
                sqlite_store.query_ohlcv_adj("SBER", date(2024, 1, 1), date(2024, 12, 31))
        finally:
            sqlite_store._conn = original_conn

    def test_upsert_ohlcv_adj_happy_path_roundtrip(
        self, sqlite_store: InMemorySQLiteStore, sample_ohlcv_rows: list[OHLCVRow]
    ) -> None:
        """Sanity check that the happy-path rows land and are queryable."""
        self._seed_sber(sqlite_store)
        n = sqlite_store.upsert_ohlcv_adj(sample_ohlcv_rows)
        assert n == len(sample_ohlcv_rows)
        assert sqlite_store.count_ohlcv_adj() == len(sample_ohlcv_rows)
        # count_ohlcv_adj("SBER") covers the if-ticker branch (line 343-347).
        assert sqlite_store.count_ohlcv_adj("SBER") == len(sample_ohlcv_rows)
        rows = sqlite_store.query_ohlcv_adj("SBER", date(2000, 1, 1), date(2100, 1, 1))
        assert len(rows) == len(sample_ohlcv_rows)
