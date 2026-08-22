"""Regression tests for scripts/run_macro_sync.py lazy-connection bug (issue #164).

The original ``run_macro_sync`` accessed ``store._conn`` at two sites
(line 59 ``_latest_in_db``, line 126 ``upsert_regime``) WITHOUT ever
calling ``store._connect()`` first. ``PostgresDataStore`` uses lazy
connection (``self._conn = None`` at __init__), so on a fresh store
``store._conn is None`` and the persistence helpers' duck-type check
(``_is_postgres`` → ``not hasattr(None, "execute")``) misclassified it
as psycopg2. ``None.cursor()`` then raised ``AttributeError``.

Net effect:
  1. Skip-gate was dead — every run re-fetched HTTP + classified + tried
     to upsert, ignoring the 1h window.
  2. Upsert always failed with rc=3 — macro_regime_log was never written.

This test pins down the contract:
  - ``store._conn`` must be connected BEFORE ``_latest_in_db`` is called.
  - ``store._conn`` must be connected BEFORE ``upsert_regime`` is called.
  - When the latest row is fresher than the skip window, main() returns 0
    WITHOUT invoking ``build_snapshot`` (the skip-gate works).
  - When the latest row is older than the skip window (or empty), main()
    proceeds through snapshot + classify + upsert and returns 0.

Coverage:
  * Lazy-conn detection: pre-fix the test would crash with
    ``AttributeError: 'NoneType' object has no attribute 'cursor'``.
  * Skip-gate: latest_regime sees the row the test stored; main exits 0
    without re-fetching.
  * Upsert path: classify + upsert_regime writes the expected regime row.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src/ and scripts/ to sys.path so imports work whether tests run
# from the repo root or from CI's container.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_PATH = str(_PROJECT_ROOT / "src")
_SCRIPTS_PATH = str(_PROJECT_ROOT / "scripts")
for _p in (_SRC_PATH, _SCRIPTS_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.macro.models import MacroRegime, MacroSnapshot  # noqa: E402

# --- FakeConnection: minimal psycopg2 stand-in ------------------------------


class _FakeCursor:
    """Minimal cursor; tests pre-program execute() results."""

    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn
        self.closed = False
        self._fetchone_returns: list = []
        self._fetchall_returns: list = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._conn.last_sql = sql
        self._conn.last_params = params

    def fetchone(self):
        if self._fetchone_returns:
            return self._fetchone_returns.pop(0)
        return None

    def fetchall(self):
        if self._fetchall_returns:
            return self._fetchall_returns.pop(0)
        return []


class _FakeConnection:
    """Minimal psycopg2 connection.

    Every ``cursor()`` call returns a fresh _FakeCursor, but they SHARE
    the queue of ``fetchone_returns`` / ``fetchall_returns`` declared on
    the connection itself. This mirrors psycopg2's behavior where each
    cursor has its own row pointer but queries are routed through the
    same connection — and lets us program a single response that any
    number of cursor() calls will see in order.
    """

    def __init__(self) -> None:
        self.closed = False
        self._cursors: list[_FakeCursor] = []
        self.last_sql: str | None = None
        self.last_params: tuple | None = None
        # Shared queues: each cursor.execute() drains one row at a time.
        # Tests push one row here for the latest_regime cursor.
        self._fetchone_returns: list = []
        self._fetchall_returns: list = []

    def cursor(self) -> _FakeCursor:
        c = _FakeCursor(self)
        # Inherit the connection's queues by reference.
        c._fetchone_returns = self._fetchone_returns
        c._fetchall_returns = self._fetchall_returns
        self._cursors.append(c)
        return c

    def commit(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


# --- Helpers ----------------------------------------------------------------


def _make_snapshot(
    fetched_at: datetime | None = None,
    cbr: str = "10.00",
    usd: str = "90.0000",
    usd_5d: str = "88.0000",
    imoex: str = "3000.00",
    imoex_60d: str = "3000.00",
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


def _make_regime(snap: MacroSnapshot, label: str = "neutral") -> MacroRegime:
    multiplier = Decimal("1.00") if label == "neutral" else Decimal("0.75")
    return MacroRegime(
        regime=label,  # type: ignore[arg-type]
        multiplier=multiplier,
        reason="test",
        snapshot=snap,
    )


def _set_fake_psycopg(monkeypatch) -> _FakeConnection:
    """Monkey-patch psycopg.connect to return a single shared _FakeConnection."""
    fake_conn = _FakeConnection()

    def _fake_connect(*args, **kwargs):
        return fake_conn

    # Patch psycopg.connect at the import location used by pg_store.
    # pg_store does ``import psycopg`` and then ``psycopg.connect``.
    monkeypatch.setattr("psycopg.connect", _fake_connect)
    return fake_conn


def _set_dsn(monkeypatch, dsn: str = "host=fake port=5432 dbname=alphard user=alphard"):
    monkeypatch.setenv("ALPHARD_PG_DSN", dsn)


# --- Tests ------------------------------------------------------------------


class TestLazyConnectionRegression:
    """The bug: pre-fix, store._conn was used before store._connect()."""

    def test_persistence_helpers_raise_on_none_conn(self) -> None:
        """Sanity-check that the persistence helpers DO raise on None conn.

        This is the duck-type bug they exploit: ``_is_postgres(None)``
        returns True (no ``.execute`` attribute → no cursor method → it's
        NOT sqlite3). Pre-fix, ``run_macro_sync`` passed ``store._conn``
        (= None) to these helpers and silently caught the AttributeError.
        """
        from src.macro import persistence  # noqa: E402

        # _is_postgres(None) returns True because None has no .execute.
        assert persistence._is_postgres(None) is True

        # latest_regime and upsert_regime both raise AttributeError because
        # they call ``None.cursor()``.
        with pytest.raises(AttributeError, match="NoneType"):
            persistence.latest_regime(None)
        with pytest.raises(AttributeError, match="NoneType"):
            persistence.upsert_regime(None, _make_regime(_make_snapshot()))

    def test_postgres_store_conn_is_none_until_connect(self) -> None:
        """PostgresDataStore.__init__ sets _conn=None; _connect() opens it.

        Confirms the lazy-connection contract the script relied on without
        checking.
        """
        with patch.dict(os.environ, {"ALPHARD_PG_DSN": "host=h dbname=d user=u"}):
            from src.data.pg_store import PostgresDataStore  # noqa: E402

            store = PostgresDataStore()
            try:
                assert store._conn is None, "PostgresDataStore should not auto-connect in __init__"
            finally:
                # No _connect() called, so _conn is still None; no close needed.
                pass


class TestFixEndToEnd:
    """The fix: main() must call store._connect() before any DB access."""

    def test_skip_gate_returns_zero_when_latest_row_fresh(self, monkeypatch, tmp_path: Path) -> None:
        """Issue #164 regression: skip-gate must fire when latest row is fresh.

        Pre-fix, _latest_in_db raised AttributeError on store._conn=None,
        was caught, returned None, and the skip-gate did NOT fire — every
        run did the full fetch. Post-fix, store._connect() is called
        before _latest_in_db, the latest row is returned, the skip-gate
        fires, and main() returns 0.
        """
        _set_dsn(monkeypatch)
        fake_conn = _set_fake_psycopg(monkeypatch)

        # Program the latest-row cursor to return a fresh regime row.
        # Latest fetched_at = now() so age < skip_window_seconds (default 3600).
        fresh_snap = _make_snapshot(fetched_at=datetime.now(tz=timezone.utc) - timedelta(seconds=10))
        fresh_regime = _make_regime(fresh_snap)
        # Push the row into the CONNECTION's shared queue so any cursor()
        # call (whether from the SET search_path inside _connect, or the
        # latest_regime cursor) drains the same queue.
        fake_conn._fetchone_returns = [
            (
                fresh_snap.fetched_at,
                str(fresh_snap.cbr_key_rate),
                str(fresh_snap.usdrub_close),
                str(fresh_snap.usdrub_5d_prev),
                str(fresh_snap.imoex_close),
                str(fresh_snap.imoex_60d_prev),
                fresh_regime.regime,
                str(fresh_regime.multiplier),
                '{"cbr": "test", "usdrub": "test", "imoex": "test"}',
            )
        ]

        # Import lazily after sys.path / env mutations.
        import run_macro_sync as rms  # noqa: E402

        # Patch argv and state-dir so argparse doesn't touch the real filesystem.
        monkeypatch.setattr("sys.argv", ["run_macro_sync.py", "--state-dir", str(tmp_path)])

        # IMPORTANT: build_snapshot MUST NOT be called if the skip-gate fires.
        # We assert that by patching it to raise — if it is called, the test
        # fails with the patch's marker.
        def _explode(*args, **kwargs):
            raise AssertionError("build_snapshot called even though skip-gate should have fired")

        monkeypatch.setattr(rms, "build_snapshot", _explode)

        rc = rms.main()
        assert rc == 0, (
            f"expected rc=0 (skip-gate), got rc={rc}; "
            "pre-fix bug: _latest_in_db(None) raised AttributeError, "
            "skip-gate disabled, build_snapshot would have been called."
        )

    def test_upsert_path_executes_when_latest_row_stale(self, monkeypatch, tmp_path: Path) -> None:
        """Issue #164 regression: stale row → main() proceeds to upsert.

        Pre-fix, upsert_regime(store._conn=None, regime) raised AttributeError
        and the script returned rc=3. Post-fix, store._connect() runs first
        so upsert_regime sees a live connection, and main() returns 0.
        """
        _set_dsn(monkeypatch)
        fake_conn = _set_fake_psycopg(monkeypatch)

        # latest_regime cursor returns None (no fresh row) → skip-gate does NOT fire.
        fake_conn._fetchone_returns = [None]

        # Build a deterministic snapshot so classify is reproducible.
        snap = _make_snapshot(cbr="16.00")  # triggers risk_off (>15% CBR)

        import run_macro_sync as rms  # noqa: E402

        monkeypatch.setattr("sys.argv", ["run_macro_sync.py", "--state-dir", str(tmp_path)])

        # Stub HTTP/cache layers. build_snapshot returns our pre-built snapshot.
        monkeypatch.setattr(rms, "build_snapshot", lambda state_dir: snap)

        # Counting stub: detect that upsert_regime was called against the LIVE
        # connection (not None). We monkey-patch the persistence.upsert_regime
        # at the module that rms uses, then assert the conn argument is the
        # SAME object as fake_conn — that confirms store._connect() ran.
        upsert_calls: list = []

        def _spy_upsert_regime(conn, regime_arg):
            upsert_calls.append((conn, regime_arg))
            # Sanity: conn must be the live _FakeConnection, not None.
            assert conn is fake_conn, (
                f"upsert_regime received conn={conn!r}; pre-fix bug would "
                "have passed None here, which then crashed on .cursor()"
            )
            assert conn is not None
            assert hasattr(conn, "cursor"), "conn must be a live connection"

        # _latest_in_db uses macro_persistence.latest_regime; patch at source.
        import src.macro.persistence as persistence_mod  # noqa: E402

        monkeypatch.setattr(persistence_mod, "latest_regime", lambda conn: None)
        monkeypatch.setattr(persistence_mod, "upsert_regime", _spy_upsert_regime)

        rc = rms.main()
        assert rc == 0, (
            f"expected rc=0 (full path succeeded), got rc={rc}. "
            "If rc=3, the fix's store._connect() didn't run before upsert."
        )
        # And upsert_regime was called exactly once with the live connection.
        assert len(upsert_calls) == 1, f"expected exactly one upsert_regime call, got {len(upsert_calls)}"
        called_conn, called_regime = upsert_calls[0]
        assert called_conn is fake_conn
        assert called_regime.regime == "risk_off"
        assert called_regime.multiplier == Decimal("0.50")

    def test_connect_failure_propagates_as_rc3(self, monkeypatch, tmp_path: Path) -> None:
        """When Postgres is unreachable, main() returns 3 (Postgres error)."""
        _set_dsn(monkeypatch)

        def _connect_fail(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr("psycopg.connect", _connect_fail)

        import run_macro_sync as rms  # noqa: E402

        monkeypatch.setattr("sys.argv", ["run_macro_sync.py", "--state-dir", str(tmp_path)])

        rc = rms.main()
        assert rc == 3, (
            f"expected rc=3 on connection failure, got rc={rc}; "
            "the fix should propagate StoreError / OperationalError as rc=3."
        )

    def test_connect_is_called_exactly_once(self, monkeypatch, tmp_path: Path) -> None:
        """store._connect() must be called exactly once per main() invocation.

        Guard against an accidental double-connect if someone refactors the
        fix into a helper that ends up calling _connect twice.
        """
        _set_dsn(monkeypatch)
        fake_conn = _set_fake_psycopg(monkeypatch)

        # Empty table → skip-gate doesn't fire → proceed to upsert.
        fake_conn._fetchone_returns = [None]

        snap = _make_snapshot()

        import run_macro_sync as rms  # noqa: E402

        monkeypatch.setattr("sys.argv", ["run_macro_sync.py", "--state-dir", str(tmp_path)])
        monkeypatch.setattr(rms, "build_snapshot", lambda state_dir: snap)

        import src.macro.persistence as persistence_mod  # noqa: E402

        monkeypatch.setattr(persistence_mod, "latest_regime", lambda conn: None)
        monkeypatch.setattr(persistence_mod, "upsert_regime", lambda conn, r: None)

        # Spy on store._connect.
        connect_calls: list = []
        from src.data.pg_store import PostgresDataStore  # noqa: E402

        orig_connect = PostgresDataStore._connect

        def _spy_connect(self):
            connect_calls.append(self)
            return orig_connect(self)

        monkeypatch.setattr(PostgresDataStore, "_connect", _spy_connect)

        rc = rms.main()
        assert rc == 0
        assert len(connect_calls) == 1, (
            f"expected exactly one _connect call, got {len(connect_calls)}; "
            "either the fix is missing or it's double-connecting."
        )
