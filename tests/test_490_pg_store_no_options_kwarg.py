"""Regression tests for issue #490: shared Postgres connections must not pass
the libpq ``options`` kwarg to psycopg.connect.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, *_args: object) -> None:
        self.statements.append(sql)


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.cursors: list[_Cursor] = []

    def cursor(self) -> _Cursor:
        cursor = _Cursor()
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


def test_store_connect_has_no_options_and_sets_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.data.pg_store import PostgresDataStore

    connection = _Connection()
    connect = MagicMock(return_value=connection)
    with patch("psycopg.connect", connect):
        store = PostgresDataStore("host=h dbname=d")
        store._connect()

    assert connect.call_args.kwargs["connect_timeout"] == 10
    assert "options" not in connect.call_args.kwargs
    assert connection.cursors[0].statements == ["SET statement_timeout = 60000"]
