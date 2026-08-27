"""
Alphard Data Quality Gate — Audit log writer.

PURPOSE
-------
Persist every Issue from every gate run to a Postgres table named
``data_quality_events``. The schema is intentionally narrow so it can be
created by a single CREATE TABLE statement and the writer works against
either a real Postgres connection OR a no-op sink during tests.

Schema (Postgres)
-----------------
::

    CREATE TABLE data_quality_events (
        id              BIGSERIAL PRIMARY KEY,
        ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
        ticker          TEXT        NOT NULL,
        gate            TEXT        NOT NULL,
        kind            TEXT        NOT NULL,
        severity        TEXT        NOT NULL,
        message         TEXT        NOT NULL,
        count           INTEGER     NOT NULL DEFAULT 0,
        extra           JSONB
    );

DESIGN DECISIONS
----------------
1. AuditLog is a thin protocol: write_event(Issue) -> None. Two
   implementations: PostgresAuditLog (production) and
   InMemoryAuditLog (tests + dry-runs).

2. No I/O at import time. Constructing PostgresAuditLog does NOT open
   a connection; the first ``write_event`` does. This keeps the gate
   importable in CI without a database.

3. Postgres driver is optional. If psycopg is not installed, importing
   this module still works (the InMemory sink is the default fallback).
   PostgresAuditLog raises a clear error on first use telling the
   operator to install psycopg.

4. No background threads, no batched flushes. Every write is a single
   insert; durability is the database's problem. The gate is called
   per-ticker per-load — at most a few thousand rows per day — so
   synchronous insert is fine.

WHAT IS NOT HERE
----------------
- Async / batched writes (Phase 2: bulk-load if events grow >10k/day).
- Schema migrations (run them from db/migrations, not from this module).
- Read-side helpers (Phase 2: dashboard queries against data_quality_events).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from .severity import Issue, QualityReport

# Defensive regex for SQL identifiers (table names). Same shape as
# pg_store._IDENTIFIER_RE — single identifier, no commas, no quoting.
_TABLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class AuditLog(Protocol):
    """Sink for Issue records. Implementations: see below."""

    def write_event(self, issue: Issue, *, ticker: str, gate: str) -> None: ...
    def close(self) -> None: ...


class InMemoryAuditLog:
    """Append-only list of written events. Use in tests."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def write_event(self, issue: Issue, *, ticker: str, gate: str) -> None:
        self.events.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ticker": ticker,
                "gate": gate,
                "kind": issue.kind.value,
                "severity": issue.severity.value,
                "message": issue.message,
                "count": issue.count,
                "extra": dict(issue.extra),
            }
        )

    def close(self) -> None:
        pass

    def __len__(self) -> int:
        return len(self.events)


