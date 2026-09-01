"""Regression tests for the cycle145 dashboard cleanup.

User feedback 2026-09-02:
- ``Heartbeat rate (5m window)`` panel was uninformative (just a simple
  ``increase(alphard_heartbeats_total[5m])`` timeseries — if the
  heartbeat loop is running, the metric goes up by 1 every 60s. The
  panel tells us nothing about whether the *system* is healthy).
- ``Heartbeat lag (alerts if > 60s)`` was redundant — `alphard_uptime_seconds`
  (panel: "Alphard uptime") and per-loop supervisor visibility cover the
  same signal more usefully.

These tests pin the post-cleanup contract so the panels cannot creep back
in via a future content edit.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "docker" / "grafana" / "dashboards" / "alphard-phase28.json"


def _load_dashboard() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


class TestRemovedHeartbeatPanels:
    """Cycle145 user request: drop heartbeat rate + heartbeat lag panels."""

    def test_heartbeat_lag_panel_gone(self) -> None:
        d = _load_dashboard()
        titles = [p.get("title", "") for p in d["panels"]]
        assert "Heartbeat lag (alerts if > 60s)" not in titles

    def test_heartbeat_rate_panel_gone(self) -> None:
        d = _load_dashboard()
        titles = [p.get("title", "") for p in d["panels"]]
        assert "Heartbeat rate (5m window)" not in titles

    def test_alphard_uptime_panel_remains(self) -> None:
        """``Alphard uptime`` is the heartbeat signal we kept (more useful than
        the rate timeseries or the lag threshold)."""
        d = _load_dashboard()
        titles = [p.get("title", "") for p in d["panels"]]
        assert "Alphard uptime" in titles

    def test_remaining_panels_match_expected(self) -> None:
        """Pin the post-cleanup panel set so an unrelated future edit cannot
        silently change the surface area."""
        d = _load_dashboard()
        titles = sorted(p.get("title", "") for p in d["panels"])
        expected = sorted(
            [
                "Alphard uptime",
                "Daily-candle row accumulation (ohlcv_daily)",
                "Tickers with full history (backfill_complete)",
                "Universe size (tickers in universe)",
            ]
        )
        assert titles == expected

    def test_no_heartbeat_metric_queries_remain_in_kept_panels(self) -> None:
        """Defensive: even if a panel is renamed, queries against
        ``alphard_heartbeat_*`` metrics should not appear in this dashboard
        (they belong in operational logs/alerts, not the trader dashboard)."""
        d = _load_dashboard()
        for pan in d["panels"]:
            for tgt in pan.get("targets", []) or []:
                expr = tgt.get("expr", "")
                assert (
                    "alphard_heartbeat" not in expr
                ), f"panel {pan.get('title')!r} references heartbeat metric: {expr!r}"
