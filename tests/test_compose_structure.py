"""Compose structure tests for alphard.

Issue #395 (chore: remove Grafana and Prometheus). The Grafana,
Prometheus, and alphard-chownfix services have been removed from
docker-compose.yaml. This module:

- Tests that the *remaining* services (postgres, alphard-bot)
  are present and structured correctly.
- Tests that the *removed* services (grafana, prometheus, chownfix)
  are explicitly absent — so a future PR that re-adds them by
  accident is caught.
- Drops the Grafana/Prometheus/Chownfix-specific tests that are
  now obsolete.

Tests run via ``docker compose config`` so the assertions are
made against the rendered, env-resolved compose tree, not the
raw yaml. This catches both syntax errors and missing-field errors
that yaml.safe_load() would miss.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yaml"


def _render_compose() -> dict:
    """Run ``docker compose config`` and parse the rendered yaml.

    Returns the parsed yaml as a dict. Tests that need the raw text
    can call ``_render_compose_text()`` instead.
    """
    import yaml  # local import: pyyaml ships with alphard dev deps

    text = _render_compose_text()
    return yaml.safe_load(text)


# Alias: tests merged from feature/alphard-web-v2 (PR #394) use the
# `_load_compose` name; keep both working without renaming all call-sites.
_load_compose = _render_compose


def _render_compose_text() -> str:
    r = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), "config"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, (
        f"docker compose config failed (rc={r.returncode}):\n" f"STDOUT:\n{r.stdout}\n\nSTDERR:\n{r.stderr}"
    )
    return r.stdout


# --- core services still present ----------------------------------------


def test_postgres_service_present() -> None:
    cfg = _render_compose()
    assert "postgres" in cfg["services"]
    svc = cfg["services"]["postgres"]
    assert svc["image"].startswith("postgres:")
    assert svc["container_name"] == "alphard-postgres"
    # ``volumes`` renders as a list of dicts after ``compose config``.
    vol_sources = [v.get("source") for v in svc.get("volumes", [])]
    assert "alphard-postgres-data" in vol_sources


def test_alphard_bot_service_present() -> None:
    cfg = _render_compose()
    assert "alphard-bot" in cfg["services"]
    svc = cfg["services"]["alphard-bot"]
    assert svc["container_name"] == "alphard-bot"
    # The bot must depend on postgres (H-7 bugfix).
    deps = svc.get("depends_on", {})
    if isinstance(deps, dict):
        assert "postgres" in deps
    else:
        assert "postgres" in deps


def test_alphard_bot_healthcheck_targets_8765() -> None:
    cfg = _render_compose()
    test_cmd = cfg["services"]["alphard-bot"]["healthcheck"]["test"]
    # `test` renders as a list of strings after compose config.
    assert any("8765" in str(arg) for arg in test_cmd)


# --- removed services are GONE ------------------------------------------


class TestGrafanaRemoved:
    """Issue #395: grafana service is no longer in the compose stack."""

    @pytest.fixture
    def cfg(self) -> dict:
        return _render_compose()

    def test_grafana_service_absent(self, cfg: dict) -> None:
        assert "grafana" not in cfg["services"]

    def test_no_grafana_container_in_active_set(self, cfg: dict) -> None:
        for name in cfg["services"]:
            assert not name.startswith("alphard-grafana"), name


class TestPrometheusRemoved:
    """Issue #395: prometheus service is no longer in the compose stack."""

    @pytest.fixture
    def cfg(self) -> dict:
        return _render_compose()

    def test_prometheus_service_absent(self, cfg: dict) -> None:
        assert "prometheus" not in cfg["services"]

    def test_no_prometheus_container_in_active_set(self, cfg: dict) -> None:
        for name in cfg["services"]:
            assert not name.startswith("alphard-prometheus"), name

    def test_prometheus_volume_absent(self, cfg: dict) -> None:
        # alphard-prometheus-data is the named volume that backed the
        # TSDB. With prometheus gone, the volume is too.
        assert "alphard-prometheus-data" not in cfg.get("volumes", {})


class TestChownfixRemoved:
    """Issue #395: alphard-chownfix sidecar is no longer in compose.

    chownfix existed only to set up the grafana/prometheus leaf
    directories. With both gone, the sidecar has no remaining
    work, so it's removed entirely.
    """

    @pytest.fixture
    def cfg(self) -> dict:
        return _render_compose()

    def test_chownfix_service_absent(self, cfg: dict) -> None:
        assert "chownfix" not in cfg["services"]

    def test_no_chownfix_container_in_active_set(self, cfg: dict) -> None:
        for name in cfg["services"]:
            assert not name.startswith("alphard-chownfix"), name

    def test_no_service_depends_on_chownfix(self, cfg: dict) -> None:
        for name, svc in cfg["services"].items():
            deps = svc.get("depends_on") or {}
            if isinstance(deps, dict):
                deps = list(deps.keys())
            assert "chownfix" not in deps, f"{name} still depends_on chownfix"


