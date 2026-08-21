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
        service that injects a scoped trust line into pg_hba.conf after
        postgres becomes healthy.

        As of issue #97 the default trust CIDR is
        ``${POSTGRES_TRUST_SUBNET:-172.16.0.0/12}`` (Docker bridge range),
        not the legacy ``192.168.0.0/16`` LAN range.
        """
        data = _load_compose()
        services = data["services"]
        assert "pg-init" in services, (
            "pg-init service must exist so init_postgres.sh runs on "
            "first deploy; without it the bot hangs on auth_probe for "
            "clusters with a fresh volume."
        )
        pg_init = services["pg-init"]
        assert (
            pg_init.get("restart") == "no"
        ), "pg-init must be a one-shot (restart: no) — once the trust line is injected, the container exits."
        # Issue #97: pg-init must source POSTGRES_TRUST_SUBNET from .env
        # and default to the Docker bridge range, never the legacy LAN /16.
        env = pg_init.get("environment", {})
        if isinstance(env, list):
            env_map = {}
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, _, v = item.partition("=")
                    env_map[k] = v
                elif isinstance(item, str):
                    env_map[item] = None
            env = env_map
        trust_subnet = env.get("POSTGRES_TRUST_SUBNET")
        assert trust_subnet is not None, (
            "pg-init.environment.POSTGRES_TRUST_SUBNET must be declared "
            "so the trust range is overridable per deploy (issue #97)."
        )
        assert "172.16.0.0/12" in str(trust_subnet), (
            f"POSTGRES_TRUST_SUBNET default must be 172.16.0.0/12 (Docker "
            f"bridge range), got: {trust_subnet!r} (issue #97)"
        )

    def test_bot_depends_on_pg_init_completed(self) -> None:
        """alphard-bot must wait for pg-init to finish before starting,
        otherwise the first auth_probe runs before the trust line
        is injected and silently falls back to scram auth.

        BUGFIX (#120): Portainer standalone (compose up directly via
        Docker socket) requires depends_on as an ARRAY, not a map with
        conditions. We accept both forms here: array is the Portainer
        canonical form, map is the Compose-CLI canonical form.
        """
        data = _load_compose()
        bot = data["services"]["alphard-bot"]
        deps = bot.get("depends_on", [])
        if isinstance(deps, dict):
            assert deps.get("pg-init", {}).get("condition") == ("service_completed_successfully"), (
                "alphard-bot.depends_on.pg-init.condition must be "
                "service_completed_successfully so the trust line is "
                "applied before the bot tries to connect"
            )
        else:
            assert "pg-init" in deps, (
                "alphard-bot must depend on pg-init so the trust line is " "applied before the bot tries to connect"
            )

    def test_bot_depends_on_postgres_health(self) -> None:
        # BUGFIX (#120): see comment above — accept array or map form.
        data = _load_compose()
        bot = data["services"]["alphard-bot"]
        deps = bot.get("depends_on", [])
        assert "postgres" in deps or "postgres" in (
            deps if isinstance(deps, dict) else deps
        ), "alphard-bot must depend_on postgres"
        if isinstance(deps, dict):
            assert deps["postgres"].get("condition") == "service_healthy"

    def test_bot_env_file_override(self) -> None:
        """BUGFIX (#84): alphard-bot must pass an explicit ENV_FILE env var
        to entrypoint.sh. Without it, entrypoint.sh falls back to the
        bind-mounted /run/secrets/alphard.{env,_env} candidates, which on
        .107 Docker 29.1.x resolve to empty directories when the source
        path is /root/.env-as-directory (production bug 2026-08-20).

        The compose value MUST be the short default `/root/.env` (11 chars
        including slash — well under the 60-char Portainer Env-parameter
        limit), so that even when the host file is missing the entrypoint
        fails fast with a clear "file not found" rather than silently
        loading nothing and crashlooping on missing TINKOFF_* tokens.
        """
        data = _load_compose()
        bot = data["services"]["alphard-bot"]
        env = bot.get("environment", {})
        # YAML may load bare keys as strings or as None (for `KEY:`).
        # Normalize: env could be a list of "KEY=value" strings too.
        if isinstance(env, list):
            env_map = {}
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, _, v = item.partition("=")
                    env_map[k] = v
                elif isinstance(item, str):
                    env_map[item] = None
            env = env_map
        env_file = env.get("ENV_FILE")
        assert env_file is not None, (
            "alphard-bot.environment.ENV_FILE must be declared so entrypoint.sh "
            "knows where to source TINKOFF_* tokens when the bind-mounted "
            "candidates resolve to empty directories on .107 Docker 29.1.x"
        )
        # Either default to /root/.env or override via host .env — both are
        # acceptable; the constraint is just that SOME path is passed.
        assert isinstance(env_file, str) and env_file.strip(), f"ENV_FILE must be a non-empty string, got: {env_file!r}"
        # The Portainer Env-parameter 60-char limit: Tinkoff sandbox tokens
        # are 64+ chars and CANNOT live here. We only put the short PATH
        # in Portainer Env; the long token values live in the .env body.
        assert len(env_file) <= 60, (
            f"ENV_FILE value must fit the 60-char Portainer Env-parameter "
            f"limit (long tokens belong in the file body); got {len(env_file)} chars: {env_file!r}"
        )

    # ------------------------------------------------------------------
    # Issue #120 (Defect 4): regression coverage for /app/logs mount
    # ------------------------------------------------------------------
    #
    # PR #119 replaced the bind-mount for /app/logs with a 100M tmpfs to
    # work around the .107 PVE LXC userns-mapping bug. The fix resolved
    # the immediate restart-loop but introduced three latent defects and
    # one missing tracking issue (see issue #120 body). These tests guard
    # the volumes block so a future PR cannot silently regress to either
    # the original bind-mount (re-introducing the .107 nobody-leaf bug)
    # or to a tmpfs size small enough to re-introduce the ENOSPC
    # restart-loop that PR #119 was merged to eliminate.
    #
    # The tmpfs mount + size requirement will become obsolete once we
    # restore the bind-mount on a non-LXC Docker host (tracked separately
    # — see Defect 3). At that point these tests must be flipped to
    # assert the bind-mount instead of the tmpfs, with a fresh
    # `tests/test_compose_structure.py::test_alphard_bot_no_legacy_bind_mount_on_logs`
    # (added below) keeping the regression guard in the other direction.

    def _bot_volumes(self) -> list:
        """Return alphard-bot.volumes as a list of mount-spec dicts.

        Compose accepts two shapes for each volume entry:

        * short form (string)        : "/host/path:/container/path[:ro]"
        * long form (dict with keys  : type/bind/tmpfs/source/target/read_only
          ``type: bind|tmpfs|volume``)
        """
        data = _load_compose()
        return list(data["services"]["alphard-bot"].get("volumes", []))

    def test_alphard_bot_logs_is_tmpfs(self) -> None:
        """Issue #120 (Defect 4a): alphard-bot must mount /app/logs as a
        tmpfs (PR #119 work-around for .107 PVE LXC userns-mapping). A
        bind-mount here leaves the leaf owned by userns-mapped nobody
        (uid 65534), which neither the container's root user nor
        CAP_CHOWN can change — the original .107 restart-loop bug.

        Regression guard: a future PR that drops this mount (or replaces
        it with a bind-mount) re-introduces the .107 failure mode on
        every restart.
        """
        volumes = self._bot_volumes()
        tmpfs_mounts = [
            v for v in volumes if isinstance(v, dict) and v.get("type") == "tmpfs" and v.get("target") == "/app/logs"
        ]
        assert tmpfs_mounts, (
            "alphard-bot must mount /app/logs as a tmpfs (PR #119, issue "
            "#108 + #120). Found volumes: "
            f"{volumes!r}"
        )

    def test_alphard_bot_logs_tmpfs_size_documented(self) -> None:
        """Issue #120 (Defect 4b): the tmpfs mount for /app/logs MUST
        declare an explicit ``size`` — and the size must be large enough
        to comfortably hold the backfill log ceiling (10 MiB active × 4
        = 40 MiB from issue #120 Defect 1's RotatingFileHandler) plus
        headroom for handler startup output, but small enough that a
        runaway log cannot grow the container memory footprint without
        bound.

        We assert ``size`` is a positive byte quantity and parses cleanly;
        we do NOT pin the exact byte value because the operator may want
        to tune it without a code review (e.g. on a 1 GiB-RAM LXC). The
        lower bound of 40 MiB matches the rotation ceiling so the
        container can never ENOSPC mid-rotation.
        """
        import re

        volumes = self._bot_volumes()
        tmpfs_mounts = [
            v for v in volumes if isinstance(v, dict) and v.get("type") == "tmpfs" and v.get("target") == "/app/logs"
        ]
        assert tmpfs_mounts, "missing /app/logs tmpfs mount (regression — see test_alphard_bot_logs_is_tmpfs)"
        spec = tmpfs_mounts[0].get("tmpfs", {})
        size_raw = spec.get("size")
        assert size_raw, (
            f"/app/logs tmpfs must declare an explicit `size` to prevent "
            f"unbounded growth on .107 LXC; got tmpfs={spec!r}"
        )
        m = re.fullmatch(r"\s*(\d+)\s*([KMG]?)\s*", str(size_raw))
        assert m, f"/app/logs tmpfs.size must be a Docker byte-quantity (e.g. '100M'), got: {size_raw!r}"
        n = int(m.group(1))
        unit = m.group(2) or ""
        factor = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024**3}[unit]
        bytes_ = n * factor
        # Lower bound: must comfortably exceed the rotation ceiling
        # (10 MiB active × 4 backups = 40 MiB), so a misconfigured shrink
        # back to "10M" (which would re-introduce the original ENOSPC
        # restart-loop) fails loudly here.
        assert bytes_ >= 40 * 1024 * 1024, (
            f"/app/logs tmpfs size must be ≥ 40 MiB to hold the rotated "
            f"backfill log (issue #120 Defect 1); got {size_raw} = {bytes_} bytes"
        )

    def test_alphard_bot_no_legacy_bind_mount_on_logs(self) -> None:
        """Issue #120 (Defect 4c — forward-looking): once we restore the
        bind-mount on a non-LXC host, this test must stay green in the
        OPPOSITE direction: it asserts there is no surviving bind-mount
        on /app/logs until the follow-up issue (issue #121, see Defect 3)
        is filed and worked.

        The legacy short-form bind-mounts we explicitly forbid here are
        ``/mnt/appdata/alphard/logs:/app/logs`` and
        ``/root/.env-as-directory:...`` style — any string mount whose
        target is ``/app/logs``. Long-form bind mounts with
        ``type: bind, target: /app/logs`` are likewise forbidden.
        """
        volumes = self._bot_volumes()
        for v in volumes:
            if isinstance(v, str) and ":" in v:
                src, _, dst = v.partition(":")
                # Allow read-only host bind-mounts of the .env file
                # etc., but never to /app/logs (issue #108/#120).
                assert dst.split(":")[0] != "/app/logs", (
                    f"legacy bind-mount to /app/logs must NOT survive "
                    f"(re-introduces .107 userns-mapping bug): {v!r}"
                )
            elif isinstance(v, dict) and v.get("target") == "/app/logs":
                assert v.get("type") != "bind", (
                    f"legacy bind-mount to /app/logs must NOT survive "
                    f"(re-introduces .107 userns-mapping bug): {v!r}"
                )
