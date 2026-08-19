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


COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yaml"


def _load_compose() -> dict:
    """Load docker-compose.yaml. Resolved via __file__ so the runner user
    doesn't need to traverse /root — works in any checkout layout."""
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

    def test_no_cron_service(self) -> None:
        """Phase 1.6 audit cleanup: cron profile is gone.

        daily_sync is now an in-process daemon thread (src/main.py),
        monitored by an in-process watchdog (_run_daily_sync_watchdog).
        The cron profile is no longer deployed; if it ever returns, it
        would compete with the in-process daemon for the daily_sync
        subprocess, causing duplicate writes and timer races.
        """
        data = _load_compose()
        services = data["services"]
        assert "cron" not in services, (
            "cron service must NOT exist — daily_sync is an in-process "
            "daemon thread with an in-process watchdog, no separate "
            "cron profile needed."
        )

    def test_pg_init_service_exists(self) -> None:
        """Phase 1.6 audit: init_postgres.sh must run automatically on
        first deploy. Compose provides this via the one-shot ``pg-init``
        service that injects the 192.168.0.0/16 trust line into
        pg_hba.conf after postgres becomes healthy.
        """
        data = _load_compose()
        services = data["services"]
        assert "pg-init" in services, (
            "pg-init service must exist so init_postgres.sh runs on "
            "first deploy; without it the bot hangs on auth_probe for "
            "clusters with a fresh volume."
        )
        pg_init = services["pg-init"]
        assert pg_init.get("restart") == "no", (
            "pg-init must be a one-shot (restart: no) — once the " "trust line is injected, the container exits."
        )

    def test_bot_depends_on_pg_init_completed(self) -> None:
        """alphard-bot must wait for pg-init to finish before starting,
        otherwise the first auth_probe runs before the trust line
        is injected and silently falls back to scram auth."""
        data = _load_compose()
        bot = data["services"]["alphard-bot"]
        deps = bot.get("depends_on", {})
        assert isinstance(deps, dict)
        assert deps.get("pg-init", {}).get("condition") == ("service_completed_successfully"), (
            "alphard-bot.depends_on.pg-init.condition must be "
            "service_completed_successfully so the trust line is "
            "applied before the bot tries to connect"
        )

    def test_bot_depends_on_postgres_health(self) -> None:
        data = _load_compose()
        bot = data["services"]["alphard-bot"]
        deps = bot.get("depends_on", {})
        assert isinstance(deps, dict)
        assert "postgres" in deps, "alphard-bot must depend_on postgres with condition: service_healthy"
        assert deps["postgres"].get("condition") == "service_healthy"
