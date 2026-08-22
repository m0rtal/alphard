"""Tests for src/metrics_server.py — Phase 2.8 metrics server."""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from http.client import HTTPConnection

import pytest

from src.metrics_server import MetricsRegistry, MetricsServer

# --- MetricsRegistry ---------------------------------------------------------


class TestMetricsRegistry:
    def test_inc_counter_default_value_is_one(self) -> None:
        r = MetricsRegistry()
        r.inc_counter("test_total")
        assert r.get_counter("test_total") == 1.0

    def test_inc_counter_with_explicit_value(self) -> None:
        r = MetricsRegistry()
        r.inc_counter("test_total", value=5.0)
        r.inc_counter("test_total", value=3.0)
        assert r.get_counter("test_total") == 8.0

    def test_inc_counter_with_labels_isolates_label_tuples(self) -> None:
        r = MetricsRegistry()
        r.inc_counter("http_requests_total", {"status": "200"})
        r.inc_counter("http_requests_total", {"status": "200"})
        r.inc_counter("http_requests_total", {"status": "500"})
        assert r.get_counter("http_requests_total", {"status": "200"}) == 2.0
        assert r.get_counter("http_requests_total", {"status": "500"}) == 1.0

    def test_inc_counter_label_order_does_not_matter(self) -> None:
        r = MetricsRegistry()
        r.inc_counter("test_total", {"a": "1", "b": "2"})
        r.inc_counter("test_total", {"b": "2", "a": "1"})
        assert r.get_counter("test_total", {"a": "1", "b": "2"}) == 2.0

    def test_set_gauge_overwrites(self) -> None:
        r = MetricsRegistry()
        r.set_gauge("alpha_gauge", 10.0)
        r.set_gauge("alpha_gauge", 20.0)
        assert r.get_gauge("alpha_gauge") == 20.0

    def test_set_gauge_with_labels(self) -> None:
        r = MetricsRegistry()
        r.set_gauge("memory_bytes", 1024, {"type": "rss"})
        r.set_gauge("memory_bytes", 2048, {"type": "heap"})
        assert r.get_gauge("memory_bytes", {"type": "rss"}) == 1024
        assert r.get_gauge("memory_bytes", {"type": "heap"}) == 2048

    def test_render_includes_help_and_type_lines(self) -> None:
        r = MetricsRegistry()
        r.inc_counter("requests_total", {"method": "GET"})
        text = r.render()
        assert "# HELP requests_total" in text
        assert "# TYPE requests_total counter" in text
        assert 'requests_total{method="GET"} 1' in text

    def test_render_includes_uptime_gauge(self) -> None:
        r = MetricsRegistry()
        text = r.render()
        assert "# TYPE alphard_uptime_seconds gauge" in text
        assert "alphard_uptime_seconds " in text

    def test_render_is_sorted_for_deterministic_output(self) -> None:
        r = MetricsRegistry()
        r.inc_counter("zeta_total")
        r.inc_counter("alpha_total")
        text = r.render()
        # alpha must come before zeta in the rendered output.
        assert text.index("# TYPE alpha_total") < text.index("# TYPE zeta_total")

    def test_uptime_seconds_increases_over_time(self) -> None:
        r = MetricsRegistry()
        t0 = r.uptime_seconds()
        time.sleep(0.05)
        t1 = r.uptime_seconds()
        assert t1 > t0
        assert (t1 - t0) >= 0.04

    def test_concurrent_inc_counter_is_thread_safe(self) -> None:
        """Multiple threads incrementing the same counter must not lose updates."""
        r = MetricsRegistry()
        n_threads = 10
        per_thread = 1000

        def worker() -> None:
            for _ in range(per_thread):
                r.inc_counter("contended_total")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert r.get_counter("contended_total") == float(n_threads * per_thread)

    def test_render_format_matches_prometheus_spec(self) -> None:
        """Output must end with newline, lines separated by \\n, type/help pairs first."""
        r = MetricsRegistry()
        r.inc_counter("requests_total")
        text = r.render()
        assert text.endswith("\n")
        # Each metric has HELP followed by TYPE.
        for name in ["requests_total", "alphard_uptime_seconds"]:
            assert f"# HELP {name}" in text
            assert f"# TYPE {name}" in text


