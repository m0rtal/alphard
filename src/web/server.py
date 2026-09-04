"""HTTP endpoints for alphard-web.

Issues #393, #396, #398. Uses Python stdlib ``http.server`` to avoid
pulling in FastAPI/starlette/uvicorn as a new dependency. The
dashboard renders single-page and polls these endpoints every 30s, so
the request rate is low and async overhead is unnecessary.

If the operator later wants async/streaming endpoints, replace
``HttpHandler`` with a starlette/quart handler — the ``dispatch()``
function in this module is the testable seam and stays unchanged.

Layering (per SOUL.md):
- This module is the HTTP layer. It imports from ``pg_store`` only
  for the psycopg connection helper. It does NOT call into the loader
  chain, supervisor, or coordinator.

Issue #406/#411 — auth gate: every /api/* endpoint (except /api/health)
requires ``Authorization: Bearer <ALPHA...N>`` when ``ALPHARD_WEB_TOKEN``
is set in the environment. The HTML root path ``/`` is intentionally
auth-open so the JS client can render a token-prompt before fetching
data — the same model as Grafana's login page (public page, gated
data). If ``ALPHARD_WEB_TOKEN`` is unset the gate fails open so local
dev still works; the compose file MUST inject the env in production
(see ``tests/test_compose_structure.py``).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.web.queries import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_SPARKLINE_DAYS,
    DEFAULT_SPARKLINE_MAX,
    DEFAULT_SPARKLINE_MIN,
    build_backfill_summary_query,
    build_events_query,
    build_macro_latest_query,
    build_settings_payload,
    build_sparkline_bars_query,
    build_sparkline_tickers_query,
    build_summary_query,
    build_ticker_detail_query,
    build_ticker_recent_bars_query,
    build_tickers_count_query,
    build_tickers_list_query,
    list_backup_payloads,
    summary_row_to_payload,
)

# --- Query executor ----------------------------------------------------
# The real one uses psycopg; tests inject a stub.

#: Regex to extract the ticker symbol from ``/api/ticker/<symbol>`` URLs.
#: Symbols may include letters, digits, dashes, dots (BRENT, SBER, GAZP,
#: etc.) but never a slash. The dispatch() function strips query strings
#: before matching, so a trailing '?...' never reaches the capture group.
_TICKER_PATH_RE: re.Pattern[str] = re.compile(r"^/api/ticker/(?P<ticker>[A-Za-z0-9._-]+)$")

#: Issue #406 — auth gate. Set ``ALPHARD_WEB_TOKEN`` in the env to require
#: ``Authorization: Bearer <token>`` on every protected request. Empty
#: / unset disables the gate (local-dev fail-open). The compose file MUST
#: inject the env in production; ``tests/test_compose_structure.py``
#: pins that contract.
_AUTH_TOKEN_ENV: str = "ALPHARD_WEB_TOKEN"
#: Paths that stay open even when ``_AUTH_TOKEN_ENV`` is set.
#: /api/health is required by the container healthcheck; the LAN-side
#: operator dashboard scrape should not depend on injecting the header.
#: ``/`` is the HTML root — it must be auth-open so the JS client can
#: render a token-prompt before fetching data (issue #411). The Grafana
#: login-page model: public page, gated data behind it.
_AUTH_OPEN_PATHS: frozenset[str] = frozenset({"/api/health", "/"})


def execute_query(dsn: str, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a single-row or multi-row query and return rows as dicts.

    BUGFIX (2026-09-02): `connect_with_timeouts()` was injecting
    `connect_timeout=10` and `options='-c statement_timeout=60000'` as
    connection kwargs. With psycopg3 + scram-sha-256, libpq discarded the
    DSN password before sending the auth packet, producing
    `fe_sendauth: no password supplied` even though
    `os.environ['ALPHARD_PG_DSN']` clearly contained the password.
    The connection timeout is still applied directly; statement timeout is
    applied after connection with `SET statement_timeout` so the DSN is not
    modified.
    """
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 60000")
            cur.execute(sql, params)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall()
    return [dict(zip(cols, row)) for row in rows]


