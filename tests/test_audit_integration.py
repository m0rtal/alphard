"""Integration tests for PostgresAuditLog.

These tests run against a live Postgres instance. Set
``ALPHARD_PG_DSN`` environment variable to enable. Without it, tests
skip — this keeps CI fast and doesn't require Docker locally.

In CI the ``postgres:16`` service container (see
``.github/workflows/ci.yml``) sets the DSN for us, so these tests run
on every push and PR. Without the DSN they're skipped silently.

Local test::

    export ALPHARD_PG_DSN="host=192.168.48.3 port=5432 dbname=alphard \\
        user=alphard password=***"
    pytest tests/test_audit_integration.py -v

Why this file exists
--------------------
The PostgresAuditLog class writes to a real DB and its ``_cursor.execute``
+ ``finally: self._conn.close()`` paths were previously marked
``# pragma: no cover`` with an inline comment that cited
``tests/test_pg_store_integration.py`` as coverage evidence. That file
does not import or exercise PostgresAuditLog (see issue #258), so the
rationale was false. This file provides the real coverage the inline
comment claimed:

* ``test_write_event_roundtrip`` exercises ``_cursor.execute(...)``
  (audit.py:164).
* ``test_close_commits_and_closes`` exercises the ``finally`` branch
  in ``close()`` (audit.py:191).
* ``test_make_default_audit_log_uses_pg`` exercises
  ``make_default_audit_log`` when ``$ALPHARD_PG_DSN`` is set.

After landing this file the inline ``# pragma: no cover`` markers on
audit.py are removed (or rather: dropped, since the lines are now
exercised on every CI run).
"""

from __future__ import annotations

import os

import pytest

from src.data.quality.audit import PostgresAuditLog, make_default_audit_log
from src.data.quality.severity import Issue, IssueKind, Severity

DSN = os.environ.get("ALPHARD_PG_DSN")
SKIP_REASON = "ALPHARD_PG_DSN not set; skipping integration test"

# Use a dedicated test table inside the alphard_test schema so we never
# collide with the production ``data_quality_events`` table. The schema
# is created by the fixture below.
TEST_TABLE = "audit_test_events"


