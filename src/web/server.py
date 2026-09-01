"""HTTP endpoints for alphard-web.

Issue #390. Uses Python stdlib ``http.server`` to avoid pulling in
FastAPI/starlette/uvicorn as a new dependency. The dashboard renders
single-page and polls these endpoints every 30s, so the request rate
is low and async overhead is unnecessary.

If the operator later wants async/streaming endpoints, replace
``HttpHandler`` with a starlette/quart handler — the ``dispatch()``
function in this module is the testable seam and stays unchanged.

Layering (per SOUL.md):
- This module is the HTTP layer. It imports from ``pg_store`` only
  for the psycopg connection helper. It does NOT call into the loader
  chain, supervisor, or coordinator.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from src.data.pg_store import connect_with_timeouts
from src.web.app import (
    DEFAULT_TIMESERIES_DAYS,
    build_kpis_query,
    build_ohlcv_timeseries_query,
    build_top_tickers_query,
    health_payload,
    kpis_row_to_payload,
)

# --- Query executors ----------------------------------------------------
# These do the actual DB I/O. Kept separate from ``app.py`` so the
# pure query-builder functions stay unit-testable without psycopg.


def execute_query(dsn: str, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a single-row or multi-row query and return rows as dicts."""
    with connect_with_timeouts(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall()
    return [dict(zip(cols, row)) for row in rows]


# --- Pure dispatch ------------------------------------------------------
# ``dispatch`` is the testable seam: given a URL path + query string +
# DSN + query executor, it returns ``(status, content_type, body)`` and
# is decoupled from the stdlib HTTP plumbing. HttpHandler.do_GET is a
# thin shim that calls dispatch() and serialises the result.


def _load_index_html() -> str:
    import os

    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _serialise_timeseries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert psycopg ``date`` buckets to ISO strings for JSON."""
    for row in rows:
        bucket = row.get("bucket")
        if bucket is not None and hasattr(bucket, "isoformat"):
            row["bucket"] = bucket.isoformat()
    return rows


def dispatch(
    path: str,
    query: str,
    dsn: str,
    executor: Any = execute_query,
) -> tuple[int, str, Any]:
    """Pure router. Returns ``(status, content_type, body)``.

    ``executor`` defaults to the real ``execute_query`` but is overridable
    in tests to inject a stub. Routing-level misses return 404; DB
    failures inside ``/api/health`` map to 503; everything else propagates
    to the caller (HttpHandler) which maps to 500.
    """
    from urllib.parse import parse_qs

    if path == "/api/health":
        try:
            executor(dsn, "SELECT 1", {})
            return 200, "application/json", health_payload(db_ok=True)
        except Exception:
            return 503, "application/json", health_payload(db_ok=False)

    if path == "/api/kpis":
        sql, params = build_kpis_query()
        rows = executor(dsn, sql, params)
        if not rows:
            return 500, "application/json", {"error": "kpis query returned no rows"}
        return 200, "application/json", kpis_row_to_payload(rows[0])

    if path == "/api/ohlcv_timeseries":
        qs = parse_qs(query or "")
        raw = (qs.get("days", [str(DEFAULT_TIMESERIES_DAYS)])[0]).strip()
        try:
            days = int(raw)
        except ValueError:
            days = DEFAULT_TIMESERIES_DAYS
        days = max(1, min(365, days))
        sql, params = build_ohlcv_timeseries_query(days=days)
        rows = executor(dsn, sql, params)
        return 200, "application/json", _serialise_timeseries(rows)

    if path == "/api/top_tickers":
        qs = parse_qs(query or "")
        raw = (qs.get("limit", ["10"])[0]).strip()
        try:
            limit = int(raw)
        except ValueError:
            limit = 10
        limit = max(1, min(100, limit))
        sql, params = build_top_tickers_query(limit=limit)
        rows = executor(dsn, sql, params)
        return 200, "application/json", rows

    if path == "/":
        return 200, "text/html; charset=utf-8", _load_index_html()

    return 404, "application/json", {"error": "not found", "path": path}


# --- HTTP layer ----------------------------------------------------------


class HttpHandler(BaseHTTPRequestHandler):
    """Tiny request handler that delegates to ``dispatch()``.

    Routes:
    - GET /api/health                          -> 200/503 {ok, db}
    - GET /api/kpis                            -> 200 {universe_size, ...}
    - GET /api/ohlcv_timeseries[?days=30]      -> 200 [{bucket, ...}]
    - GET /api/top_tickers[?limit=10]          -> 200 [{ticker, bar_count}]
    - GET /                                    -> 200 HTML

    The DSN is taken from $ALPHARD_PG_DSN at request time so it
    matches what other alphard services use.
    """

    server_version = "alphard-web/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 — stdlib API
        # Quiet the default stderr access log. The bot already has
        # stdout JSON logging; one stream per process keeps grep simple.
        return

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        url = urlparse(self.path)
        dsn = os.environ.get("ALPHARD_PG_DSN")
        if not dsn:
            self._send_json(
                {"error": "ALPHARD_PG_DSN is not set", "type": "ConfigError"},
                status=500,
            )
            return
        try:
            status, content_type, body = dispatch(url.path, url.query, dsn)
        except Exception as exc:
            self._send_json({"error": str(exc), "type": type(exc).__name__}, status=500)
            return
        self._respond(status, content_type, body)

    # ---- helpers -----------------------------------------------------

    def _respond(self, status: int, content_type: str, body: Any) -> None:
        if isinstance(body, (dict, list)):
            raw = json.dumps(body, default=str).encode("utf-8")
            content_type = content_type  # already application/json
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = str(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if content_type.startswith("application/json"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        self._respond(status, "application/json; charset=utf-8", payload)


# --- Server entrypoint --------------------------------------------------


def make_server(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:  # noqa: S104 — internal-only
    """Build a configured HTTP server. Caller invokes ``serve_forever()``."""
    return ThreadingHTTPServer((host, port), HttpHandler)


if __name__ == "__main__":  # pragma: no cover — exercised in pre_pr_smoke
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = make_server(port=port)
    server.serve_forever()