# --- Auth gate (issue #406) ---------------------------------------------
def check_auth(path: str, authorization_header: str | None) -> bool:
    """Return True iff the request is authorised.

    Pure function: takes the URL path and the raw ``Authorization``
    header value (or ``None``), reads the token from the env, and
    returns whether the request should proceed.

    Rules:
    - If ``ALPHARD_WEB_TOKEN`` is unset / empty, fail open (dev mode).
    - If set, every protected path requires ``Authorization: Bearer <token>``.
    - Paths in ``_AUTH_OPEN_PATHS`` (currently just ``/api/health``)
      bypass the gate so container healthchecks keep working.
    - Constant-time string comparison avoids leaking token length via
      timing; ``hmac.compare_digest`` is the stdlib tool for this.
    """
    expected = os.environ.get(_AUTH_TOKEN_ENV, "") or ""
    if not expected:
        # Auth disabled (local dev). Keep the gate here so the rule is
        # obvious in code; the compose file is the production gate.
        return True
    if path in _AUTH_OPEN_PATHS:
        return True
    if not authorization_header:
        return False
    scheme, _, supplied = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied, expected)


# --- Pure dispatch ------------------------------------------------------
# ``dispatch`` is the testable seam: given a URL path + query string +
# DSN + query executor, it returns ``(status, content_type, body)`` and
# is decoupled from the stdlib HTTP plumbing. HttpHandler.do_GET is a
# thin shim that calls dispatch() and serialises the result.


def _load_index_html() -> str:
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _first_query_int(qs: dict[str, list[str]], key: str, default: int, lo: int, hi: int) -> int:
    """Read an int query param with bounds clamping.

    Invalid input falls back to ``default``. Out-of-range input is
    clamped to ``[lo, hi]`` rather than rejected — the dashboard
    tolerates the clamp and there is no useful error to surface for
    a single bad value.
    """
    raw_values = qs.get(key, [])
    if not raw_values:
        return default
    raw = raw_values[0].strip()
    try:
        n = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, n))


def _first_query_str(qs: dict[str, list[str]], key: str) -> str | None:
    """Read a string query param; return ``None`` if absent or empty."""
    raw_values = qs.get(key, [])
    if not raw_values:
        return None
    v = raw_values[0].strip()
    return v or None


def _isoify_row(row: dict[str, Any], iso_keys: tuple[str, ...]) -> dict[str, Any]:
    """Serialise date/datetime values inside a row to ISO strings.

    Mutates ``row`` in place — callers must pass a fresh dict from
    psycopg. No-op for non-date values.
    """
    for key in iso_keys:
        value = row.get(key)
        if value is not None and hasattr(value, "isoformat"):
            row[key] = value.isoformat()
    return row


