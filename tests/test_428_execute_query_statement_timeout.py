"""Regression tests for issue #428 — alphard-web `execute_query` must enforce a
server-side statement timeout so a hung query cannot wedge the
`ThreadingHTTPServer` request thread.

PR #425 originally dropped ``options='-c statement_timeout=60000'`` from the
psycopg.connect kwargs because libpq discarded the DSN password under the
alphard-web ``network_mode: host`` + scram-sha-256 setup. The follow-up fix
restored the safety net as a per-transaction ``SET LOCAL statement_timeout``
inside the same ``with`` block, leaving the DSN alone.

These tests pin the contract on the ``psycopg.connect`` call shape AND the
statement sequence so a future refactor cannot silently re-introduce the
broken ``options=...`` kwarg path or skip the per-call timeout guard.

Contract:

- ``connect_timeout=10`` is always set on the connect kwargs (the handshake
  guard from #232 — H-NETWORK-DETECT).
- ``options=...`` is NEVER passed on the connect kwargs (the broken path
  that libpq interpreted as dropping the DSN password).
- The statement_timeout safety is enforced via ``SET LOCAL`` inside the
  same transaction (per-call, scoped to the transaction, no DSN pollution).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest


@dataclass
class _Desc:
    """Stand-in for ``psycopg.rows.Row`` description entry — only ``.name`` is read."""

    name: str


class _RecordingCursor:
    """Captures every SQL statement passed to ``execute()`` in order.

    Mirrors the relevant slice of the psycopg3 cursor contract:
    - ``__enter__``/``__exit__`` for the ``with`` block in ``execute_query``.
    - ``description`` exposes a list of ``_Desc`` objects so
      ``[d.name for d in cur.description]`` works.
    - ``fetchall`` returns a single ``(1,)`` row once a SELECT was issued.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.description: list[_Desc] | None = None

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.statements.append(sql)
        if sql.upper().startswith("SELECT") and not self.description:
            self.description = [_Desc("ok")]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [(1,)]


@dataclass
class _RecordedCall:
    """One ``psycopg.connect`` invocation: kwargs + the SQL statements on that cursor."""

    dsn: str
    connect_kwargs: dict[str, Any]
    statements: list[str]


@pytest.fixture
def recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[_RecordedCall]:
    """Replace ``psycopg.connect`` with a recording stub.

    ``src.web.server.execute_query`` does a *local* import of ``psycopg``
    inside the function body, so we patch ``sys.modules['psycopg']``. The
    returned list has one ``_RecordedCall`` per ``connect`` call.
    """
    import sys

    calls: list[_RecordedCall] = []

    def fake_connect(dsn: str, **kwargs: Any) -> MagicMock:
        cursor = _RecordingCursor()
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        conn.cursor.return_value = cursor
        calls.append(_RecordedCall(dsn=dsn, connect_kwargs=dict(kwargs), statements=cursor.statements))
        return conn

    fake_psycopg = MagicMock()
    fake_psycopg.connect = fake_connect
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    return calls


def _run_execute_query() -> list[dict[str, Any]]:
    from src.web import server as server_mod

    return server_mod.execute_query("postgresql://x", "SELECT 1", {})


def test_execute_query_preserves_connect_timeout(recorded_calls: list[_RecordedCall]) -> None:
    """``connect_timeout=10`` must always be set on the connect kwargs."""
    rows = _run_execute_query()
    assert rows == [{"ok": 1}]
    assert len(recorded_calls) == 1
    assert recorded_calls[0].connect_kwargs.get("connect_timeout") == 10


def test_execute_query_does_not_pass_options_kwarg_to_connect(
    recorded_calls: list[_RecordedCall],
) -> None:
    """Regression for #428 — the broken DSN ``options="..."`` kwarg path must NOT return.

    libpq's behaviour with ``connect(dsn, options="-c statement_timeout=...")``
    dropped the password under alphard-web's ``network_mode: host`` +
    scram-sha-256 setup. If a future refactor reintroduces ``options=``,
    this test fails and surfaces the regression.
    """
    _run_execute_query()
    assert "options" not in recorded_calls[0].connect_kwargs, (
        "execute_query must NOT pass `options=` to psycopg.connect — "
        "libpq drops the DSN password under alphard-web's setup (issue #428)."
    )


def test_execute_query_emits_set_local_statement_timeout(
    recorded_calls: list[_RecordedCall],
) -> None:
    """The 60 s runaway-query guard is enforced via ``SET LOCAL`` on every call.

    The contract is: every successful ``execute_query`` call must issue
    ``SET LOCAL statement_timeout = 60000`` (or equivalent per-transaction
    ``SET statement_timeout``) as the first statement on the cursor so a
    hung query fails fast at 60 s instead of wedging the
    ``ThreadingHTTPServer`` thread. The SET LOCAL is scoped to the
    transaction so it never poisons the underlying pool.
    """
    rows = _run_execute_query()
    assert rows == [{"ok": 1}]
    statements = recorded_calls[0].statements
    assert len(statements) >= 2, f"expected SET LOCAL + SELECT, got: {statements!r}"
    first = statements[0].strip().upper()
    assert "STATEMENT_TIMEOUT" in first and "60000" in first, (
        f"expected `SET LOCAL statement_timeout = 60000` as the first " f"statement, got: {statements!r}"
    )
    # The actual SELECT comes after the SET LOCAL.
    assert any(
        "SELECT" in s.upper() for s in statements[1:]
    ), f"expected the user's SELECT after the SET LOCAL, got: {statements!r}"


def test_execute_query_contract_holds_across_multiple_calls(
    recorded_calls: list[_RecordedCall],
) -> None:
    """Repeated calls do not leak kwargs or skip the timeout guard."""
    _run_execute_query()
    _run_execute_query()
    _run_execute_query()
    assert len(recorded_calls) == 3
    for call in recorded_calls:
        assert call.connect_kwargs.get("connect_timeout") == 10
        assert "options" not in call.connect_kwargs
        first_stmt = call.statements[0].strip().upper()
        assert "STATEMENT_TIMEOUT" in first_stmt and "60000" in first_stmt
