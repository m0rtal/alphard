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
        """The provider must scan a path that actually contains
        dashboards. Pre-fix the provider scanned
        ``/etc/grafana/provisioning/dashboards`` while compose mounted
        the JSONs at ``/var/lib/grafana/dashboards``, so Grafana
        loaded zero dashboards.

        Compose refactor 2.0 (kanban t_884fec4a): the grafana
        service no longer bind-mounts ``/var/lib/grafana/dashboards``
        from the host. Instead, the docker/entrypoint_grafana.sh
        wrapper decodes DASHBOARD_*_JSON_B64 env vars and writes
        the JSON files into ``/var/lib/grafana/dashboards`` at
        container startup. The provider's ``path: /var/lib/grafana/dashboards``
        remains the in-container scan target — just now it's
        populated by the entrypoint rather than a host bind.

        Why this still satisfies the issue #56 contract:
          - Pre-refactor: provider scans target T, compose mounts
            dashboards at target T from the host. ✅
          - Post-refactor: provider scans target T, entrypoint writes
            dashboards into target T from B64 env vars. ✅
          - Same provider path, same in-container target — just a
            different population mechanism.

        This test verifies the entrypoint decodes at least one
        DASHBOARD_*_B64 variable into ``/var/lib/grafana/dashboards``
        and that the provider path matches.
        """
        provider = _load_provider()
        provider_path = provider["providers"][0]["options"]["path"]

        compose = _load_compose()
        grafana = compose["services"].get("grafana", {})

        # 1. Provider path must be /var/lib/grafana/dashboards (the
        # upstream-default in-container target). Refactor 2.0 keeps
        # this path — we just populate it via the entrypoint rather
        # than a host bind-mount.
        assert provider_path == "/var/lib/grafana/dashboards", (
            f"provider path must be /var/lib/grafana/dashboards "
            f"(matches the entrypoint write target + upstream "
            f"default). Got: {provider_path!r}"
        )

        # 2. The grafana service's entrypoint MUST write dashboards
        # to the provider path. We grep the entrypoint script for
        # the decode call + the provider path.
        entrypoint = grafana.get("entrypoint")
        assert entrypoint is not None, (
            "grafana service must declare an entrypoint (compose "
            "refactor 2.0 wraps the upstream /run.sh with our decoder)"
        )
        if isinstance(entrypoint, list):
            entrypoint_str = " ".join(str(x) for x in entrypoint)
        else:
            entrypoint_str = str(entrypoint)
        assert "/entrypoint_grafana.sh" in entrypoint_str, (
            f"grafana entrypoint must point at /entrypoint_grafana.sh "
            f"(our wrapper that decodes *_B64 env vars). Got: {entrypoint_str!r}"
        )
        # Read the entrypoint script and verify it writes to the
        # provider path AND decodes at least one DASHBOARD_*_JSON_B64.
        entrypoint_script = REPO / "docker" / "entrypoint_grafana.sh"
        assert entrypoint_script.is_file(), (
            f"docker/entrypoint_grafana.sh must exist at the repo root. " f"Got path: {entrypoint_script}"
        )
        script_text = entrypoint_script.read_text()
        assert provider_path in script_text, (
            f"entrypoint_grafana.sh must write to {provider_path} "
            f"(the provider scan target). Got script:\n{script_text[:800]}"
        )
        assert "DASHBOARD_" in script_text and "_B64" in script_text, (
            f"entrypoint_grafana.sh must decode at least one "
            f"DASHBOARD_*_B64 env var. Got script:\n{script_text[:800]}"
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