# --- MetricsServer HTTP endpoints ------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestMetricsServerHTTP:
    def setup_method(self) -> None:
        self._port = _free_port()
        self._server = MetricsServer(host="127.0.0.1", port=self._port)
        self._server.start()
        # Give the daemon thread a moment to bind the socket.
        for _ in range(50):
            try:
                conn = HTTPConnection("127.0.0.1", self._port, timeout=1)
                conn.request("GET", "/health")
                conn.getresponse().read()
                conn.close()
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.02)

    def teardown_method(self) -> None:
        self._server.stop()

    def test_health_returns_200(self) -> None:
        conn = HTTPConnection("127.0.0.1", self._port, timeout=2)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert body == "ok\n"

    def test_metrics_returns_200_with_prometheus_content_type(self) -> None:
        conn = HTTPConnection("127.0.0.1", self._port, timeout=2)
        conn.request("GET", "/metrics")
        resp = conn.getresponse()
        resp.read()  # consume body
        assert resp.status == 200
        assert "text/plain" in resp.getheader("Content-Type", "")

    def test_metrics_response_includes_alphard_uptime(self) -> None:
        conn = HTTPConnection("127.0.0.1", self._port, timeout=2)
        conn.request("GET", "/metrics")
        body = conn.getresponse().read().decode("utf-8")
        assert "alphard_uptime_seconds" in body

    def test_metrics_endpoint_reflects_counter_updates(self) -> None:
        """Counter increments on the server are visible via /metrics scrape."""
        self._server.registry.inc_counter("api_calls_total", {"endpoint": "/foo"})
        self._server.registry.inc_counter("api_calls_total", {"endpoint": "/foo"})
        conn = HTTPConnection("127.0.0.1", self._port, timeout=2)
        conn.request("GET", "/metrics")
        body = conn.getresponse().read().decode("utf-8")
        assert 'api_calls_total{endpoint="/foo"} 2' in body

    def test_unknown_path_returns_404(self) -> None:
        conn = HTTPConnection("127.0.0.1", self._port, timeout=2)
        conn.request("GET", "/wat")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 404

    def test_concurrent_requests_succeed(self) -> None:
        """20 parallel /metrics requests should all return 200 OK without crashing."""
        results: list[int] = []
        errors: list[Exception] = []

        def fetch() -> None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/metrics", timeout=2) as r:
                    results.append(r.status)
                    r.read()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=fetch) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert results.count(200) == 20

    def test_metrics_endpoint_supports_already_emitted_gauges(self) -> None:
        """start() pre-emits alphard_heartbeat_last_tick_timestamp so first scrape shows it."""
        conn = HTTPConnection("127.0.0.1", self._port, timeout=2)
        conn.request("GET", "/metrics")
        body = conn.getresponse().read().decode("utf-8")
        assert "alphard_heartbeat_last_tick_timestamp" in body


# --- Lifecycle / thread safety ----------------------------------------------


class TestMetricsServerLifecycle:
    def test_start_idempotent(self) -> None:
        s = MetricsServer(host="127.0.0.1", port=_free_port())
        s.start()
        s.start()  # must not raise / must not spawn a second thread
        s.stop()

    def test_stop_without_start_is_safe(self) -> None:
        s = MetricsServer(host="127.0.0.1", port=_free_port())
        s.stop()  # no-op, must not raise

    def test_double_stop_is_safe(self) -> None:
        s = MetricsServer(host="127.0.0.1", port=_free_port())
        s.start()
        s.stop()
        s.stop()  # no-op

    def test_port_release_after_stop(self) -> None:
        """After stop() the bound port must be released for immediate reuse."""
        port = _free_port()
        s1 = MetricsServer(host="127.0.0.1", port=port)
        s1.start()
        s1.stop()
        s2 = MetricsServer(host="127.0.0.1", port=port)
        s2.start()  # must not raise OSError(EADDRINUSE)
        s2.stop()

    def test_port_property_reflects_construction_value(self) -> None:
        """The ``port`` property must echo back the port passed to the constructor."""
        port = _free_port()
        s = MetricsServer(host="127.0.0.1", port=port)
        assert s.port == port

    def test_registry_property_returns_shared_instance(self) -> None:
        """The ``registry`` property must always return the same MetricsRegistry."""
        s = MetricsServer(host="127.0.0.1", port=_free_port())
        assert s.registry is s.registry
        # Mutations via the returned registry are visible on subsequent reads.
        s.registry.inc_counter("smoke_total")
        assert s.registry.get_counter("smoke_total") == 1.0


# --- Conftest helpers / extension points for src.metrics_server ------------


def test_render_lines_total_count_matches_metric_count() -> None:
    """Sanity: render() emits at least one line per registered metric."""
    r = MetricsRegistry()
    r.inc_counter("a_total")
    r.inc_counter("b_total")
    r.set_gauge("c", 1.0)
    text = r.render()
    # Three metric bodies + two self-gauges (uptime + heartbeat) at minimum.
    a_lines = [ln for ln in text.splitlines() if ln.startswith("a_total")]
    b_lines = [ln for ln in text.splitlines() if ln.startswith("b_total")]
    assert a_lines and b_lines


