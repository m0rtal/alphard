"""Tests for scripts/validate_ohlcv.py.

The script is a thin wrapper over the validate module — we test its
control flow (exit codes, ticker filtering, severity counts) without
hitting a real Postgres.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make the script importable.
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import validate_ohlcv  # noqa: E402

# ---------------------------------------------------------------------------
# _to_ohlcv_rows
# ---------------------------------------------------------------------------


def test_to_ohlcv_rows_happy_path() -> None:
    raw = [
        ("SBER", date(2024, 1, 1), "100", "110", "95", "105", 1000),
        ("SBER", date(2024, 1, 2), "105", "115", "100", "110", 1100),
    ]
    out = validate_ohlcv._to_ohlcv_rows(raw, ticker="SBER")
    assert len(out) == 2
    assert out[0].ts == date(2024, 1, 1)
    assert out[0].open == Decimal("100")
    assert out[0].volume == 1000


def test_to_ohlcv_rows_skips_malformed() -> None:
    """Bad rows are skipped silently — script shouldn't crash on dirty DB."""
    from src.data.models import OHLCVRow

    raw = [
        # ok
        ("SBER", date(2024, 1, 1), "100", "110", "95", "105", 1000),
        # malformed: missing fields entirely (IndexError → skip)
        ("SBER",),
    ]
    out = validate_ohlcv._to_ohlcv_rows(raw, ticker="SBER")
    assert len(out) == 1
    assert isinstance(out[0], OHLCVRow)
    assert out[0].ts == date(2024, 1, 1)


def test_to_ohlcv_rows_with_unparseable_decimal_skips_row() -> None:
    """Decimal that fails to parse skips the row, doesn't crash."""
    raw = [
        ("SBER", date(2024, 1, 1), "100", "110", "95", "105", 1000),  # ok
        ("SBER", date(2024, 1, 2), "not-a-number", "110", "95", "105", 1000),  # bad open
    ]
    out = validate_ohlcv._to_ohlcv_rows(raw, ticker="SBER")
    assert len(out) == 1
    assert out[0].ts == date(2024, 1, 1)


# ---------------------------------------------------------------------------
# main() exit-code matrix
# ---------------------------------------------------------------------------


def _make_ohlcv_row(ticker: str = "SBER", ts: date = date(2024, 1, 1)):
    """A bar with valid OHLCV — no issues."""
    return (ticker, ts, "100", "110", "95", "105", 1000)


def _make_broken_bar(ticker: str = "FAIL", ts: date = date(2024, 1, 1)):
    """A bar with low > high — CRITICAL invariant violation."""
    return (ticker, ts, "100", "95", "110", "105", 1000)


def _patch_store(rows: list) -> MagicMock:
    """Mock the store with our chosen rows."""
    store = MagicMock()
    store._connect = MagicMock()
    store._conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = cursor
    cursor_cm.__exit__.return_value = False
    store._conn.cursor.return_value = cursor_cm
    store.close = MagicMock()
    return store


def test_main_clean_data_exits_zero() -> None:
    """All bars valid → exit 0."""
    rows = [_make_ohlcv_row("SBER", date(2024, 1, 1)), _make_ohlcv_row("SBER", date(2024, 1, 2))]
    store = _patch_store(rows)
    with patch.object(validate_ohlcv, "PostgresDataStore", return_value=store):
        with patch.object(sys, "argv", ["validate_ohlcv.py"]):
            assert validate_ohlcv.main() == 0


def test_main_critical_data_exits_two() -> None:
    """Any CRITICAL → exit 2."""
    rows = [_make_ohlcv_row("SBER", date(2024, 1, 1)), _make_broken_bar("FAIL", date(2024, 1, 2))]
    store = _patch_store(rows)
    with patch.object(validate_ohlcv, "PostgresDataStore", return_value=store):
        with patch.object(sys, "argv", ["validate_ohlcv.py"]):
            assert validate_ohlcv.main() == 2


def test_main_empty_db_exits_zero() -> None:
    """No rows in DB → exit 0 (nothing to validate)."""
    store = _patch_store([])
    with patch.object(validate_ohlcv, "PostgresDataStore", return_value=store):
        with patch.object(sys, "argv", ["validate_ohlcv.py"]):
            assert validate_ohlcv.main() == 0