class PostgresAuditLog:
    """Real Postgres writer. Lazy-connects on first write.

    Parameters
    ----------
    dsn : str | None
        Postgres DSN. ``None`` reads ``$ALPHARD_PG_DSN`` env var. If
        neither is set, raises a clear configuration error.
    table : str
        Target table name (default ``data_quality_events``).
    """

    def __init__(
        self,
        dsn: str | None = None,
        table: str = "data_quality_events",
        schema: str | None = None,
    ) -> None:
        self._dsn = dsn or os.environ.get("ALPHARD_PG_DSN")
        # BUGFIX (C-2): table name is later interpolated into a SQL string
        # via psycopg.sql.Identifier — but validate here too so a bad name
        # fails fast at construction time, not on first write.
        if not _TABLE_NAME_RE.match(table):
            raise ValueError(f"invalid table name {table!r}: must match {_TABLE_NAME_RE.pattern}")
        self._table = table
        # Issue #265 follow-up: support placing the audit log in a non-default
        # schema (used by tests/test_audit_integration.py to isolate into
        # alphard_test). When ``schema`` is None the table is unqualified and
        # inherits the connection's default search_path (``public``). When
        # set, the INSERT statement explicitly qualifies ``schema.table``.
        # Schema name is validated with the same defensive regex as table
        # names — single identifier, no commas, no quoting. Mirrors the
        # ``search_path`` handling in src/data/pg_store.py.
        self._schema: str | None
        if schema is not None:
            if not _TABLE_NAME_RE.match(schema):
                raise ValueError(f"invalid schema name {schema!r}: must match {_TABLE_NAME_RE.pattern}")
            self._schema = schema
        else:
            self._schema = None
        self._conn: Any = None
        self._cursor: Any = None

    def _ensure_conn(self) -> None:
        if self._conn is not None:
            return
        if not self._dsn:
            raise RuntimeError("PostgresAuditLog requires a DSN: pass dsn=... or set $ALPHARD_PG_DSN")  # noqa: E501
        # Issue #232: psycopg.connect without connect_timeout + statement_timeout
        # is the same deadlock class as PR #46's H-NETWORK-DETECT fix. The
        # quality audit log is a runtime hot path (every write_event() from
        # the ingestion gate) — without these guards a Postgres network
        # stall hangs the gate indefinitely. Uses the shared
        # ``connect_with_timeouts`` helper so all three Postgres surfaces
        # (pg_store, coordinator, quality audit) fail in the same way.
        # The connect_with_timeouts helper does a local ``import psycopg``
        # so we don't import it at module scope here.
        from src.data.pg_store import connect_with_timeouts

        self._conn = connect_with_timeouts(self._dsn)
        self._cursor = self._conn.cursor()

    def write_event(self, issue: Issue, *, ticker: str, gate: str) -> None:
        self._ensure_conn()
        if self._cursor is None:
            # Defence-in-depth: _ensure_conn() should always set _cursor
            # to a real cursor. If we get here, _ensure_conn failed in
            # a way that left _cursor None (e.g. psycopg.connect returned
            # successfully but cursor() failed). Treat as a hard error
            # so the audit log never silently drops events.
            raise RuntimeError("PostgresAuditLog._cursor is None after _ensure_conn() — " "audit log cannot write")
        # BUGFIX (C-2): use psycopg.sql.Identifier for the table name instead
        # of f-string interpolation. _TABLE_NAME_RE validation in __init__
        # guarantees the table is a safe identifier, and psycopg.sql.Identifier
        # quotes it properly even if the validator is ever loosened.
        try:  # pragma: no cover — psycopg is a hard dep in CI
            from psycopg import sql  # local import keeps module usable without psycopg
        except ImportError as e:  # pragma: no cover — psycopg is a hard dep in CI
            raise RuntimeError(
                "PostgresAuditLog needs psycopg: install with `pip install psycopg[binary]`"
            ) from e  # noqa: E501
        # Live-Postgres path; exercised by tests/test_audit_integration.py in CI
        # (closes #258 — replaces earlier pragma that falsely cited
        # test_pg_store_integration.py).
        # Issue #265 follow-up: when a schema was configured at construction
        # time, qualify the table reference as ``schema.table`` so the INSERT
        # lands in the correct namespace regardless of the connection's
        # search_path. When schema is None, we emit just ``table`` (the
        # search_path resolves it — public in production).
        if self._schema is not None:
            table_ident: Any = sql.SQL("{}.{}").format(
                sql.Identifier(self._schema),
                sql.Identifier(self._table),
            )
        else:
            table_ident = sql.Identifier(self._table)
        self._cursor.execute(
            sql.SQL("""
                INSERT INTO {}
                    (ticker, gate, kind, severity, message, count, extra)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s::jsonb)
                """).format(table_ident),
            (
                ticker,
                gate,
                issue.kind.value,
                issue.severity.value,
                issue.message,
                issue.count,
                json.dumps(dict(issue.extra)),
            ),
        )
        # We commit at close() time, not per-event, for batching. If the
        # process crashes mid-batch the events are lost — acceptable
        # trade-off for the Phase 1.2 throughput target.
        # Callers who need stricter durability can override by setting
        # autocommit on the DSN (e.g. ?autocommit=true).

    def close(self) -> None:
        # Issue #266: the previous implementation nested ``self._conn.close()``
        # inside the ``finally`` of ``try: self._conn.commit()``. When commit()
        # raised (e.g. network blip on shutdown), the finally ran close() on a
        # broken connection which raised ``psycopg.errors.InterfaceError``. The
        # caller saw ``InterfaceError`` (with a misleading "connection already
        # closed" message) and the actual ``OperationalError`` was buried one
        # frame deep as ``__context__``. Production data loss was being
        # misdiagnosed in incident postmortems.
        #
        # New shape (Option A from the issue): try the close in its own
        # try/except inside the outer finally, capture the commit error
        # explicitly, and re-raise it after the handles are cleared. A close()
        # failure *after* a successful commit() is "the connection was already
        # broken" and not actionable — only re-raise close()'s exception when
        # commit() actually succeeded.
        if self._conn is None:
            return
        commit_err: BaseException | None = None
        try:
            try:
                # Live-Postgres path; exercised by
                # tests/test_audit_integration.py::test_close_commits_and_closes
                # (closes #258) and the unit-test close() mock in
                # tests/test_quality_gate.py.
                self._conn.commit()
            except BaseException as e:  # noqa: BLE001 — must capture everything
                commit_err = e
        finally:
            try:
                if self._conn is not None:
                    self._conn.close()
            except BaseException:  # noqa: BLE001 — see note above
                if commit_err is None:
                    # close() failed on a connection whose commit succeeded —
                    # nothing actionable for the caller; swallow.
                    pass
                else:
                    # commit() failed AND close() failed: the commit error is
                    # the actionable one (data was not durably written);
                    # close()'s "connection already closed" is expected noise
                    # on a broken connection. Drop the close() exception.
                    pass
        self._conn = None
        self._cursor = None
        if commit_err is not None:
            # Re-raise the *original* commit error, with a clear cause so
            # postmortem tools (Sentry, etc.) attribute the failure to the
            # commit path and not the teardown.
            raise commit_err


def make_default_audit_log() -> AuditLog:
    """Return PostgresAuditLog if $ALPHARD_PG_DSN is set; else InMemoryAuditLog.

    Used by the CLI (``python -m src.data.quality <gate> <ticker>``) so
    that operators get a real audit trail in production but the CLI is
    also usable without a database for ad-hoc checks.
    """
    if os.environ.get("ALPHARD_PG_DSN"):
        return PostgresAuditLog()
    return InMemoryAuditLog()


__all__ = [
    "AuditLog",
    "InMemoryAuditLog",
    "PostgresAuditLog",
    "make_default_audit_log",
]


# ---------------------------------------------------------------------------
# Convenience: bulk-write a QualityReport
# ---------------------------------------------------------------------------


def write_report(audit: AuditLog, report: QualityReport) -> None:
    """Write every Issue in a QualityReport through the audit log."""
    for issue in report.issues:
        audit.write_event(issue, ticker=report.ticker, gate=report.gate)