def dispatch(
    path: str,
    query: str,
    dsn: str,
    executor: Any = execute_query,
    backup_dir: str | None = None,
) -> tuple[int, str, Any]:
    """Pure router. Returns ``(status, content_type, body)``.

    ``executor`` defaults to the real ``execute_query`` but is
    overridable in tests to inject a stub. ``backup_dir`` overrides
    the env-driven default for the ``/api/backups`` endpoint; tests
    pass a tmp path, production leaves it as ``None`` and reads
    ``ALPHARD_BACKUP_DIR``.

    Routing-level misses return 404; DB failures inside ``/api/health``
    map to 503; everything else propagates to the caller (HttpHandler)
    which maps to 500.
    """
    qs = parse_qs(query or "")

    if path == "/api/health":
        try:
            executor(dsn, "SELECT 1", {})
        except Exception:
            return 503, "application/json", {"ok": False, "db": False}
        return 200, "application/json", {"ok": True, "db": True}

    if path == "/api/summary":
        sql, params = build_summary_query()
        rows = executor(dsn, sql, params)
        if not rows:
            return (
                200,
                "application/json",
                summary_row_to_payload(
                    {
                        "universe_size": 0,
                        "backfill_done": 0,
                        "daily_sync_bars": 0,
                        "daily_sync_at": None,
                        "regime": None,
                        "regime_multiplier": None,
                        "cbr_key_rate": None,
                        "usdrub_close": None,
                        "imoex_close": None,
                    }
                ),
            )
        return 200, "application/json", summary_row_to_payload(rows[0])

    if path == "/api/sparkline":
        metric = _first_query_str(qs, "metric") or "tickers"
        days = _first_query_int(qs, "days", DEFAULT_SPARKLINE_DAYS, DEFAULT_SPARKLINE_MIN, DEFAULT_SPARKLINE_MAX)
        if metric == "bars":
            sql, params = build_sparkline_bars_query(days=days)
        else:
            # Default + fallback for unknown metrics. The HTML only ever
            # asks for "tickers" or "bars", but a typo from the operator
            # shouldn't 500 — fall back to tickers.
            sql, params = build_sparkline_tickers_query(days=days)
        rows = executor(dsn, sql, params)
        sparkline_out: list[dict[str, Any]] = []
        for r in rows:
            bucket = r.get("bucket")
            if bucket is not None and hasattr(bucket, "isoformat"):
                r["bucket"] = bucket.isoformat()
            sparkline_out.append(r)
        return 200, "application/json", sparkline_out

    if path == "/api/tickers":
        q = _first_query_str(qs, "q")
        status = _first_query_str(qs, "status")
        limit = _first_query_int(qs, "limit", 50, 1, 500)
        offset = _first_query_int(qs, "offset", 0, 0, 100_000)
        sql, params = build_tickers_list_query(q=q, status=status, limit=limit, offset=offset)
        rows = executor(dsn, sql, params)
        # Each row has 4 timestamps: listed_at, delisted_at, updated_at, last_bar_at.
        tickers_out: list[dict[str, Any]] = []
        for r in rows:
            _isoify_row(r, ("listed_at", "delisted_at", "updated_at", "last_bar_at"))
            tickers_out.append(r)
        return 200, "application/json", tickers_out

    if path == "/api/tickers/count":
        q = _first_query_str(qs, "q")
        status = _first_query_str(qs, "status")
        sql, params = build_tickers_count_query(q=q, status=status)
        rows = executor(dsn, sql, params)
        n = int(rows[0]["count"]) if rows else 0
        return 200, "application/json", {"count": n}

    ticker_match = _TICKER_PATH_RE.match(path)
    if ticker_match is not None:
        ticker = ticker_match.group("ticker")
        detail_sql, detail_params = build_ticker_detail_query(ticker)
        detail_rows = executor(dsn, detail_sql, detail_params)
        if not detail_rows:
            return 404, "application/json", {"error": "ticker not found", "ticker": ticker}
        detail = detail_rows[0]
        _isoify_row(detail, ("listed_at", "delisted_at", "updated_at", "backfill_complete_at"))
        bars_sql, bars_params = build_ticker_recent_bars_query(ticker)
        bars_rows = executor(dsn, bars_sql, bars_params)
        for r in bars_rows:
            _isoify_row(r, ("ts",))
        detail["recent_bars"] = bars_rows
        return 200, "application/json", detail

    if path == "/api/backfill":
        sql, params = build_backfill_summary_query()
        rows = executor(dsn, sql, params)
        row = rows[0] if rows else {}
        # All 8 scalars are integers, current_ticker/figi are strings or None.
        backfill_out: dict[str, Any] = {
            "done": int(row.get("done") or 0),
            "running": int(row.get("running") or 0),
            "pending": int(row.get("pending") or 0),
            "no_data": int(row.get("no_data") or 0),
            "failed": int(row.get("failed") or 0),
            "delisted": int(row.get("delisted") or 0),
            "total": int(row.get("total") or 0),
            "current_ticker": row.get("current_ticker"),
            "current_figi": row.get("current_figi"),
        }
        return 200, "application/json", backfill_out

    if path == "/api/events":
        limit = _first_query_int(qs, "limit", 20, 1, 200)
        sql, params = build_events_query(limit=limit)
        rows = executor(dsn, sql, params)
        events_out: list[dict[str, Any]] = []
        for r in rows:
            _isoify_row(r, ("at",))
            events_out.append(r)
        return 200, "application/json", events_out

    if path == "/api/macro":
        sql, params = build_macro_latest_query()
        rows = executor(dsn, sql, params)
        if not rows:
            return (
                200,
                "application/json",
                {
                    "id": None,
                    "fetched_at": None,
                    "cbr_key_rate": None,
                    "usdrub_close": None,
                    "usdrub_5d_prev": None,
                    "imoex_close": None,
                    "imoex_60d_prev": None,
                    "regime": None,
                    "multiplier": None,
                    "sources": None,
                },
            )
        row = rows[0]
        _isoify_row(row, ("fetched_at",))
        return 200, "application/json", row

    if path == "/api/backups":
        # /api/backups reads from disk, not the DB. No executor call.
        root = backup_dir if backup_dir is not None else os.environ.get("ALPHARD_BACKUP_DIR", DEFAULT_BACKUP_DIR)
        return 200, "application/json", list_backup_payloads(root)

    if path == "/api/settings":
        # /api/settings reads from os.environ only. No DB.
        return 200, "application/json", build_settings_payload()

    if path == "/":
        return 200, "text/html; charset=utf-8", _load_index_html()

    return 404, "application/json", {"error": "not found", "path": path}


# --- HTTP layer ---------------------------------------------------------