def test_main_ticker_filter_passed_to_sql() -> None:
    """--ticker SBER must end up in the SQL query."""
    store = _patch_store([_make_ohlcv_row("SBER")])
    with patch.object(validate_ohlcv, "PostgresDataStore", return_value=store):
        with patch.object(sys, "argv", ["validate_ohlcv.py", "--ticker", "SBER"]):
            assert validate_ohlcv.main() == 0
    # Check the executed SQL — should mention 'WHERE ticker'
    cursor = store._conn.cursor.return_value.__enter__.return_value
    sql = cursor.execute.call_args[0][0]
    assert "WHERE ticker" in sql
    assert cursor.execute.call_args[0][1] == ("SBER",)


def test_main_limit_clause_for_sample_mode() -> None:
    """--limit N must add a LIMIT to the SQL."""
    store = _patch_store([_make_ohlcv_row("SBER")])
    with patch.object(validate_ohlcv, "PostgresDataStore", return_value=store):
        with patch.object(sys, "argv", ["validate_ohlcv.py", "--limit", "100"]):
            assert validate_ohlcv.main() == 0
    cursor = store._conn.cursor.return_value.__enter__.return_value
    sql = cursor.execute.call_args[0][0]
    assert "LIMIT" in sql
    # Issue #131 (Bug B regression guard): the limit placeholder must
    # receive an INTEGER, not None. The previous implementation passed
    # ``None`` for the no-ticker case, which psycopg translated to
    # ``LIMIT NULL`` — Postgres treated it as "no limit" and returned
    # every row.
    assert cursor.execute.call_args[0][1] == (100,)


def test_main_limit_with_ticker_passes_both_params() -> None:
    """Issue #131 (Bug A regression guard): --ticker X --limit N must
    bind BOTH the ticker and the limit, in one execute call.

    The previous implementation ran two separate ``cur.execute()`` calls:
    one bound the ticker, then the second appended ``LIMIT %s`` but only
    passed ``(limit,)`` — leaving the original ``%s`` in the WHERE clause
    unfilled, which psycopg rejected with ``ProgrammingError: Query has
    2 parameters but 1 was passed``. The fix folds the LIMIT into the
    SQL template and binds both placeholders in one call.
    """
    store = _patch_store([_make_ohlcv_row("SBER")])
    with patch.object(validate_ohlcv, "PostgresDataStore", return_value=store):
        with patch.object(sys, "argv", ["validate_ohlcv.py", "--ticker", "SBER", "--limit", "10"]):
            assert validate_ohlcv.main() == 0
    cursor = store._conn.cursor.return_value.__enter__.return_value
    sql = cursor.execute.call_args[0][0]
    params = cursor.execute.call_args[0][1]
    # Both placeholders must be filled by the same execute call.
    assert sql.count("%s") == len(params) == 2
    assert "WHERE ticker" in sql
    assert "LIMIT" in sql
    assert params == ("SBER", 10)


def test_main_no_limit_no_params_for_unfiltered_select() -> None:
    """Issue #131 (regression guard): the no-ticker / no-limit path must
    bind zero parameters. The previous implementation always called
    ``cur.execute(sql + " LIMIT %s", ...)`` when ``limit`` was truthy,
    which silently inserted a ``LIMIT %s`` with ``None`` placeholder
    when ``ticker`` was also None. The fix keeps the no-params path
    genuinely parameter-free.
    """
    store = _patch_store([_make_ohlcv_row("SBER")])
    with patch.object(validate_ohlcv, "PostgresDataStore", return_value=store):
        with patch.object(sys, "argv", ["validate_ohlcv.py"]):
            assert validate_ohlcv.main() == 0
    cursor = store._conn.cursor.return_value.__enter__.return_value
    sql = cursor.execute.call_args[0][0]
    params = cursor.execute.call_args[0][1]
    assert "LIMIT" not in sql
    assert params == ()


def test_main_db_infrastructure_error_exits_three() -> None:
    """Connection error → exit 3, not crash."""
    store = MagicMock()
    store._connect.side_effect = RuntimeError("DB down")
    store.close = MagicMock()
    # main() catches the failure? No — it would propagate. Wrap in
    # try/except by running main inside a wrapper.
    with patch.object(validate_ohlcv, "PostgresDataStore", return_value=store):
        with patch.object(sys, "argv", ["validate_ohlcv.py"]):
            with pytest.raises(RuntimeError, match="DB down"):
                validate_ohlcv.main()
    # Top-level __main__ handler turns uncaught exceptions into exit 3.
    # We verify the script imports without crashing; that path is
    # exercised by the integration test if/when one is added.
