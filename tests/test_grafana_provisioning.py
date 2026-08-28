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
        """The provider must scan a path that contains dashboards.
        Issue #297: provisioning + dashboards are bind-mounted from
        the repo directly. The provider at
        docker/grafana/provisioning/dashboards/provider.yml scans
        ``/var/lib/grafana/dashboards``, which compose bind-mounts
        from ``./docker/grafana/dashboards``. Verify both ends match.
        """
        provider = _load_provider()
        provider_path = provider["providers"][0]["options"]["path"]

        compose = _load_compose()
        grafana = compose["services"].get("grafana", {})

        # Provider path must be /var/lib/grafana/dashboards (matches
        # the bind-mount target in compose.yaml grafana.volumes).
        assert provider_path == "/var/lib/grafana/dashboards", (
            f"provider path must be /var/lib/grafana/dashboards. " f"Got: {provider_path!r}"
        )

        # grafana service must bind-mount /var/lib/grafana/dashboards
        # from ./docker/grafana/dashboards.
        volumes = grafana.get("volumes", [])
        dash_mount = None
        for v in volumes:
            v_str = str(v)
            if "/var/lib/grafana/dashboards" in v_str:
                dash_mount = v_str
                break
        assert dash_mount is not None, (
            f"grafana.volumes must bind-mount /var/lib/grafana/dashboards "
            f"from ./docker/grafana/dashboards (issue #297); got: {volumes}"
        )
        # Must be read-only (dashboards are git-versioned, not UI-editable).
        assert ":ro" in dash_mount, f"dashboards bind-mount must be :ro (issue #297); got: {dash_mount}"

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