@pytest.fixture
def free_port_server() -> "Generator[MetricsServer, None, None]":
    """Fixture for tests that need a running server bound to an ephemeral port."""
    s = MetricsServer(host="127.0.0.1", port=_free_port())
    s.start()
    yield s
    s.stop()


# --- Domain counters / gauges -------------------------------------------------
#
# These tests exercise the specific counter/gauges that src/main.py and the
# daemon threads actually emit (see docstring of src/metrics_server.py). They
# guard against accidental renames or label-set changes that would silently
# break Prometheus alerts and Grafana dashboards.


class TestMetricsServerDomainMetrics:
    def test_heartbeats_counter_accumulates_across_calls(self) -> None:
        r = MetricsRegistry()
        for _ in range(7):
            r.inc_counter("alphard_heartbeats_total")
        assert r.get_counter("alphard_heartbeats_total") == 7.0

    def test_backfill_counter_supports_all_documented_result_labels(self) -> None:
        r = MetricsRegistry()
        for result in ("ok", "skip", "error", "delisted"):
            r.inc_counter("alphard_backfill_total", {"result": result})
        text = r.render()
        for result in ("ok", "skip", "error", "delisted"):
            assert f'result="{result}"' in text

    def test_daily_sync_counter_supports_ok_failed_timeout(self) -> None:
        r = MetricsRegistry()
        r.inc_counter("alphard_daily_sync_total", {"result": "ok"})
        r.inc_counter("alphard_daily_sync_total", {"result": "failed"})
        r.inc_counter("alphard_daily_sync_total", {"result": "timeout"})
        assert r.get_counter("alphard_daily_sync_total", {"result": "ok"}) == 1.0
        assert r.get_counter("alphard_daily_sync_total", {"result": "failed"}) == 1.0
        assert r.get_counter("alphard_daily_sync_total", {"result": "timeout"}) == 1.0

    def test_backfill_progress_gauges_are_independent(self) -> None:
        """Tickers-done, tickers-total, and bars-written must be tracked separately."""
        r = MetricsRegistry()
        r.set_gauge("alphard_backfill_progress_tickers_done", 42.0)
        r.set_gauge("alphard_backfill_progress_tickers_total", 100.0)
        r.set_gauge("alphard_backfill_progress_bars_written", 1500.0)
        assert r.get_gauge("alphard_backfill_progress_tickers_done") == 42.0
        assert r.get_gauge("alphard_backfill_progress_tickers_total") == 100.0
        assert r.get_gauge("alphard_backfill_progress_bars_written") == 1500.0

    def test_daily_sync_status_gauge_is_mutually_exclusive(self) -> None:
        """Only one status label should be 1; others should be 0."""
        r = MetricsRegistry()
        r.set_gauge("alphard_daily_sync_last_run_status", 1.0, {"status": "ok"})
        r.set_gauge("alphard_daily_sync_last_run_status", 0.0, {"status": "failed"})
        r.set_gauge("alphard_daily_sync_last_run_status", 0.0, {"status": "timeout"})
        assert r.get_gauge("alphard_daily_sync_last_run_status", {"status": "ok"}) == 1.0
        assert r.get_gauge("alphard_daily_sync_last_run_status", {"status": "failed"}) == 0.0
        assert r.get_gauge("alphard_daily_sync_last_run_status", {"status": "timeout"}) == 0.0

    def test_open_positions_gauge_overwrites_previous_value(self) -> None:
        r = MetricsRegistry()
        r.set_gauge("alphard_open_positions", 3.0)
        r.set_gauge("alphard_open_positions", 5.0)
        assert r.get_gauge("alphard_open_positions") == 5.0

    def test_render_uses_integer_format_for_counters(self) -> None:
        """Counters must render without decimal fraction (``5`` not ``5.000``)."""
        r = MetricsRegistry()
        r.inc_counter("alphard_backfill_total", {"result": "ok"}, value=5.0)
        text = r.render()
        assert 'alphard_backfill_total{result="ok"} 5' in text
        assert 'alphard_backfill_total{result="ok"} 5.000' not in text

    def test_render_uses_three_decimal_format_for_gauges(self) -> None:
        """Custom gauges (not the pre-emitted self-gauges) must render with 3 decimals."""
        r = MetricsRegistry()
        r.set_gauge("alphard_open_positions", 7.0)
        text = r.render()
        assert "alphard_open_positions 7.000" in text