@pytest.fixture(scope="module")
def pg_audit():
    """Skip if no DSN. Otherwise return an audit log pointed at an isolated table."""
    if not DSN:
        pytest.skip(SKIP_REASON)
    log = PostgresAuditLog(dsn=DSN, table=TEST_TABLE)
    # Pre-create the schema AND table so we don't depend on a separate migration
    # step. The schema mirrors tests/test_pg_store_integration.py:43 (also
    # ``CREATE SCHEMA IF NOT EXISTS alphard_test``) — the original draft of
    # this fixture missed the schema-creation step and failed on fresh Postgres
    # with ``InvalidSchemaName: schema "alphard_test" does not exist``
    # (closes issue #265; CI's postgres:16 service has no pre-existing schema).
    log._ensure_conn()
    with log._cursor as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS alphard_test")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS alphard_test.{TEST_TABLE} (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
                ticker      TEXT        NOT NULL,
                gate        TEXT        NOT NULL,
                kind        TEXT        NOT NULL,
                severity    TEXT        NOT NULL,
                message     TEXT        NOT NULL,
                count       INTEGER     NOT NULL DEFAULT 0,
                extra       JSONB
            )
            """)
    log._conn.commit()
    try:
        # Wipe any rows left over from a previous failed run.
        with log._cursor as cur:
            cur.execute(f"DELETE FROM alphard_test.{TEST_TABLE}")
        log._conn.commit()
        yield log
    finally:
        log.close()
        # Best-effort cleanup; ignore errors so a flaky teardown never masks
        # the test outcome. Drop the whole schema (CASCADE) so we don't leave
        # other tables around if the fixture was ever extended; this mirrors
        # tests/test_pg_store_integration.py teardown.
        try:
            import psycopg

            with psycopg.connect(DSN) as conn:
                with conn.cursor() as cur:
                    cur.execute("DROP SCHEMA IF EXISTS alphard_test CASCADE")
                conn.commit()
        except Exception:
            pass


class TestPostgresAuditLogWrite:
    def test_write_event_roundtrip(self, pg_audit):
        """Exercises audit.py ``self._cursor.execute(...)``.

        Note: the ``with pg_audit._cursor as cur:`` block below is a
        read-side artifact that bypasses the production ``close()``
        commit path — the cursor context manager's ``__exit__`` commits
        on our behalf. The actual commit-on-close semantics are exercised
        by ``test_close_commits_and_closes`` below, which uses a fresh
        writer and a fresh connection for the read-back.

        Closes #267: prior version claimed "We don't commit per-write;
        close() commits at the end" but the read used the cursor context
        manager which commits independently — the test passed even if
        ``close()`` were a no-op.
        """
        issue = Issue.make(
            gate="ingestion",
            kind=IssueKind.ING_NULL_PRIMARY_KEY,
            message="null ticker in row 42",
            count=1,
            extra={"row_index": 42, "source": "csv"},
        )
        pg_audit.write_event(issue, ticker="PG_AUDIT_TEST", gate="ingestion")

        # The cursor context manager commits on exit; this is a read-side
        # convenience that does NOT exercise the production close()-commits
        # path (see test_close_commits_and_closes for that). We use it here
        # only because pg_audit is the live writer and we're inspecting
        # uncommitted-then-committed state within the same connection.
        with pg_audit._cursor as cur:
            cur.execute(
                f"SELECT ticker, gate, kind, severity, message, count, extra "
                f"FROM alphard_test.{TEST_TABLE} WHERE ticker = %s",
                ("PG_AUDIT_TEST",),
            )
            row = cur.fetchone()
        assert row is not None, "expected exactly one row for PG_AUDIT_TEST"
        ticker, gate, kind, severity, message, count, extra = row
        assert ticker == "PG_AUDIT_TEST"
        assert gate == "ingestion"
        assert kind == IssueKind.ING_NULL_PRIMARY_KEY.value
        assert severity == Severity.CRITICAL.value
        assert message == "null ticker in row 42"
        assert count == 1
        # JSONB comes back as a Python dict (psycopg default).
        assert extra == {"row_index": 42, "source": "csv"}

    def test_write_multiple_events(self, pg_audit):
        """Two writes from the same connection — exercises the no-flush path."""
        for i in range(2):
            issue = Issue.make(
                gate="history",
                kind=IssueKind.HST_SPLIT_DETECTED,
                message=f"split event {i}",
                count=1,
            )
            pg_audit.write_event(issue, ticker="PG_AUDIT_MULTI", gate="history")

        with pg_audit._cursor as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM alphard_test.{TEST_TABLE} " f"WHERE ticker = %s",
                ("PG_AUDIT_MULTI",),
            )
            (n,) = cur.fetchone()
        assert n == 2

    def test_close_commits_and_closes(self, pg_audit):
        """Exercises ``close()`` actually commits pending writes.

        Closes #267: prior version only checked ``_conn is None`` and
        ``_cursor is None`` after ``close()``. A regression that replaced
        ``self._conn.commit()`` with ``pass`` would still leave ``_conn``
        and ``_cursor`` set after close() (the old code set them to None
        only after the finally block) — but more importantly, the test
        never verified the row was actually durable on disk. It was a
        false-confidence test.

        New shape: write through a *fresh* PostgresAuditLog instance so
        we can't accidentally inherit the fixture's connection state,
        call ``close()`` (the only commit path for that instance), then
        read back from a *fresh* connection / cursor — the read-back
        only succeeds if ``close()`` actually committed the transaction.

        Acceptance (closes #267): if ``audit.py:close()`` is patched to
        ``pass`` (commit removed), this test fails because no row is
        visible from the fresh connection. Today it passed even when
        close() did nothing.
        """
        # Use a fresh writer so we have an isolated connection to close.
        # The fixture's pg_audit remains open for sibling tests.
        second = PostgresAuditLog(dsn=DSN, table=TEST_TABLE)
        issue = Issue.make(
            gate="ingestion",
            kind=IssueKind.ING_OUTLIER,
            message="price outlier",
            count=1,
        )
        second.write_event(issue, ticker="PG_AUDIT_CLOSE", gate="ingestion")

        # Pre-close: the live writer has both handles.
        assert second._conn is not None
        assert second._cursor is not None

        # Close is the ONLY commit path for this instance.
        second.close()

        # Post-close: handles cleared.
        assert second._conn is None
        assert second._cursor is None

        # Read-back from a *fresh* connection (and therefore a fresh
        # transaction). This is what proves close() actually committed —
        # a reader on a different connection cannot see uncommitted data.
        import psycopg

        with psycopg.connect(DSN) as verify_conn:
            with verify_conn.cursor() as cur:
                cur.execute(
                    f"SELECT ticker, gate, kind, severity, message, count "
                    f"FROM alphard_test.{TEST_TABLE} WHERE ticker = %s",
                    ("PG_AUDIT_CLOSE",),
                )
                row = cur.fetchone()

        assert row is not None, (
            "row must be durable after close() commits; if this fails, "
            "close() did not actually commit the transaction (issue #267)"
        )
        ticker, gate, kind, severity, message, count = row
        assert ticker == "PG_AUDIT_CLOSE"
        assert gate == "ingestion"
        assert kind == IssueKind.ING_OUTLIER.value
        assert severity == Severity.MEDIUM.value
        assert message == "price outlier"
        assert count == 1

    def test_make_default_audit_log_uses_pg(self):
        """When ``$ALPHARD_PG_DSN`` is set, make_default_audit_log returns PostgresAuditLog.

        Skipped when no DSN is set — mirrors the rest of this file's gating.
        """
        if not DSN:
            pytest.skip(SKIP_REASON)
        log = make_default_audit_log()
        try:
            assert isinstance(log, PostgresAuditLog)
        finally:
            log.close()