# --- networking + compose-wide invariants -------------------------------


def test_alphard_net_network_present() -> None:
    cfg = _render_compose()
    assert "alphard-net" in cfg["networks"]
    assert cfg["networks"]["alphard-net"].get("driver") == "bridge"


def test_all_services_join_alphard_net() -> None:
    cfg = _render_compose()
    for name, svc in cfg["services"].items():
        nets = svc.get("networks") or ["default"]
        assert "alphard-net" in nets, f"{name} does not join alphard-net"


def test_no_docker_compose_version_key() -> None:
    """``version`` at the top of compose is obsolete and docker logs
    a warning. We removed it when removing the deprecated services
    so a future PR that re-adds it is caught.
    """
    import yaml

    with open(COMPOSE_PATH) as f:
        raw = yaml.safe_load(f)
    assert "version" not in raw, "docker-compose.yaml still has the obsolete top-level " "'version' key; remove it."


class TestAlphardWebSecurity:
    """Issue #406 — security contract for the alphard-web service.

    Pins three production invariants so a future compose refactor
    cannot silently re-introduce the LAN-exposure defect:

    1. ALPHARD_WEB_TOKEN env var MUST be injected (auth gate enabled
       on every protected endpoint — see src/web/server.py check_auth).
    2. Container port MUST NOT publish to a host LAN-reachable address
       in a way that bypasses the gate (the compose port mapping is
       `127.0.0.1:8081:8080` so only loopback can reach it directly;
       operators who want LAN access must front it with a reverse
       proxy that injects the bearer header).
    """

    def test_alphard_web_injects_auth_token_env(self) -> None:
        """Issue #406: ALPHARD_WEB_TOKEN MUST be in alphard-web's env.

        Without this env var, src/web/server.py::check_auth fails open
        and every protected endpoint returns sensitive data (DSN-derived
        values, full universe, backup paths) to any LAN peer.
        """
        data = _load_compose()
        web = data["services"].get("alphard-web")
        assert web is not None, "alphard-web service must exist"
        env = web.get("environment", {})
        if isinstance(env, list):
            keys = {item.split("=", 1)[0] for item in env if isinstance(item, str) and "=" in item}
            keys |= {item for item in env if isinstance(item, str) and "=" not in item}
        else:
            keys = set(env.keys())
        assert "ALPHARD_WEB_TOKEN" in keys, (
            "alphard-web.environment MUST declare ALPHARD_WEB_TOKEN "
            "(issue #406: auth gate fails open without it). Got: "
            f"{sorted(keys)}"
        )

    def test_alphard_web_port_is_loopback_only(self) -> None:
        """Issue #406: published port MUST bind to 127.0.0.1 (loopback).

        The compose port mapping for alphard-web must resolve to a host
        IP of ``127.0.0.1`` (loopback) so the dashboard is reachable
        only via a reverse proxy that injects the bearer header. Docker
        Compose v2 renders long-form ``HOST_IP:HOST_PORT:CONTAINER_PORT``
        entries as a dict with ``host_ip``, ``published``, ``target``;
        the source YAML still uses the string form ``127.0.0.1:8081:8080``
        for readability.
        """
        data = _load_compose()
        web = data["services"]["alphard-web"]
        ports = web.get("ports", [])
        web_ports = [p for p in ports if "8081" in str(p)]
        assert web_ports, f"alphard-web MUST publish port 8081; got: {ports}"
        for port_mapping in web_ports:
            # Normalize: long-form string OR Compose v2 dict both appear
            if isinstance(port_mapping, dict):
                host_ip = port_mapping.get("host_ip", "")
                published = port_mapping.get("published", "")
                target = port_mapping.get("target", "")
            else:
                s = str(port_mapping)
                # Long form must start with 127.0.0.1:
                assert "127.0.0.1:8081:8080" in s, (
                    f"alphard-web port mapping must bind 127.0.0.1:8081:8080 "
                    f"(issue #406: 0.0.0.0/short form exposes dashboard to LAN); "
                    f"got: {s}"
                )
                continue
            assert host_ip == "127.0.0.1", (
                f"alphard-web port mapping host_ip MUST be 127.0.0.1 "
                f"(issue #406: 0.0.0.0 exposes dashboard to LAN); "
                f"got: {host_ip!r} (full mapping: {port_mapping!r})"
            )
            assert str(published) == "8081", f"alphard-web port mapping published MUST be 8081; " f"got: {published!r}"
            assert str(target) == "8080", f"alphard-web port mapping target MUST be 8080; " f"got: {target!r}"
