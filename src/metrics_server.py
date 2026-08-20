"""Lightweight Prometheus metrics HTTP server for Alphard.

Exposes two endpoints on a single background ThreadingHTTPServer:
- ``GET /health`` returns 200 OK with body ``"ok\n"``. Cheap liveness probe;
  no metric state is touched.
- ``GET /metrics`` returns the Prometheus text exposition format (RFC-style).

The server is intentionally stdlib-only — no ``prometheus_client`` dependency.
Metrics are tracked in a single in-process dict guarded by a lock. Counters
and gauges are typed; values are formatted at exposition time.

Counters exposed
----------------
- ``alphard_heartbeats_total`` — incremented on every heartbeat tick.
- ``alphard_backfill_total{result="ok|skip|error|delisted"}`` — backfill
  outcomes, labeled by result. Counters are emitted for every per-ticker
  outcome as well as bulk operations.
- ``alphard_daily_sync_total{result="ok|failed|timeout"}`` — daily_sync
  outcomes from the daemon thread.

Gauges exposed
--------------
- ``alphard_uptime_seconds`` — seconds since ``alphard_metrics_server.start()``.
- ``alphard_heartbeat_last_tick_timestamp`` — unix epoch of the most recent
  heartbeat tick. Pair with ``time() - alphard_heartbeat_last_tick_timestamp``
  in Prometheus to alert on stale heartbeats.
- ``alphard_backfill_progress_tickers_done`` — count of tickers processed
  in the current/last backfill run.
- ``alphard_backfill_progress_tickers_total`` — total ticker universe size.
- ``alphard_backfill_progress_bars_written`` — bars inserted into Postgres
  in the current/last backfill run.
- ``alphard_daily_sync_last_run_timestamp`` — unix epoch of the most
  recent daily_sync completion.
- ``alphard_daily_sync_last_run_status{status}`` — 1 for the status
  last reported (only one of {ok, failed, timeout} is 1 at a time; others 0).
- ``alphard_open_positions`` — current count of positions (broker stub).
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Maximum request body size accepted (we never read bodies, but BaseHTTPRequestHandler
# requires a sane limit).
_MAX_REQUEST_LINE = 8192


class MetricsRegistry:
    """In-process metrics store with thread-safe increments."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at: float = time.monotonic()
        self._start_epoch: float = time.time()
        # counters: dict[name, dict[label_key, value]] where label_key is frozenset of (k, v)
        self._counters: dict[str, dict[frozenset[tuple[str, str]], float]] = {}
        # gauges: dict[name, dict[label_key, value]]
        self._gauges: dict[str, dict[frozenset[tuple[str, str]], float]] = {}

    # --- counters ---------------------------------------------------------
    def inc_counter(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        labels = labels or {}
        key = frozenset(labels.items())
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            bucket[key] = bucket.get(key, 0.0) + value

    # --- gauges -----------------------------------------------------------
    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        labels = labels or {}
        key = frozenset(labels.items())
        with self._lock:
            bucket = self._gauges.setdefault(name, {})
            bucket[key] = value

    # --- read-only helpers (test + introspection surface) ---------------
    def get_counter(self, name: str, labels: dict[str, str] | None = None) -> float:
        """Return current value of a counter (0.0 if absent). Public for tests."""
        key = frozenset((labels or {}).items())
        with self._lock:
            bucket = self._counters.get(name, {})
            return bucket.get(key, 0.0)

    def get_gauge(self, name: str, labels: dict[str, str] | None = None) -> float:
        """Return current value of a gauge (0.0 if absent). Public for tests."""
        key = frozenset((labels or {}).items())
        with self._lock:
            bucket = self._gauges.get(name, {})
            return bucket.get(key, 0.0)

    # --- exposition ------------------------------------------------------
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_at

    def render(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines: list[str] = []
        # Self-gauges first (always present, no labels).
        lines.append("# HELP alphard_uptime_seconds Process uptime in seconds.")
        lines.append("# TYPE alphard_uptime_seconds gauge")
        lines.append(f"alphard_uptime_seconds {self.uptime_seconds():.3f}")
        lines.append("# HELP alphard_heartbeat_last_tick_timestamp " "Unix epoch of last heartbeat tick.")
        lines.append("# TYPE alphard_heartbeat_last_tick_timestamp gauge")
        last_tick = self._get_gauge_or_zero("alphard_heartbeat_last_tick_timestamp")
        lines.append(f"alphard_heartbeat_last_tick_timestamp {last_tick:.0f}")
        with self._lock:
            counters_snapshot = {name: dict(buckets) for name, buckets in self._counters.items()}
            gauges_snapshot = {name: dict(buckets) for name, buckets in self._gauges.items()}
        for name in sorted(counters_snapshot):
            lines.append(f"# HELP {name} Counter.")
            lines.append(f"# TYPE {name} counter")
            for labels_key, value in sorted(counters_snapshot[name].items()):
                label_str = _format_labels(labels_key)
                lines.append(f"{name}{label_str} {value:.0f}")
        for name in sorted(gauges_snapshot):
            # Skip the self-gauges already emitted at the top.
            if name in {"alphard_uptime_seconds", "alphard_heartbeat_last_tick_timestamp"}:
                continue
            lines.append(f"# HELP {name} Gauge.")
            lines.append(f"# TYPE {name} gauge")
            for labels_key, value in sorted(gauges_snapshot[name].items()):
                label_str = _format_labels(labels_key)
                lines.append(f"{name}{label_str} {value:.3f}")
        return "\n".join(lines) + "\n"

    def _get_gauge_or_zero(self, name: str) -> float:
        with self._lock:
            bucket = self._gauges.get(name, {})
            for value in bucket.values():
                return value
        return 0.0


def _format_labels(label_key: frozenset[tuple[str, str]]) -> str:
    if not label_key:
        return ""
    parts = [f'{k}="{v}"' for k, v in sorted(label_key)]
    return "{" + ",".join(parts) + "}"


class _Handler(BaseHTTPRequestHandler):
    """HTTP handler for /health and /metrics endpoints."""

    # Suppress default access logging — it goes to stderr and is noisy.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature is from stdlib
        return

    def do_GET(self) -> None:  # noqa: N802 - signature is from stdlib
        if self.path == "/health":
            self._reply(200, "text/plain", b"ok\n")
        elif self.path == "/metrics":
            body = self.server.metrics.render().encode("utf-8")  # type: ignore[attr-defined]
            self._reply(200, "text/plain; version=0.0.4", body)
        else:
            self._reply(404, "text/plain", b"not found\n")

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MetricsServer:
    """ThreadingHTTPServer wrapper with lifecycle helpers."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self._registry = MetricsRegistry()
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.metrics = self._registry  # type: ignore[attr-defined]
        self._server.timeout = 1.0  # allow shutdown check every second
        self._thread: threading.Thread | None = None
        self._port = port

    @property
    def registry(self) -> MetricsRegistry:
        return self._registry

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        if self._thread is not None:
            return
        # Heartbeat baseline so the metric is present from the first scrape.
        self._registry.set_gauge("alphard_heartbeat_last_tick_timestamp", time.time())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="alphard-metrics-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
        self._thread = None
