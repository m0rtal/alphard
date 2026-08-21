"""Tests for src.macro.persistence (Phase 2.3 Macro Agent).

Persistence is store-agnostic — the helpers accept any DB-API connection.
The SQLite path is what we exercise here (matches the project's
``InMemorySQLiteStore`` pattern). Postgres is structurally identical SQL,
validated by the fact that the SQLite path runs end-to-end on the same
``_conn`` the rest of the suite uses.

Coverage:
* Round-trip: upsert then read-back gives the same ``MacroRegime``.
* Upsert on conflict REPLACES (idempotency, not duplication).
* Latest-only ordering (older rows ignored).
* Decimal serialisation survives the TEXT round-trip.
* Sources JSON survives parse/serialise.
* Error path: missing snapshot raises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.data.sqlite_store import InMemorySQLiteStore
from src.macro.models import MacroRegime, MacroSnapshot
from src.macro import persistence
from src.macro.regime import classify


def _snap(
    *,
    cbr: str = "10.00",
    usd: str = "90.0000",
    usd_5d: str = "88.0000",
    imoex: str = "3000.00",
    imoex_60d: str = "3000.00",
    fetched_at: datetime = None,  # type: ignore[assignment]
) -> MacroSnapshot:
    return MacroSnapshot(
        fetched_at=fetched_at or datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        cbr_key_rate=Decimal(cbr),
        usdrub_close=Decimal(usd),
        usdrub_5d_prev=Decimal(usd_5d),
        imoex_close=Decimal(imoex),
        imoex_60d_prev=Decimal(imoex_60d),
        sources={"cbr": "test", "usdrub": "test", "imoex": "test"},
    )


def _regime_for(snap: MacroSnapshot) -> MacroRegime:
    return classify(snap)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_upsert_then_latest_round_trip() -> None:
    store = InMemorySQLiteStore()
    try:
        snap = _snap(cbr="16.00")
        reg = _regime_for(snap)
        persistence.upsert_regime(store._conn, reg)  # type: ignore[attr-defined]

        loaded = persistence.latest_regime(store._conn)  # type: ignore[attr-defined]
        assert loaded is not None
        assert loaded.regime == reg.regime
        assert loaded.multiplier == reg.multiplier
        assert loaded.snapshot is not None
        assert loaded.snapshot.cbr_key_rate == snap.cbr_key_rate
        assert loaded.snapshot.usdrub_close == snap.usdrub_close
        assert loaded.snapshot.fetched_at == snap.fetched_at
    finally:
        store.close()


def test_upsert_is_idempotent_on_same_fetched_at() -> None:
    """Same fetched_at → re-upsert REPLACES (single row)."""
    store = InMemorySQLiteStore()
    try:
        snap1 = _snap(cbr="10.00")
        snap2 = _snap(cbr="20.00")  # same fetched_at (default)
        persistence.upsert_regime(store._conn, _regime_for(snap1))  # type: ignore[attr-defined]
        persistence.upsert_regime(store._conn, _regime_for(snap2))  # type: ignore[attr-defined]

        # Only ONE row in the table.
        cur = store._conn.execute("SELECT COUNT(*) FROM macro_regime_log")  # type: ignore[attr-defined]
        assert cur.fetchone()[0] == 1

        loaded = persistence.latest_regime(store._conn)  # type: ignore[attr-defined]
        assert loaded is not None
        assert loaded.snapshot is not None
        # The LATER upsert wins.
        assert loaded.snapshot.cbr_key_rate == Decimal("20.00")
    finally:
        store.close()


def test_latest_regime_picks_most_recent_fetched_at() -> None:
    """Two rows with different fetched_at → latest_regime returns the newer."""
    store = InMemorySQLiteStore()
    try:
        snap_old = _snap(
            cbr="10.00",
            fetched_at=datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
        )
        snap_new = _snap(
            cbr="20.00",
            fetched_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        )
        persistence.upsert_regime(store._conn, _regime_for(snap_old))  # type: ignore[attr-defined]
        persistence.upsert_regime(store._conn, _regime_for(snap_new))  # type: ignore[attr-defined]

        loaded = persistence.latest_regime(store._conn)  # type: ignore[attr-defined]
        assert loaded is not None
        assert loaded.snapshot is not None
        assert loaded.snapshot.fetched_at == snap_new.fetched_at
        assert loaded.snapshot.cbr_key_rate == Decimal("20.00")
    finally:
        store.close()


def test_latest_regime_empty_table_returns_none() -> None:
    store = InMemorySQLiteStore()
    try:
        loaded = persistence.latest_regime(store._conn)  # type: ignore[attr-defined]
        assert loaded is None
    finally:
        store.close()


def test_decimal_precision_survives_round_trip() -> None:
    """USD/RUB close has 4 decimal places — must not be truncated."""
    store = InMemorySQLiteStore()
    try:
        snap = _snap(usd="91.4523", usd_5d="88.1234")
        persistence.upsert_regime(store._conn, _regime_for(snap))  # type: ignore[attr-defined]
        loaded = persistence.latest_regime(store._conn)  # type: ignore[attr-defined]
        assert loaded is not None
        assert loaded.snapshot is not None
        assert loaded.snapshot.usdrub_close == Decimal("91.4523")
        assert loaded.snapshot.usdrub_5d_prev == Decimal("88.1234")
    finally:
        store.close()


def test_sources_dict_survives_round_trip() -> None:
    store = InMemorySQLiteStore()
    try:
        snap = MacroSnapshot(
            fetched_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
            cbr_key_rate=Decimal("10.00"),
            usdrub_close=Decimal("90.0000"),
            usdrub_5d_prev=Decimal("88.0000"),
            imoex_close=Decimal("3000.00"),
            imoex_60d_prev=Decimal("3000.00"),
            sources={
                "cbr": "https://cbr-xml-daily.ru/daily.xml",
                "usdrub": "cache:usdrub.json",
                "imoex": "https://iss.moex.com/.../MOEX.csv",
            },
        )
        persistence.upsert_regime(store._conn, _regime_for(snap))  # type: ignore[attr-defined]
        loaded = persistence.latest_regime(store._conn)  # type: ignore[attr-defined]
        assert loaded is not None
        assert loaded.snapshot is not None
        assert loaded.snapshot.sources["cbr"] == "https://cbr-xml-daily.ru/daily.xml"
        assert loaded.snapshot.sources["usdrub"] == "cache:usdrub.json"
    finally:
        store.close()


def test_upsert_requires_snapshot() -> None:
    store = InMemorySQLiteStore()
    try:
        bare_regime = MacroRegime(regime="neutral", multiplier=Decimal("1.0"), reason="no snapshot attached")
        with pytest.raises(ValueError, match="requires regime.snapshot"):
            persistence.upsert_regime(store._conn, bare_regime)  # type: ignore[attr-defined]
    finally:
        store.close()


def test_macro_regime_log_table_is_present_after_init() -> None:
    """The schema migration added the table to InMemorySQLiteStore."""
    store = InMemorySQLiteStore()
    try:
        cur = store._conn.execute(  # type: ignore[attr-defined]
            "SELECT name FROM sqlite_master WHERE type='table' AND name='macro_regime_log'"
        )
        assert cur.fetchone() is not None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# _parse_sources edge cases (covers the JSON-decode / wrong-type branches).
# ---------------------------------------------------------------------------


def test_parse_sources_from_mapping_passes_through() -> None:
    """If the driver hands us a dict (psycopg2 JSONB), we use it directly."""
    from src.macro.persistence import _parse_sources

    out = _parse_sources({"cbr": "live", "imoex": "cache"})
    assert out == {"cbr": "live", "imoex": "cache"}


def test_parse_sources_handles_garbage_string() -> None:
    """Bad JSON string → empty dict, never raises."""
    from src.macro.persistence import _parse_sources

    assert _parse_sources("not valid json") == {}
    assert _parse_sources("[1, 2, 3]") == {}  # valid JSON but not a dict


def test_parse_sources_handles_none() -> None:
    from src.macro.persistence import _parse_sources

    assert _parse_sources(None) == {}


# ---------------------------------------------------------------------------
# Postgres branch coverage (duck-typed fake connection).
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple] = []
        self.closed = False
        self._row: tuple | None = None
        # Default SELECT row — used by latest_regime tests.
        self._select_row: tuple | None = (
            datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
            Decimal("10.00"),
            Decimal("90.0000"),
            Decimal("88.0000"),
            Decimal("3000.00"),
            Decimal("2950.00"),
            "neutral",
            Decimal("1.00"),
            {"cbr": "live", "usdrub": "live", "imoex": "live"},
        )

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple | None:
        return self._row if self._row is not None else self._select_row

    def close(self) -> None:
        self.closed = True

    # Context-manager protocol — psycopg2 connections expose ``cursor()``
    # as a context manager (``with conn.cursor() as cur:``), so the
    # production code now relies on that. Test fakes must match.
    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class _FakePgConn:
    """Duck-typed psycopg2 connection — has ``.cursor()`` but NOT ``.execute``."""

    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True


def test_upsert_uses_postgres_branch_when_conn_lacks_execute() -> None:
    """conn.cursor() present + no conn.execute() → psycopg2 path."""
    fake = _FakePgConn()
    snap = _snap()
    persistence.upsert_regime(fake, classify(snap))  # type: ignore[arg-type]
    assert fake.committed
    assert fake.cursor_obj.closed
    assert len(fake.cursor_obj.executed) == 1
    # The SQL must be the Postgres variant (has %s placeholders, JSONB cast).
    sql = fake.cursor_obj.executed[0][0]
    assert "%s::jsonb" in sql
    assert "ON CONFLICT" in sql


def test_latest_regime_uses_postgres_branch() -> None:
    fake = _FakePgConn()
    out = persistence.latest_regime(fake)  # type: ignore[arg-type]
    assert out is not None
    assert out.regime == "neutral"
    assert out.multiplier == Decimal("1.00")
    assert out.snapshot is not None
    assert out.snapshot.cbr_key_rate == Decimal("10.00")
    assert out.snapshot.sources == {"cbr": "live", "usdrub": "live", "imoex": "live"}


def test_latest_regime_postgres_returns_none_when_empty() -> None:
    """Postgres fetchone() returns None when the table is empty."""

    class _EmptyCursor(_FakeCursor):
        def fetchone(self) -> tuple | None:
            return None

    class _EmptyPgConn(_FakePgConn):
        def __init__(self) -> None:
            self.cursor_obj = _EmptyCursor()
            self.committed = False

        def cursor(self) -> _FakeCursor:
            return self.cursor_obj

    fake = _EmptyPgConn()
    assert persistence.latest_regime(fake) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Issue #95: cursor-acquisition failure must propagate, not be masked by
# ``UnboundLocalError`` on the manual try/finally pattern.
# ---------------------------------------------------------------------------


class _FlakyCursorFactory:
    """conn.cursor() raises — simulates a failed-transaction-state connection."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.call_count = 0

    def __call__(self) -> None:
        self.call_count += 1
        raise self._exc