class HttpHandler(BaseHTTPRequestHandler):
    """Tiny request handler that delegates to ``dispatch()``.

    Routes:
    - GET /api/health                              -> 200/503 {ok, db}
    - GET /api/summary                             -> 200 {universe_size, ...}
    - GET /api/sparkline?metric=tickers|bars&days=N -> 200 [{bucket, value}]
    - GET /api/tickers?q=&status=&limit=&offset=   -> 200 [...]
    - GET /api/tickers/count?q=&status=            -> 200 {count: N}
    - GET /api/ticker/<symbol>                     -> 200 {...} | 404
    - GET /api/backfill                            -> 200 {done, running, ...}
    - GET /api/events?limit=N                      -> 200 [{kind, at, status, msg}]
    - GET /api/macro                               -> 200 {regime, cbr_key_rate, ...}
    - GET /api/backups                             -> 200 [{file, size, kind, ...}]
    - GET /api/settings                            -> 200 {env, token_set, ...}
    - GET /                                        -> 200 HTML

    The DSN is taken from $ALPHARD_PG_DSN at request time so it
    matches what other alphard services use.
    """

    server_version = "alphard-web/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 — stdlib API
        # BUGFIX (issue #398): PR #392 muted access logs to "one stream
        # per process" but the alphard-web container is NOT the bot's
        # process — its entrypoint is `python3 -m src.web.server`, so
        # the bot's JSON logger never gets a chance to log anything.
        # Restore stderr access logging so `docker logs alphard-web`
        # shows the request, and add a single error line in do_GET
        # for any unhandled exception (see below).
        sys.stderr.write(f"[alphard-web] {self.address_string()} - {fmt % args}\n")
        sys.stderr.flush()

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        # Issue #406 — auth gate runs BEFORE the DSN check so a 401
        # response does not require a Postgres connection. Headers are
        # read via ``self.headers`` (BaseHTTPRequestHandler contract):
        # the case-insensitive ``.get()`` keeps the lookup resilient to
        # client-side header casing (e.g. ``authorization`` vs
        # ``Authorization``).
        url = urlparse(self.path)
        auth_header = self.headers.get("Authorization") if self.headers else None
        if not check_auth(url.path, auth_header):
            sys.stderr.write(
                f"[alphard-web] AUTH-FAIL {self.address_string()} {url.path}: " "missing or wrong bearer token\n"
            )
            sys.stderr.flush()
            self._send_unauthorized()
            return
        dsn = os.environ.get("ALPHARD_PG_DSN")
        if not dsn:
            sys.stderr.write(f"[alphard-web] ERROR {self.address_string()} {url.path}: " "ALPHARD_PG_DSN is not set\n")
            sys.stderr.flush()
            self._send_json(
                {"error": "ALPHARD_PG_DSN is not set", "type": "ConfigError"},
                status=500,
            )
            return
        try:
            status, content_type, body = dispatch(url.path, url.query, dsn)
        except Exception as exc:
            # BUGFIX (issue #398): also log here, not just in log_message.
            # Unhandled exceptions inside dispatch() never reach
            # log_message (stdlib sends its own format line on success
            # only), so an uncaught exception in a query builder would
            # be silent in `docker logs`. Mirror the error here.
            sys.stderr.write(
                f"[alphard-web] ERROR {self.address_string()} {url.path}: " f"{type(exc).__name__}: {exc}\n"
            )
            sys.stderr.flush()
            self._send_json({"error": str(exc), "type": type(exc).__name__}, status=500)
            return
        self._respond(status, content_type, body)

    # ---- helpers -----------------------------------------------------

    def _respond(self, status: int, content_type: str, body: Any) -> None:
        if isinstance(body, (dict, list)):
            raw = json.dumps(body, default=str).encode("utf-8")
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

    def _send_unauthorized(self) -> None:
        """Issue #406 — 401 with WWW-Authenticate: Bearer challenge."""
        body = json.dumps({"error": "unauthorized", "type": "AuthError"}).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("WWW-Authenticate", 'Bearer realm="alphard-web"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# --- Server entrypoint --------------------------------------------------


def make_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """Build a configured HTTP server. Caller invokes ``serve_forever()``.

    Issue #406 — default bind is ``127.0.0.1`` (loopback only). The
    previous default of ``0.0.0.0`` exposed the dashboard on every
    interface the container saw. Operators who really need LAN access
    can pass ``ALPHARD_WEB_HOST=0.0.0.0`` and combine it with the
    bearer-token gate; the compose file pins 127.0.0.1 as the safe
    production default.
    """
    bind_host = os.environ.get("ALPHARD_WEB_HOST", host)
    return ThreadingHTTPServer((bind_host, port), HttpHandler)


if __name__ == "__main__":  # pragma: no cover — exercised in pre_pr_smoke
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = make_server(port=port)
    server.serve_forever()
