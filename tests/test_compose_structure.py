"""Structural regression tests for docker-compose.yaml.

Pure-Python: we never shell out to `docker compose config` because
that requires the Compose CLI and a build context. Instead we read
the file as a YAML document and check that the alphard services exist
with the required keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


COMPOSE = Path("/root/projects/alphard/docker-compose.yaml")


def _load_compose() -> dict:
    import os

    # CI runners (actions/checkout@v5) check out files read-only.
    # Best-effort chmod so our read_text() doesn't PermissionError.
    try:
        os.chmod(COMPOSE, 0o644)
    except OSError:
        pass
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


class TestCompose:
    def test_yaml_parses(self) -> None:
        data = _load_compose()
        assert isinstance(data, dict)
        assert "services" in data

    def test_alphard_bot_exists(self) -> None:
        data = _load_compose()
        assert "alphard-bot" in data["services"], "alphard-bot service must exist in docker-compose.yaml"

    def test_postgres_healthcheck_uses_custom_script(self) -> None:
        data = _load_compose()
        healthcheck = data["services"].get("postgres", {}).get("healthcheck")
        assert healthcheck is not None, "postgres service must declare a healthcheck"
        # The command must reference our real-auth-check script, not
        # the bare pg_isready (which silently passes on stale passwords).
        cmd = " ".join(healthcheck.get("test", []))
        assert "pg-healthcheck.sh" in cmd, f"postgres healthcheck must call our custom script; got: {cmd}"

    def test_cron_service_present_when_scheduled(self) -> None:
        """Phase 1.6 audit: daily_sync must run on a schedule, otherwise
        the bot is a Phase 0 stub once backfill finishes."""
        data = _load_compose()
        services = data["services"]
        assert "cron" in services, (
            "cron service must exist so daily_sync + freshness + "
            "db_health checks run automatically; without it the bot "
            "stops updating after backfill."
        )
        cron = services["cron"]
        assert cron.get("profiles") == ["scheduled"], (
            "cron must be opt-in via `profiles: [scheduled]` so smoke " "deploys (no cron, just bot) still work"
        )

    def test_cron_mounts_logs_volume(self) -> None:
        data = _load_compose()
        cron = data["services"]["cron"]
        volumes = cron.get("volumes", [])
        # At least one bind mount pointing at the cron-logs path.
        assert any(
            "/app/logs" in str(v) for v in volumes
        ), "cron needs a writable /app/logs mount for the cron.log file"

    def test_bot_depends_on_postgres_health(self) -> None:
        data = _load_compose()
        bot = data["services"]["alphard-bot"]
        deps = bot.get("depends_on", {})
        assert isinstance(deps, dict)
        assert "postgres" in deps, "alphard-bot must depend_on postgres with condition: service_healthy"
        assert deps["postgres"].get("condition") == "service_healthy"