class _ExplodingPgConn:
    """Postgres-flavoured connection whose .cursor() raises on every call."""

    def __init__(self, exc: BaseException) -> None:
        self._cursor_factory = _FlakyCursorFactory(exc)
        self.committed = False  # must remain False after the failure

    def cursor(self) -> None:
        return self._cursor_factory()

    def commit(self) -> None:
        self.committed = True


def test_upsert_postgres_cursor_acquisition_failure_propagates() -> None:
    """Issue #95: cursor() raises → original exception propagates, no UnboundLocalError,
    commit() never runs."""
    sentinel = RuntimeError("simulated failed-transaction state")
    fake = _ExplodingPgConn(sentinel)
    snap = _snap()

    with pytest.raises(RuntimeError) as excinfo:
        persistence.upsert_regime(fake, classify(snap))  # type: ignore[arg-type]

    # The original exception is what surfaces — no UnboundLocalError masking.
    assert excinfo.value is sentinel
    assert "UnboundLocalError" not in str(excinfo.value)
    # Cursor was actually attempted.
    assert fake._cursor_factory.call_count == 1
    # commit() must NOT run when the cursor never opened.
    assert fake.committed is False


def test_latest_regime_postgres_cursor_acquisition_failure_propagates() -> None:
    """Same guarantee for the read path."""
    sentinel = ConnectionError("simulated psycopg2 connection closed")
    fake = _ExplodingPgConn(sentinel)

    with pytest.raises(ConnectionError) as excinfo:
        persistence.latest_regime(fake)  # type: ignore[arg-type]

    assert excinfo.value is sentinel
    assert "UnboundLocalError" not in str(excinfo.value)
