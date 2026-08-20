"""Structural regression tests for the Grafana provisioning configuration.

Issue #56: the provider scans a different path than the compose mount, so no
dashboards ever load via `docker compose --profile observability up`. We pin
the provider path so a future refactor cannot silently re-introduce the
mismatch.

These tests are pure-Python: they read JSON / YAML without invoking the
Compose CLI or Grafana itself, so they run in any checkout layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


REPO = Path(__file__).resolve().parent.parent
PROVIDER_YML = REPO / "docker" / "grafana" / "provisioning" / "dashboards" / "provider.yml"
COMPOSE_YML = REPO / "docker-compose.yaml"
GRAFANA_DASHBOARDS_DIR = REPO / "docker" / "grafana" / "dashboards"


def _load_provider() -> dict:
    """Load provider.yml as JSON (the file is JSON, despite the .yml suffix)."""
    return json.loads(PROVIDER_YML.read_text(encoding="utf-8"))


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_YML.read_text(encoding="utf-8"))


class TestGrafanaProviderPath:
    """Issue #56: provider path must match the compose mount."""

    def test_provider_yaml_parses(self) -> None:
        data = _load_provider()
        assert "providers" in data
        assert isinstance(data["providers"], list)
        assert len(data["providers"]) >= 1

    def test_provider_path_matches_compose_mount(self) -> None:
        """The provider must scan the SAME path that compose mounts dashboards
        to. Pre-fix the provider scanned /etc/grafana/provisioning/dashboards
        while compose mounted the JSONs at /var/lib/grafana/dashboards, so
        Grafana loaded zero dashboards.
        """
        provider = _load_provider()
        provider_path = provider["providers"][0]["options"]["path"]

        compose = _load_compose()
        grafana = compose["services"].get("grafana", {})
        mounts = grafana.get("volumes", [])

        # The compose grafana service must mount the dashboards directory
        # somewhere — find the bind-mount whose source ends with
        # /docker/grafana/dashboards and capture the host-side target path.
        # Compose volume syntax is `src:dst[:mode]` (short) or
        # `{source, target, ...}` (long). Parse both shapes.
        dashboard_mount_targets = []
        for v in mounts:
            if isinstance(v, str):
                # Strip optional trailing mode (":ro" / ":rw") so the dst
                # is always the 2nd field.
                parts = v.split(":")
                if len(parts) < 2:
                    continue
                src = parts[0]
                target = parts[1]
            else:
                src = v.get("source", "")
                target = v.get("target", "")
            # Match the dashboards directory OR any ancestor path that ends
            # with /docker/grafana/dashboards (handles bind mounts with or
            # without leading ./).
            if src.rstrip("/").endswith("docker/grafana/dashboards"):
                dashboard_mount_targets.append(target)

        assert dashboard_mount_targets, (
            "compose grafana service must mount docker/grafana/dashboards " "at a target path the provider scans"
        )
        for target in dashboard_mount_targets:
            assert provider_path == target, (
                f"provider scans {provider_path!r} but compose mounts "
                f"dashboards at {target!r}; pick one path and align both. "
                f"See issue #56."
            )

    def test_dashboard_jsons_exist(self) -> None:
        """Sanity: at least one alphard dashboard JSON must exist under
        docker/grafana/dashboards/, otherwise the empty-mount case looks
        like a path mismatch when it is actually a missing-files case.
        """
        jsons = list(GRAFANA_DASHBOARDS_DIR.glob("*.json"))
        assert jsons, (
            f"no dashboard JSONs found in {GRAFANA_DASHBOARDS_DIR}; " f"provider cannot load what does not exist"
        )
        # Sanity-check one of them is a real grafana dashboard (has a "title" key).
        first = json.loads(jsons[0].read_text(encoding="utf-8"))
        assert "title" in first, f"{jsons[0].name} does not look like a Grafana dashboard"
