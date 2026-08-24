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

        Issue #216: the compose grafana service binds
        ``${APPDATA_DIR:-/srv/alphard}/grafana/dashboards`` →
        ``/var/lib/grafana/dashboards`` (sister-fix to PR #148 which
        parameterised the /var/lib/grafana data bind). The provider
        ``path: /var/lib/grafana/dashboards`` is unchanged because
        that is the in-container target, not the host-side source.
        So the contract still holds: provider scans target T, compose
        mounts to target T.
        """
        provider = _load_provider()
        provider_path = provider["providers"][0]["options"]["path"]

        compose = _load_compose()
        grafana = compose["services"].get("grafana", {})
        mounts = grafana.get("volumes", [])

        # The compose grafana service must mount the dashboards directory
        # somewhere — find the bind-mount whose container-side target is
        # /var/lib/grafana/dashboards (the provider scan target) and
        # capture it. Issue #216 switched the host source from a
        # relative ./docker/grafana/dashboards path to the
        # parameterised ${APPDATA_DIR:-/srv/alphard}/grafana/dashboards,
        # so we match on the container TARGET, not the host source.
        dashboard_mount_targets = []
        for v in mounts:
            if isinstance(v, str):
                # Strip optional trailing mode (":ro" / ":rw") so the
                # dst is always the last field. The host path may
                # itself contain ":" (the APPDATA_DIR default), so we
                # rsplit on the LAST ":" rather than split(":")[1].
                stripped = v
                if v.endswith(":ro") or v.endswith(":rw"):
                    stripped = v[:-3]
                if ":" not in stripped:
                    continue
                _src, target = stripped.rsplit(":", 1)
            else:
                target = v.get("target", "")
            if target == "/var/lib/grafana/dashboards":
                dashboard_mount_targets.append(target)

        assert dashboard_mount_targets, (
            "compose grafana service must mount a dashboards directory "
            "at /var/lib/grafana/dashboards (the provider scan target); "
            "no such bind-mount found (issue #216: APPDATA_DIR "
            "parameterisation must keep /var/lib/grafana/dashboards as "
            "the in-container target so the provider scans the right "
            "directory). Mounts: " + repr(mounts)
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
