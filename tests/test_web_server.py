"""HTTP-layer tests for alphard-web (issue #390).

Stubs the executor seam so no live Postgres is needed. End-to-end
verification still happens in the smoke gate against alphard-postgres.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from src.web.server import (
    _serialise_timeseries,
    dispatch,
)


def _ok_executor(rows: list[tuple[Any, ...]], cols: list[str]) -> Any:
    """Build a fake executor that returns the supplied rows/cols tuple."""

    def _exe(dsn: str, sql: str, params: Any) -> list[dict[str, Any]]:
        return [dict(zip(cols, r)) for r in rows]

    return _exe


def test_health_ok_when_db_reachable() -> None:
    status, ctype, body = dispatch("/api/health", "", "postgresql://stub", _ok_executor([(1,)], ["ok"]))
    assert status == 200
    assert ctype == "application/json"
    assert body == {"ok": True, "db": True}


def test_health_503_when_db_raises() -> None:
    def _boom(dsn: str, sql: str, params: Any) -> list[dict[str, Any]]:
        raise RuntimeError("connection refused")

    status, ctype, body = dispatch("/api/health", "", "postgresql://stub", _boom)
    assert status == 503
    assert body == {"ok": False, "db": False}


def test_kpis_endpoint() -> None:
    rows = [(3263, 1568, 1_338_311, "2026-09-01")]
    cols = ["universe_size", "tickers_complete", "ohlcv_rows", "last_bar_at"]
    status, _, body = dispatch("/api/kpis", "", "postgresql://stub", _ok_executor(rows, cols))
    assert status == 200
    assert body == {
        "universe_size": 3263,
        "tickers_complete": 1568,
        "ohlcv_rows": 1_338_311,
        "last_bar_at": "2026-09-01",
    }


def test_kpis_empty_rows_returns_500() -> None:
    status, _, body = dispatch("/api/kpis", "", "postgresql://stub", _ok_executor([], []))
    assert status == 500
    assert body["error"] == "kpis query returned no rows"


def test_ohlcv_timeseries_serialises_dates() -> None:
    buckets = [dt.date(2026, 8, 25), dt.date(2026, 8, 26)]
    rows = [(b, 100, 100 + 50 * i) for i, b in enumerate(buckets)]
    cols = ["bucket", "rows_in_bucket", "cum_rows"]
    status, _, body = dispatch("/api/ohlcv_timeseries", "days=30", "postgresql://stub", _ok_executor(rows, cols))
    assert status == 200
    assert body[0]["bucket"] == "2026-08-25"
    assert body[1]["bucket"] == "2026-08-26"
    assert body[1]["cum_rows"] == 150


def test_ohlcv_timeseries_days_param_bounds() -> None:
    # Above hi cap → clamped to 365
    status, _, body = dispatch("/api/ohlcv_timeseries", "days=99999", "postgresql://stub", _ok_executor([], []))
    assert status == 200
    assert body == []

    # Below lo cap → clamped to 1 (still returns empty stub list)
    status, _, body = dispatch("/api/ohlcv_timeseries", "days=0", "postgresql://stub", _ok_executor([], []))
    assert status == 200

    # Garbage → default 30, returns empty list
    status, _, body = dispatch("/api/ohlcv_timeseries", "days=abc", "postgresql://stub", _ok_executor([], []))
    assert status == 200


def test_top_tickers_default_limit() -> None:
    rows = [(f"T{i}", 100 - i) for i in range(10)]
    cols = ["ticker", "bar_count"]
    status, _, body = dispatch("/api/top_tickers", "", "postgresql://stub", _ok_executor(rows, cols))
    assert status == 200
    assert len(body) == 10
    assert body[0]["ticker"] == "T0"


def test_top_tickers_limit_bounds() -> None:
    status, _, body = dispatch("/api/top_tickers", "limit=9999", "postgresql://stub", _ok_executor([], []))
    assert status == 200
    assert body == []


def test_index_serves_html() -> None:
    status, ctype, body = dispatch("/", "", "postgresql://stub", _ok_executor([], []))
    assert status == 200
    assert ctype.startswith("text/html")
    assert "<title>alphard-web</title>" in str(body)


def test_unknown_path_returns_404() -> None:
    status, _, body = dispatch("/api/nope", "", "postgresql://stub", _ok_executor([], []))
    assert status == 404
    assert body["error"] == "not found"


def test_serialise_timeseries_passthrough_for_strings() -> None:
    rows = [{"bucket": "2026-09-01", "cum_rows": 100}]
    out = _serialise_timeseries(list(rows))
    assert out[0]["bucket"] == "2026-09-01"
