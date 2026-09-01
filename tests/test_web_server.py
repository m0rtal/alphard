"""TDD tests for alphard-web v2 dispatch (issue #396 wire-up).

Covers every route the v2 dashboard calls:
- /api/health, /api/summary, /api/sparkline
- /api/tickers, /api/tickers/count, /api/ticker/<symbol>
- /api/backfill, /api/events, /api/macro, /api/backups, /api/settings
- / (HTML)
- 404 fallback

Uses a stub ``executor`` so tests do not need a live Postgres. The
``/api/backups`` and ``/api/settings`` routes don't touch the DB, so
their tests are fully self-contained.

Issue #398 logging regression is tested at the bottom — the no-op
override from PR #392 is replaced with stderr output.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from src.web import server as server_mod
from src.web.server import HttpHandler, dispatch

# --- Test helpers -------------------------------------------------------


class _FakeExecutor:
    """Captures (sql, params) calls and returns scripted rows.

    Routes set ``self.next`` to a list-of-dicts that ``__call__``
    returns. Default is an empty list.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next: list[dict[str, Any]] = []

    def __call__(self, dsn: str, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return self.next


def _ok_executor(rows: list[dict[str, Any]] | None = None) -> _FakeExecutor:
    """Return a FakeExecutor primed with ``rows`` (default: empty)."""
    ex = _FakeExecutor()
    ex.next = rows if rows is not None else []
    return ex


class _StubHandler(HttpHandler):
    """Minimal HttpHandler that captures writes/headers without sockets.

    Skips BaseHTTPRequestHandler.__init__ which expects a real socket.
    Mirrors the stdlib contract enough for ``do_GET`` and
    ``log_message`` to run without errors.
    """

    def __init__(self, path: str = "/api/health") -> None:
        self._captured: list[bytes] = []
        self._headers: dict[str, str] = {}

        class _W:
            def __init__(self, outer: _StubHandler) -> None:
                self._outer = outer

            def write(self, data: bytes) -> None:
                self._outer._captured.append(data)

        self.wfile = _W(self)  # type: ignore[assignment]
        self.headers = {}
        self.command = "GET"
        self.path = path
        self.request_version = "HTTP/1.1"
        self.server_version = "alphard-web/0.2"
        self.sys_version = ""
        self.client_address = ("127.0.0.1", 12345)
        self.response_code: int = 0

    def send_response(self, code: int) -> None:
        self.response_code = code

    def send_header(self, key: str, value: str) -> None:
        self._headers[key] = value

    def end_headers(self) -> None:
        pass

    def address_string(self) -> str:
        return "127.0.0.1"


# --- /api/health --------------------------------------------------------


def test_health_ok_returns_200_with_db_true() -> None:
    ex = _ok_executor()
    status, ctype, body = dispatch("/api/health", "", "postgresql://x", executor=ex)
    assert status == 200
    assert ctype == "application/json"
    assert body == {"ok": True, "db": True}


def test_health_failure_returns_503() -> None:
    def boom(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    status, _ctype, body = dispatch("/api/health", "", "postgresql://x", executor=boom)
    assert status == 503
    assert body == {"ok": False, "db": False}


# --- /api/summary -------------------------------------------------------


def test_summary_returns_payload_shape() -> None:
    ex = _ok_executor(
        [
            {
                "universe_size": 100,
                "backfill_done": 50,
                "daily_sync_bars": 12345,
                "daily_sync_at": datetime(2026, 9, 1, 20, 0, 0),
                "regime": "neutral",
                "regime_multiplier": 1.0,
                "cbr_key_rate": 7.5,
                "usdrub_close": 92.5,
                "imoex_close": 3100.0,
            }
        ]
    )
    status, ctype, body = dispatch("/api/summary", "", "postgresql://x", executor=ex)
    assert status == 200
    assert ctype == "application/json"
    assert body["universe_size"] == 100
    assert body["backfill_pct"] == 50.0
    assert body["regime"] == "neutral"
    assert body["daily_sync_at"] == "2026-09-01T20:00:00"


def test_summary_empty_db_returns_zero_payload() -> None:
    """No rows from DB should still produce a valid payload, not a 500."""
    ex = _ok_executor([])
    status, _ctype, body = dispatch("/api/summary", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body["universe_size"] == 0
    assert body["backfill_pct"] == 0.0


# --- /api/sparkline -----------------------------------------------------


def test_sparkline_tickers_passes_days_param() -> None:
    ex = _ok_executor([{"bucket": date(2026, 9, 1), "value": 5}])
    status, _ctype, body = dispatch("/api/sparkline", "metric=tickers&days=14", "postgresql://x", executor=ex)
    assert status == 200
    assert body == [{"bucket": "2026-09-01", "value": 5}]
    assert ex.calls[0][1] == {"days": 14}


def test_sparkline_bars_metric_passes_days_param() -> None:
    ex = _ok_executor([])
    dispatch("/api/sparkline", "metric=bars&days=30", "postgresql://x", executor=ex)
    assert ex.calls[0][1] == {"days": 30}


def test_sparkline_defaults_when_no_query() -> None:
    ex = _ok_executor([])
    dispatch("/api/sparkline", "", "postgresql://x", executor=ex)
    assert ex.calls[0][1] == {"days": 7}  # DEFAULT_SPARKLINE_DAYS


def test_sparkline_clamps_days_out_of_range() -> None:
    """days=999 should clamp to DEFAULT_SPARKLINE_MAX=90, not 500."""
    ex = _ok_executor([])
    dispatch("/api/sparkline", "days=999", "postgresql://x", executor=ex)
    assert ex.calls[0][1] == {"days": 90}


def test_sparkline_unknown_metric_falls_back_to_tickers() -> None:
    """Typo from operator shouldn't 500. Fall back to tickers query."""
    ex = _ok_executor([])
    dispatch("/api/sparkline", "metric=bogus", "postgresql://x", executor=ex)
    # The tickers query contains "backfill_complete", the bars query doesn't.
    assert "backfill_complete" in ex.calls[0][0]


# --- /api/tickers (paginated list) --------------------------------------


def test_tickers_list_passes_filters_and_pagination() -> None:
    ex = _ok_executor(
        [
            {
                "ticker": "SBER",
                "figi": "BBG004730N88",
                "listed_at": date(2014, 1, 1),
                "delisted_at": None,
                "backfill_complete": True,
                "updated_at": datetime(2026, 9, 1),
                "bar_count": 2500,
                "last_bar_at": date(2026, 9, 1),
            }
        ]
    )
    status, _ctype, body = dispatch(
        "/api/tickers",
        "q=SBER&status=done&limit=25&offset=50",
        "postgresql://x",
        executor=ex,
    )
    assert status == 200
    assert body[0]["ticker"] == "SBER"
    assert body[0]["listed_at"] == "2014-01-01"
    assert body[0]["last_bar_at"] == "2026-09-01"
    params = ex.calls[0][1]
    assert params["q"] == "%SBER%"
    assert params["limit"] == 25
    assert params["offset"] == 50


def test_tickers_list_clamps_limit_above_500() -> None:
    ex = _ok_executor([])
    dispatch("/api/tickers", "limit=99999", "postgresql://x", executor=ex)
    assert ex.calls[0][1]["limit"] == 500


# --- /api/tickers/count -------------------------------------------------


def test_tickers_count_returns_count_field() -> None:
    ex = _ok_executor([{"count": 1234}])
    status, _ctype, body = dispatch("/api/tickers/count", "status=done", "postgresql://x", executor=ex)
    assert status == 200
    assert body == {"count": 1234}


def test_tickers_count_handles_empty_rows() -> None:
    ex = _ok_executor([])
    status, _ctype, body = dispatch("/api/tickers/count", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body == {"count": 0}


# --- /api/ticker/<symbol> -----------------------------------------------


def test_ticker_detail_returns_404_when_missing() -> None:
    ex = _ok_executor([])
    status, _ctype, body = dispatch("/api/ticker/NOPE", "", "postgresql://x", executor=ex)
    assert status == 404
    assert body == {"error": "ticker not found", "ticker": "NOPE"}


def test_ticker_detail_returns_kv_and_recent_bars() -> None:
    detail_row = {
        "ticker": "SBER",
        "figi": "BBG004730N88",
        "listed_at": date(2014, 1, 1),
        "delisted_at": None,
        "backfill_complete": True,
        "backfill_complete_at": datetime(2024, 6, 1),
        "updated_at": datetime(2026, 9, 1),
        "bar_count": 2500,
    }
    # executor returns the same row for both calls (detail + recent_bars).
    # That's fine — we only check the dispatch surface, not the actual
    # SQL semantics.
    ex = _ok_executor([detail_row, detail_row])
    status, _ctype, body = dispatch("/api/ticker/SBER", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body["ticker"] == "SBER"
    assert body["bar_count"] == 2500
    assert body["listed_at"] == "2014-01-01"
    assert body["backfill_complete_at"] == "2024-06-01T00:00:00"
    # Two SQL calls: detail + recent_bars.
    assert len(ex.calls) == 2


def test_ticker_detail_supports_dashed_and_dotted_symbols() -> None:
    detail_row = {
        "ticker": "BRENT.V1",
        "figi": "BBG000B9Z9G4",
        "listed_at": None,
        "delisted_at": None,
        "backfill_complete": False,
        "backfill_complete_at": None,
        "updated_at": None,
        "bar_count": 0,
    }
    ex = _ok_executor([detail_row, detail_row])
    status, _ctype, body = dispatch("/api/ticker/BRENT.V1", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body["ticker"] == "BRENT.V1"


def test_ticker_detail_404_for_empty_segment() -> None:
    """A trailing slash leaves an empty ticker; regex requires ≥1 char."""
    ex = _ok_executor([])
    status, _ctype, body = dispatch("/api/ticker/", "", "postgresql://x", executor=ex)
    assert status == 404
    assert body["path"] == "/api/ticker/"


# --- /api/backfill ------------------------------------------------------


def test_backfill_returns_zero_payload_when_no_rows() -> None:
    ex = _ok_executor([])
    status, ctype, body = dispatch("/api/backfill", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body == {
        "done": 0,
        "running": 0,
        "pending": 0,
        "no_data": 0,
        "failed": 0,
        "delisted": 0,
        "total": 0,
        "current_ticker": None,
        "current_figi": None,
    }


def test_backfill_returns_full_payload() -> None:
    ex = _ok_executor(
        [
            {
                "done": 100,
                "running": 5,
                "pending": 20,
                "no_data": 3,
                "failed": 2,
                "delisted": 50,
                "total": 180,
                "current_ticker": "GAZP",
                "current_figi": "BBG004730RP0",
            }
        ]
    )
    status, _ctype, body = dispatch("/api/backfill", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body["total"] == 180
    assert body["current_ticker"] == "GAZP"


# --- /api/events --------------------------------------------------------


def test_events_iso_serialises_at_column() -> None:
    ex = _ok_executor(
        [
            {
                "kind": "daily_sync",
                "at": datetime(2026, 9, 1, 20, 0, 0),
                "status": "ok",
                "msg": "1234 bars updated",
            }
        ]
    )
    status, _ctype, body = dispatch("/api/events", "limit=5", "postgresql://x", executor=ex)
    assert status == 200
    assert body[0]["at"] == "2026-09-01T20:00:00"
    assert ex.calls[0][1] == {"limit": 5}


# --- /api/macro ---------------------------------------------------------


def test_macro_returns_empty_payload_when_no_rows() -> None:
    ex = _ok_executor([])
    status, _ctype, body = dispatch("/api/macro", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body["regime"] is None
    assert body["cbr_key_rate"] is None


def test_macro_serialises_fetched_at() -> None:
    ex = _ok_executor(
        [
            {
                "id": 42,
                "fetched_at": datetime(2026, 9, 1, 20, 0, 0),
                "cbr_key_rate": 7.5,
                "usdrub_close": 92.5,
                "usdrub_5d_prev": 90.0,
                "imoex_close": 3100.0,
                "imoex_60d_prev": 3000.0,
                "regime": "neutral",
                "multiplier": 1.0,
                "sources": "cbr,moex",
            }
        ]
    )
    status, _ctype, body = dispatch("/api/macro", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body["id"] == 42
    assert body["fetched_at"] == "2026-09-01T20:00:00"
    assert body["regime"] == "neutral"


# --- /api/backups (filesystem) ------------------------------------------


def test_backups_returns_empty_list_when_dir_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    status, _ctype, body = dispatch(
        "/api/backups", "", "postgresql://x", executor=_ok_executor(), backup_dir=str(missing)
    )
    assert status == 200
    assert body == []


def test_backups_returns_metadata_with_retention_kind(tmp_path: Path) -> None:
    # 7 daily + 1 weekly-style older backup.
    for day in [25, 26, 27, 28, 29, 30, 31]:
        (tmp_path / f"alphard_2026-08-{day:02d}_120000.sql.gz").write_bytes(b"x" * 100)
    (tmp_path / "alphard_2026-09-01_120000.sql.gz").write_bytes(b"x" * 800)
    (tmp_path / "not_a_backup.txt").write_bytes(b"x")

    status, _ctype, body = dispatch(
        "/api/backups", "", "postgresql://x", executor=_ok_executor(), backup_dir=str(tmp_path)
    )
    assert status == 200
    assert len(body) == 8
    # The non-matching file is filtered out.
    assert all("not_a_backup" not in row["file"] for row in body)
    # Newest first.
    assert body[0]["file"] == "alphard_2026-09-01_120000.sql.gz"
    # Retention kind assigned.
    daily_kinds = [r["kind"] for r in body if r["kind"] == "daily"]
    assert len(daily_kinds) == 7
    # The oldest is "weekly" (the only non-daily in this set).
    assert body[-1]["kind"] == "weekly"


def test_backups_reads_default_dir_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHARD_BACKUP_DIR", str(tmp_path))
    (tmp_path / "alphard_2026-09-01_120000.sql.gz").write_bytes(b"x" * 100)

    status, _ctype, body = dispatch("/api/backups", "", "postgresql://x", executor=_ok_executor())
    assert status == 200
    assert len(body) == 1


# --- /api/settings ------------------------------------------------------


def test_settings_reads_env_only_no_executor_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPHARD_ENV", "sandbox")
    monkeypatch.setenv("TINKOFF_INVEST_TOKEN", "test-token")
    monkeypatch.setenv("ALPHARD_LOOP_HEARTBEAT", "1")
    monkeypatch.setenv("ALPHARD_LOOP_DAILY_SYNC", "1")

    ex = _ok_executor()
    status, ctype, body = dispatch("/api/settings", "", "postgresql://x", executor=ex)
    assert status == 200
    assert body["env"] == "sandbox"
    assert body["token_set"] is True
    assert body["loops"]["heartbeat"] is True
    assert body["loops"]["daily_sync"] is True
    # Unset loops default to False (flag() rejects "0", "" etc).
    assert body["loops"]["backup"] is False
    # No DB call.
    assert ex.calls == []


def test_settings_token_absent_reports_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TINKOFF_INVEST_TOKEN", raising=False)
    ex = _ok_executor()
    _status, _ctype, body = dispatch("/api/settings", "", "postgresql://x", executor=ex)
    assert body["token_set"] is False


# --- / (HTML) -----------------------------------------------------------


def test_index_returns_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """The / route should serve the static HTML.

    We monkey-patch _load_index_html so the dispatch path is exercised
    without touching the real file.
    """
    monkeypatch.setattr(server_mod, "_load_index_html", lambda: "<html>alphard-web</html>")
    status, ctype, body = dispatch("/", "", "postgresql://x", executor=_ok_executor())
    assert status == 200
    assert "text/html" in ctype
    assert body == "<html>alphard-web</html>"


# --- 404 fallback -------------------------------------------------------


def test_unknown_path_returns_404_json() -> None:
    status, ctype, body = dispatch("/api/whatever", "", "postgresql://x", executor=_ok_executor())
    assert status == 404
    assert body == {"error": "not found", "path": "/api/whatever"}


# --- HttpHandler integration --------------------------------------------


def test_http_handler_returns_200_on_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke-test the HttpHandler wrapper path with a stub executor."""
    ex = _ok_executor()

    original_dispatch = server_mod.dispatch

    def patched(
        path: str,
        query: str,
        dsn: str,
        executor: Any = server_mod.execute_query,
        **kw: Any,
    ) -> tuple[int, str, Any]:
        return original_dispatch(path, query, dsn, executor=ex, **kw)

    monkeypatch.setattr(server_mod, "dispatch", patched)
    monkeypatch.setenv("ALPHARD_PG_DSN", "postgresql://x")

    h = _StubHandler(path="/api/health")
    h.do_GET()
    assert h.response_code == 200
    assert any(b'"ok": true' in chunk for chunk in h._captured)


def test_http_handler_returns_500_when_dsn_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPHARD_PG_DSN", raising=False)
    h = _StubHandler(path="/api/summary")
    h.do_GET()
    assert h.response_code == 500
    assert any(b"ALPHARD_PG_DSN" in chunk for chunk in h._captured)


def test_http_handler_logs_unhandled_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BUGFIX (issue #398): unhandled exceptions must surface in stderr."""

    def boom(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("simulated db failure")

    original_dispatch = server_mod.dispatch

    def patched(
        path: str,
        query: str,
        dsn: str,
        executor: Any = server_mod.execute_query,
        **kw: Any,
    ) -> tuple[int, str, Any]:
        return original_dispatch(path, query, dsn, executor=boom, **kw)

    monkeypatch.setattr(server_mod, "dispatch", patched)
    monkeypatch.setenv("ALPHARD_PG_DSN", "postgresql://x")

    h = _StubHandler(path="/api/summary")
    h.do_GET()
    assert h.response_code == 500
    captured = capsys.readouterr()
    assert "[alphard-web] ERROR" in captured.err
    assert "simulated db failure" in captured.err


# --- logging: BUGFIX issue #398 ----------------------------------------


def test_log_message_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """BUGFIX (issue #398): PR #392 made log_message a no-op. Restore stderr."""
    h = _StubHandler()
    h.log_message("test %s", "hello")
    captured = capsys.readouterr()
    assert "[alphard-web]" in captured.err
    assert "test hello" in captured.err


# --- auth gate (issue #406) --------------------------------------------
#
# Regression for Security: High — PR #394 dashboard had no authentication.
# /api/settings, /api/backups, /api/tickers, /api/ticker/<sym>, /api/backfill,
# /api/events, /api/macro, /api/summary, /api/sparkline, /api/tickers/count
# all returned sensitive data (DSN-derived values, backup paths, full
# universe) without any check.
#
# Fix: dispatch() requires an ALPHARD_WEB_TOKEN env var to be set; an
# Authorization: Bearer header carrying the same value is required for
# every /api/* route. /api/health remains open so container healthchecks
# keep working. / (HTML root) is gated too so the page itself is not
# reachable from the LAN without the token.


# Paths that DO NOT require auth (must stay open for the orchestrator).
_OPEN_PATHS: frozenset[str] = frozenset({"/api/health"})


def _auth_dispatch(
    path: str,
    headers: dict[str, str] | None = None,
    *,
    monkeypatch: pytest.MonkeyPatch,
    executor: _FakeExecutor | None = None,
) -> tuple[int, str, Any]:
    """Call HttpHandler.do_GET with optional auth headers.

    Patches dispatch's executor to a stub and forces ALPHARD_PG_DSN so
    the handler reaches the router. The Authorization header is read
    by the handler from self.headers (BaseHTTPRequestHandler contract).
    """
    ex = executor or _ok_executor()

    original_dispatch = server_mod.dispatch

    def patched(
        path_arg: str,
        query: str,
        dsn: str,
        executor: Any = server_mod.execute_query,
        **kw: Any,
    ) -> tuple[int, str, Any]:
        return original_dispatch(path_arg, query, dsn, executor=ex, **kw)

    monkeypatch.setattr(server_mod, "dispatch", patched)
    monkeypatch.setenv("ALPHARD_PG_DSN", "postgresql://x")
    if headers is not None:
        for k, v in headers.items():
            monkeypatch.setenv(k, v)

    h = _StubHandler(path=path)
    h.headers = dict(headers) if headers else {}
    h.do_GET()
    return h.response_code, h._headers.get("Content-Type", ""), h._captured


def test_auth_required_when_token_env_set_no_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: ALPHARD_WEB_TOKEN set but no Authorization header → 401."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    code, _ctype, _body = _auth_dispatch("/api/summary", monkeypatch=monkeypatch)
    assert code == 401


def test_auth_required_when_token_env_set_wrong_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: bearer token mismatch → 401."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    code, _ctype, _body = _auth_dispatch(
        "/api/summary",
        headers={"Authorization": "Bearer wrong"},
        monkeypatch=monkeypatch,
    )
    assert code == 401


def test_auth_passes_with_matching_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: matching bearer token → 200."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    code, _ctype, _body = _auth_dispatch(
        "/api/summary",
        headers={"Authorization": "Bearer secret-token-abc"},
        monkeypatch=monkeypatch,
    )
    assert code == 200


def test_auth_disabled_when_token_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: ALPHARD_WEB_TOKEN unset → auth gate disabled (legacy/dev).

    This is the fail-open fallback for local dev where the operator has
    not yet set a token. In production, the compose file MUST inject
    ALPHARD_WEB_TOKEN; the compose-structure test pins that contract.
    """
    monkeypatch.delenv("ALPHARD_WEB_TOKEN", raising=False)
    code, _ctype, _body = _auth_dispatch("/api/summary", monkeypatch=monkeypatch)
    assert code == 200


def test_auth_open_paths_skip_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: /api/health stays open for container healthchecks."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    # No Authorization header — would normally 401, but health is open.
    code, _ctype, _body = _auth_dispatch("/api/health", monkeypatch=monkeypatch)
    assert code == 200


def test_auth_gate_404_also_requires_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: unknown paths don't leak routing info without auth."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    code, _ctype, _body = _auth_dispatch(
        "/api/does-not-exist",
        monkeypatch=monkeypatch,
    )
    assert code == 401


def test_auth_challenge_includes_www_authenticate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: 401 response carries WWW-Authenticate so curl/HTTPie can prompt."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    _code, _ctype, _body = _auth_dispatch("/api/summary", monkeypatch=monkeypatch)
    # Use a fresh dispatch via the live handler to inspect headers.
    # _auth_dispatch already invoked do_GET; we need to look at the
    # captured headers from that call by re-running with capture.
    ex = _ok_executor()
    original_dispatch = server_mod.dispatch

    def patched(
        path: str,
        query: str,
        dsn: str,
        executor: Any = server_mod.execute_query,
        **kw: Any,
    ) -> tuple[int, str, Any]:
        return original_dispatch(path, query, dsn, executor=ex, **kw)

    monkeypatch.setattr(server_mod, "dispatch", patched)
    monkeypatch.setenv("ALPHARD_PG_DSN", "postgresql://x")
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    h = _StubHandler(path="/api/summary")
    h.headers = {}
    h.do_GET()
    assert h.response_code == 401
    assert "WWW-Authenticate" in h._headers
    assert "Bearer" in h._headers["WWW-Authenticate"]


# --- check_auth pure-function tests ------------------------------------


def test_check_auth_disabled_when_token_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: ALPHARD_WEB_TOKEN unset → gate is off (dev mode)."""
    monkeypatch.delenv("ALPHARD_WEB_TOKEN", raising=False)
    assert server_mod.check_auth("/api/summary", None) is True
    assert server_mod.check_auth("/api/summary", "Bearer x") is True


def test_check_auth_enabled_requires_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: token set but no/empty header → denied."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    assert server_mod.check_auth("/api/summary", None) is False
    assert server_mod.check_auth("/api/summary", "") is False


def test_check_auth_wrong_scheme_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: Basic auth / non-bearer schemes are denied."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    assert server_mod.check_auth("/api/summary", "Basic dXNlcjpwYXNz") is False
    assert server_mod.check_auth("/api/summary", "secret-token-abc") is False


def test_check_auth_open_paths_skip_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: /api/health bypasses the gate."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    assert server_mod.check_auth("/api/health", None) is True
    assert server_mod.check_auth("/api/health", "Bearer wrong") is True


def test_check_auth_html_root_is_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: HTML root path requires auth (the page itself leaks)."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    assert server_mod.check_auth("/", None) is False
    assert server_mod.check_auth("/", "Bearer secret-token-abc") is True


def test_check_auth_matching_bearer_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #406: matching bearer passes."""
    monkeypatch.setenv("ALPHARD_WEB_TOKEN", "secret-token-abc")
    assert server_mod.check_auth("/api/summary", "Bearer secret-token-abc") is True
