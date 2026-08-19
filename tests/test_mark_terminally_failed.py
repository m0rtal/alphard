"""Tests for scripts/mark_terminally_failed.py."""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# Force a DSN before importing the script
os.environ.setdefault("ALPHARD_PG_DSN", "postgresql://test:test@localhost:5432/test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "mark_terminally_failed.py")
spec = importlib.util.spec_from_file_location("mark_terminally_failed", SCRIPT)
assert spec is not None and spec.loader is not None
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeCursor:
    """Cursor that returns different rows depending on call number."""

    def __init__(self, select_rows, update_rowcount):
        self._select_rows = select_rows
        self._update_rowcount = update_rowcount
        self._call = 0
        self.execute = self._execute
        self.fetchall = self._fetchall
        self.rowcount = 0
        self.queries: list[tuple] = []

    def _execute(self, sql, params=None):
        self._call += 1
        self.queries.append((sql, params))
        if self._call == 1:
            # SELECT
            self._mode = "select"
        else:
            # UPDATE
            self._mode = "update"
            self.rowcount = self._update_rowcount

    def _fetchall(self):
        if self._mode == "select":
            return self._select_rows
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.cursor = self._cursor_factory  # type: ignore[assignment]
        self.commit = self._commit

    def _cursor_factory(self):
        return self._cursor

    def _commit(self):
        self.committed = True

    # psycopg uses `with conn: ...` for autocommit transactions, so
    # FakeConn must support both `with conn.cursor() as cur` and `with conn as c`.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def _patched_main(monkeypatch, conn):
    """Patch psycopg.connect in the m module and return a context manager."""
    return monkeypatch.setattr(m.psycopg, "connect", lambda *a, **kw: conn)


def test_sql_picks_old_or_null_listed_at():
    sql = m._heuristic_sql()
    assert "delisted_at IS NULL" in sql
    assert "backfill_complete = false" in sql
    assert "class_code IS NULL" in sql
    assert "SPBXM" in sql
    assert "ohlcv_daily" in sql
    assert "listed_at" in sql


def test_dry_run_prints_but_does_not_write(monkeypatch, capsys):
    candidates = [("DELISTED1",), ("DELISTED2",), ("SPBXM_US",)]
    conn = FakeConn(FakeCursor(select_rows=candidates, update_rowcount=0))
    _patched_main(monkeypatch, conn)
    monkeypatch.setattr("sys.argv", ["prog", "--dry-run"])

    rc = m.main()
    out = capsys.readouterr().out
    assert "would mark DELISTED1" in out
    assert "would mark DELISTED2" in out
    assert "would mark SPBXM_US" in out
    # Only SELECT was executed
    assert conn._cursor._call == 1
    assert not conn.committed
    assert rc == 3


def test_live_run_updates_and_commits(monkeypatch, capsys):
    conn = FakeConn(FakeCursor(select_rows=[("T1",), ("T2",)], update_rowcount=2))
    _patched_main(monkeypatch, conn)
    monkeypatch.setattr("sys.argv", ["prog"])

    rc = m.main()
    out = capsys.readouterr().out
    assert "marked T1" in out
    assert "marked T2" in out
    assert conn._cursor._call == 2
    assert conn.committed
    assert rc == 2


def test_no_candidates_returns_zero(monkeypatch, capsys):
    conn = FakeConn(FakeCursor(select_rows=[], update_rowcount=0))
    _patched_main(monkeypatch, conn)
    monkeypatch.setattr("sys.argv", ["prog"])

    rc = m.main()
    assert rc == 0
    assert conn._cursor._call == 1
    assert not conn.committed
    # logger.info went to stderr/logging, not stdout — just confirm rc=0
    # (we trust the FakeConn flag-tracking to prove no UPDATE was issued)


def test_custom_horizon_replaces_interval(monkeypatch, capsys):
    conn = FakeConn(FakeCursor(select_rows=[("X",)], update_rowcount=1))
    _patched_main(monkeypatch, conn)
    monkeypatch.setattr("sys.argv", ["prog", "--horizon-days", "365"])

    m.main()
    # First call is the SELECT; SQL must mention 365
    sql, _ = conn._cursor.queries[0]
    assert "365" in sql
    assert "730" not in sql


def test_dsn_missing_raises(monkeypatch):
    monkeypatch.delenv("ALPHARD_PG_DSN", raising=False)
    with pytest.raises(RuntimeError, match="ALPHARD_PG_DSN not set"):
        m._dsn()
