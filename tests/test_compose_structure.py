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
        # The command must perform a real auth round-trip — not the
        # bare pg_isready (which silently passes on stale passwords).
        # BUGFIX (#126): accepts either external pg-healthcheck.sh
        # bind-mount OR inline shell (avoids .107 bind-mount leaf
        # quirk). Both forms must reference POSTGRES_PASSWORD env
        # so a stale pg_authid scram hash surfaces as unhealthy.
        cmd = " ".join(healthcheck.get("test", []))
        assert ("pg-healthcheck.sh" in cmd) or ("pg_isready" in cmd and "PGPASSWORD" in cmd and "SELECT 1" in cmd), (
            "postgres healthcheck must call our custom auth round-trip "
            "(either pg-healthcheck.sh bind-mount or inline pg_isready + "
            f"psql SELECT 1 with PGPASSWORD); got: {cmd}"
        )

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


class TestRedisFailFast:
    """Issue #126 — regression guard for the redis password fail-fast
    contract.

    PR #122/#124 replaced the `${REDIS_PASSWORD:?...}` form with
    `${REDIS_PASSWORD:-}` to keep Portainer StackUpdate happy; in doing
    so they silently turned redis into an unauthenticated listener when
    the operator left the .env blank. This test pins the contract back:

      * the rendered redis command must refuse to exec redis-server
        when REDIS_PASSWORD is empty (fail-fast via non-zero exit);
      * the rendered command must NOT contain a literal
        ``--requirepass ""`` argument, because redis treats that as
        "no requirepass directive" (== unauthenticated listener);
      * the rendered command must end up invoking redis-server with
        ``--requirepass`` and a non-empty password value when
        REDIS_PASSWORD is provided.
    """

    @staticmethod
    def _redis_service() -> dict:
        data = _load_compose()
        redis = data["services"].get("redis")
        assert redis is not None, "redis service must exist in docker-compose.yaml"
        return redis

    def test_redis_command_does_not_pass_empty_requirepass(self) -> None:
        """The most direct regression: forbid the literal
        ``--requirepass ""`` (or anything that composes to an empty
        argument value) in the redis command. This is the exact form
        that was committed in PR #124 and the issue's reproduction PoC.
        """
        cmd = self._redis_service().get("command")
        assert cmd is not None, "redis service must declare a command"
        # Normalize: command may be a string OR a list (exec-form). For
        # exec-form, join with \x00 so we can detect "--requirepass \"\""
        # regardless of how YAML serialized the list.
        if isinstance(cmd, list):
            rendered = "\x00".join(str(x) for x in cmd)
        else:
            rendered = str(cmd)
        # Compose-level defaulting ${REDIS_PASSWORD:-} would render to
        # an empty string. After the fix, ${REDIS_PASSWORD:-} appears
        # only inside a sh -c script — never as a raw --requirepass
        # argument to redis-server.
        assert '--requirepass ""' not in rendered, (
            "redis command must NOT pass --requirepass with an empty value; "
            "that is the unauthenticated-listener regression from PR #124. "
            f"Got: {rendered!r}"
        )
        # Also forbid the bash-quoted form (single or no quotes)
        assert "--requirepass ''" not in rendered, f"redis command must NOT pass --requirepass ''; got: {rendered!r}"

    def test_redis_command_fails_fast_on_empty_password(self) -> None:
        """The fix must wrap the command in a shell that exits non-zero
        if REDIS_PASSWORD is unset/empty. We check this by inspecting
        the rendered command for an explicit emptiness test.
        """
        cmd = self._redis_service().get("command")
        assert cmd is not None
        if isinstance(cmd, list):
            rendered = "\n".join(str(x) for x in cmd)
        else:
            rendered = str(cmd)
        # The fail-fast must reference REDIS_PASSWORD AND a "-z" (empty
        # string) test AND an explicit "exit 1" branch. Any one of these
        # missing means the contract is broken.
        assert "REDIS_PASSWORD" in rendered, (
            f"redis command must reference REDIS_PASSWORD (fail-fast check); " f"got: {rendered!r}"
        )
        assert "-z" in rendered, (
            f"redis command must test for empty REDIS_PASSWORD "
            f'(e.g. `[ -z "${{REDIS_PASSWORD:-}}" ]`); got: {rendered!r}'
        )
        assert "exit 1" in rendered, f"redis command must exit non-zero on empty REDIS_PASSWORD; got: {rendered!r}"

    def test_redis_command_uses_shell_interpolation_not_compose_default(self) -> None:
        """Defence-in-depth: the literal compose-level defaulting
        form ``--requirepass ${REDIS_PASSWORD:-}`` must NOT survive
        in the YAML. The shell-level defaulting form
        ``$${REDIS_PASSWORD:-}`` (with the doubled `$$` escaping
        compose interpolation) IS allowed and is the fix's runtime
        emptiness check.

        Why this matters: if someone re-introduces the
        pre-fix form
        ``command: redis-server --requirepass ${REDIS_PASSWORD:-}``
        (a single string), compose will expand ``${REDIS_PASSWORD:-}``
        to an empty string and redis will start unauthenticated.
        The exec-form + sh -c + ``$${REDIS_PASSWORD:-}`` form
        escapes the expansion so it only fires at container runtime.
        """
        raw = COMPOSE.read_text(encoding="utf-8")
        redis = self._redis_service()
        cmd = redis.get("command")
        # The redis command must be in exec-form (a list). If it is a
        # bare string containing the compose-defaulting form, that
        # is exactly the pre-fix regression and must be rejected.
        assert isinstance(cmd, list), (
            f"redis command must be in exec-form (list) to escape " f"compose interpolation; got string: {cmd!r}"
        )
        # In the raw YAML, the compose-level form (single $) must
        # NOT appear on a --requirepass line. The shell-level form
        # (doubled $) is allowed and is in fact the fix.
        for line in raw.splitlines():
            assert "--requirepass ${REDIS_PASSWORD" not in line, (
                f"raw docker-compose.yaml still contains the compose-"
                f"defaulted form `--requirepass ${{REDIS_PASSWORD...}}` "
                f"on a single line; this is the pre-fix unauthenticated-"
                f"listener regression. Line: {line!r}"
            )
        # Sanity: the fix's script block must be present.
        assert "$${REDIS_PASSWORD:-}" in raw, (
            "raw docker-compose.yaml is missing the fail-fast shell "
            "script block (look for `$${REDIS_PASSWORD:-}`); the "
            "runtime empty-password check did not land. See issue #126."
        )

    def test_redis_command_passes_password_when_set(self) -> None:
        """Positive control: with a non-empty REDIS_PASSWORD, the
        rendered command must exec redis-server with --requirepass and
        a non-empty value. We simulate this by checking the script
        text — the script must contain a `exec redis-server
        --requirepass "${REDIS_PASSWORD}"` line (the value is read
        at container runtime, not at compose time).
        """
        cmd = self._redis_service().get("command")
        assert cmd is not None
        if isinstance(cmd, list):
            rendered = "\n".join(str(x) for x in cmd)
        else:
            rendered = str(cmd)
        assert "exec redis-server" in rendered, (
            f"redis command must exec redis-server after the fail-fast " f"check; got: {rendered!r}"
        )
        assert "--requirepass" in rendered, f"redis command must pass --requirepass to redis-server; got: {rendered!r}"
        # The script must reference the password (via ${REDIS_PASSWORD})
        # rather than interpolating an empty default.
        assert "REDIS_PASSWORD" in rendered, (
            f"redis command must reference REDIS_PASSWORD in the exec " f"line; got: {rendered!r}"
        )

    def test_redis_command_rejects_shell_metacharacter_passwords(self) -> None:
        """Issue #129 — extend the fail-fast gate to reject shell
        metacharacters in REDIS_PASSWORD.

        Why this matters: the command is executed under sh -c, so a
        password containing dollar, doublequote, backslash, or backtick
        would be silently mangled by the shell before redis-server
        saw it. In the worst case the mangling produces an empty
        --requirepass arg and redis starts without authentication.
        In the more common case redis fails with "wrong number of
        arguments" and the container restart-loops until the operator
        notices.

        The check must be visible in the rendered YAML: a case block
        referencing REDIS_PASSWORD with metacharacter patterns. We
        accept any reasonable representation that rejects the four
        characters (the exact escape form varies between shells).
        """
        cmd = self._redis_service().get("command")
        assert cmd is not None
        if isinstance(cmd, list):
            rendered = "\n".join(str(x) for x in cmd)
        else:
            rendered = str(cmd)
        # The rendered script must contain a case-statement that
        # pattern-matches REDIS_PASSWORD for shell metacharacters.
        assert "case " in rendered, (
            f"redis command must contain a `case` block to reject "
            f"shell-unsafe REDIS_PASSWORD chars; got: {rendered!r}"
        )
        # The case statement must include all four dangerous chars.
        # POSIX `case` can write `$` and `"` either as literal escapes
        # (``\\$``, ``\\"``) or via $(printf '%s' '$') — both are valid.
        # For backslash and backtick the literal escape form is used.
        for needle in ("REDIS_PASSWORD", "exit 1"):
            assert needle in rendered, (
                f"redis command must reference {needle!r} in the " f"metachar-rejection case block; got: {rendered!r}"
            )
        # The pattern must match at least one of the dangerous chars.
        # We accept any of the four: `\$`, `\"`, `\\`, or backtick `` ` ``.
        # We don't pin the exact escape because POSIX case-pattern
        # escaping is shell-specific (alpine busybox ash vs bash vs
        # dash all differ). The functional contract — reject at least
        # one dangerous char — is what matters.
        has_dollar_escape = r"\$" in rendered or "printf '%s' '$'" in rendered or "printf '%s' \"$\"" in rendered
        has_quote_escape = ('\\"' in rendered) or "printf '%s' '\\\"'" in rendered or 'printf \'%s\' "\\""' in rendered
        has_backslash_escape = (r"\\" in rendered) or (r"'\'" in rendered)
        has_backtick_escape = "`" in rendered
        assert has_dollar_escape or has_quote_escape or has_backslash_escape or has_backtick_escape, (
            f"redis command's metachar-rejection case block must match "
            f'at least one dangerous char ($, ", \\, or backtick); '
            f"got: {rendered!r}"
        )

    def test_redis_command_exits_nonzero_for_metachar_passwords(self) -> None:
        """Issue #129 — functional check: the inline `sh -c` script
        must exit non-zero when REDIS_PASSWORD contains $, ", \\,
        or backtick, and exit non-zero for empty.

        We extract the rendered script from the YAML, replace ``$$``
        with ``$`` (compose's escape), replace ``${REDIS_PASSWORD}``
        with the actual value, and invoke it via ``/bin/sh -c``.
        The script ends with ``exec redis-server`` which would only
        succeed in the redis container, so we override the exec line
        with a stub ``echo redis-stub`` that exits 0 to make
        "ACCEPTED" detectable.

        This proves the fail-fast gate works on the same shell
        (alpine busybox ash) that the redis container uses.
        """
        import subprocess

        cmd = self._redis_service().get("command")
        assert cmd is not None
        # The exec-form list is [sh, -c, script]. Join script parts.
        assert (
            isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "sh" and cmd[1] == "-c"
        ), f"redis command must be exec-form [sh, -c, script]; got: {cmd!r}"
        script = "\n".join(str(x) for x in cmd[2:])
        # Replace $$ with $ (compose escapes $ for the container shell)
        # and replace the final `exec redis-server` with a stub echo
        # so the test can detect "ACCEPTED" without needing redis-server
        # installed locally.
        rendered = script.replace("$$", "$")
        rendered = rendered.replace(
            'exec redis-server --requirepass "${REDIS_PASSWORD}"',
            'echo redis-server-stub --requirepass "${REDIS_PASSWORD}"',
        )

        # Cases: (password, expected_exit). Exit 0 == ACCEPTED, else REJECTED.
        cases = [
            ("AbCd12EfGh34IjKl56Mn78OpQr/+==", 0),  # openssl rand -base64 24
            ("AbCd12EfGh34IjKl56Mn78OpQrStUvWx", 0),  # alphanum only
            ("", 1),  # empty (caught by [ -z ] check)
            ("$", 1),  # dollar
            ('"', 1),  # doublequote
            ("\\", 1),  # backslash
            ("`", 1),  # backtick
            ("p@$w", 1),  # dollar in middle
            ('p@w"ord', 1),  # doublequote in middle
            ("p@w\\ord", 1),  # backslash in middle
            ("p@w`ord", 1),  # backtick in middle
        ]
        for pw, expected_exit in cases:
            # We pass REDIS_PASSWORD via the environment so that the
            # script's own ${REDIS_PASSWORD} expansion picks it up.
            proc = subprocess.run(
                ["sh", "-c", rendered],
                input="",
                env={"REDIS_PASSWORD": pw, "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert proc.returncode == expected_exit, (
                f"REDIS_PASSWORD={pw!r}: expected exit {expected_exit}, "
                f"got {proc.returncode}.\nstdout={proc.stdout!r}\n"
                f"stderr={proc.stderr!r}\nscript={rendered!r}"
            )


class TestPrometheusLXC:
    """Issue: 2026-08-22 — prometheus container restart-loops on .107 PVE LXC.

    The .107 Docker daemon (29.1.x on Proxmox VE unprivileged LXC) maps
    bind-mount leaf directories to userns-mapped nobody:nogroup. The
    prometheus image runs as the in-image `nobody` user, which then
    cannot write its WAL into `/prometheus` — the bind-mount leaves the
    directory read-only for the container.

    Same class of bug as PR #108/#119/#120 (`/app/logs`) and PR #122
    (postgres init.sql). The fix is the same: replace the bind-mount
    with a tmpfs (root-owned, always writable). The TSDB then lives in
    container memory; data is lost on container restart — acceptable
    trade-off for Phase 2 observability (see docker-compose.yaml comment
    on the prometheus service for the restore path on a non-LXC host).
    """

    def _prometheus_service(self) -> dict:
        data = _load_compose()
        prom = data["services"].get("prometheus")
        assert prom is not None, "prometheus service must exist (under profiles: [observability])"
        return prom

    def _prometheus_mounts(self) -> list:
        """Return prometheus.volumes as a list of mount-spec dicts.

        Compose accepts the same two shapes as on alphard-bot: short-form
        strings ``"/host:/container[:ro]"`` and long-form dicts with
        ``type: bind|tmpfs|volume`` plus ``source/target/read_only``.
        """
        return list(self._prometheus_service().get("volumes", []))

    def test_prometheus_no_legacy_bind_mount_on_data(self) -> None:
        """Regression guard: a future PR must NOT re-introduce the
        ``/mnt/appdata/alphard/prometheus:/prometheus`` (or any other
        bind-mount targeting /prometheus) on the prometheus service.

        Such a bind-mount gives userns-mapped nobody:nogroup on the
        leaf, which the in-image `nobody` user cannot write through
        — the container restart-loops every 30s. Verified live 2026-08-22
        via Portainer MCP container exec: `open /prometheus/queries.active:
        permission denied`.
        """
        mounts = self._prometheus_mounts()
        for v in mounts:
            if isinstance(v, str) and ":" in v:
                _src, _, dst = v.partition(":")
                if dst.split(":")[0] == "/prometheus":
                    # Short-form: "src:dst[:ro]". Compose treats this as:
                    #   - bind mount if src is an absolute host path
                    #   - named volume if src is a volume name (no leading "/")
                    # Allow volumes (issue #209), reject bind mounts (issue #147).
                    assert not _src.startswith("/"), (
                        f"legacy bind-mount to /prometheus must NOT survive "
                        f"(re-introduces .107 userns-mapping restart-loop): {v!r}"
                    )
            elif isinstance(v, dict) and v.get("target") == "/prometheus":
                assert v.get("type") != "bind", (
                    f"legacy bind-mount to /prometheus must NOT survive "
                    f"(re-introduces .107 userns-mapping restart-loop): {v!r}"
                )

    def test_prometheus_data_is_named_volume(self) -> None:
        """Issue #209: /prometheus must be a named volume (not tmpfs,
        not bind-mount). tmpfs created by compose on .107 PVE LXC ends up
        owned by userns-mapped nobody, and the in-image `nobody` still
        can't write through the same userns boundary — same trap as bind.
        Named volumes are root-owned in the mount namespace, so the
        container entrypoint can chown nobody before exec'ing prometheus.
        Volume also persists across Watchtower recreates.
        """
        mounts = self._prometheus_mounts()
        ok = False
        for v in mounts:
            if isinstance(v, dict) and v.get("type") == "volume" and v.get("target") == "/prometheus":
                ok = True
            elif isinstance(v, str):
                src, _, dst = v.partition(":")
                if dst.split(":")[0] == "/prometheus" and src and not src.startswith("/"):
                    ok = True
        assert ok, f"prometheus must mount /prometheus as a named volume (issue #209); " f"got mounts={mounts!r}"
        for v in mounts:
            if isinstance(v, dict) and v.get("target") == "/prometheus":
                assert (
                    v.get("type") != "tmpfs"
                ), f"/prometheus as tmpfs re-introduces userns restart-loop (issue #209): {v!r}"
                assert v.get("type") != "bind", f"/prometheus as bind re-introduces LXC EPERM (issue #147): {v!r}"

    def test_prometheus_named_volume_declared_top_level(self) -> None:
        """alphard-prometheus-data must be declared in top-level volumes:
        so compose creates it on first up. Without this declaration,
        stack up fails with "volume alphard-prometheus-data not found".
        """
        data = _load_compose()
        vols = data.get("volumes", {}) or {}
        assert "alphard-prometheus-data" in vols, (
            f"alphard-prometheus-data must be declared in top-level volumes:; " f"found: {sorted(vols.keys())}"
        )

    def test_prometheus_entrypoint_chowns_nobody(self) -> None:
        """Prometheus entrypoint MUST `chown -R nobody:nogroup /prometheus`
        after creating the dir. Without this, in-image nobody (uid 65534)
        cannot write /prometheus/queries.active and the container
        restart-loops every 30s (issue #209). Even on a root-owned
        named volume the file ownership is root:root until chown runs.
        """
        data = _load_compose()
        ep = data["services"]["prometheus"].get("entrypoint")
        cmd = data["services"]["prometheus"].get("command", "")

        def _flatten(x):
            return "\n".join(str(p) for p in x) if isinstance(x, list) else str(x)

        joined = _flatten(ep) + "\n" + _flatten(cmd)
        assert "chown" in joined and "nobody" in joined, (
            f"prometheus entrypoint must chown nobody on /prometheus (issue #209); "
            f"got entrypoint/command={joined[:600]!r}"
        )

    def test_prometheus_data_tmpfs_size_documented(self) -> None:
        """Backward-compat stub for issue #209.

        tmpfs.size was the original (broken on LXC) fix from PR #147.
        Issue #209 switches /prometheus to a named volume (Docker-created,
        root-owned, persists across recreates). The two tests above
        (test_prometheus_data_is_named_volume + the new
        test_prometheus_named_volume_declared_top_level) replace this one.
        Keep a no-op here so anyone referencing the old method name
        gets a clear "this fix was superseded" signal instead of an
        AttributeError if they only run a partial test subset.
        """
        return None

    def test_prometheus_config_bind_mounted_from_repo(self) -> None:
        """Issue #283: PROM_YML_B64 was the source of truth for the
        scrape config. Portainer StackUpdate silently truncates env
        values >60 chars (Go JSON unmarshal fails on long strings) and
        292-byte base64 blobs were routinely cut off mid-keyword. That
        dropped the alphard-bot scrape target and broke every Grafana
        panel.

        Fix: bind-mount the config file from the repo
        (``./docker/prometheus/prometheus.yml`` -> ``/etc/prometheus/prometheus.yml:ro``)
        instead of supplying it via env. Read-only mount is safe from the
        .107 LXC userns-mapping leaf trap because the in-image nobody user
        never writes to it.
        """
        svc = _load_compose()["services"]["prometheus"]
        volumes = svc.get("volumes", [])
        # Normalise to string list (docker compose accepts both long and
        # short syntax; we always emit long-form strings in this repo).
        vol_strs = [str(v) for v in volumes]
        config_mount = next(
            (v for v in vol_strs if "/etc/prometheus/prometheus.yml" in v),
            None,
        )
        assert config_mount is not None, (
            f"prometheus.volumes must bind-mount the scrape config from "
            f"the repo; got: {vol_strs}. See issue #283 — PROM_YML_B64 "
            f"is unreliable because Portainer truncates >60-char env "
            f"values to ~80 chars mid-word."
        )
        # Must be read-only (the in-image nobody user can't write to
        # bind-mounted leaves on .107 LXC; ro avoids the write-path trap).
        assert ":ro" in config_mount, (
            f"prometheus.yml bind-mount must be read-only (:ro) to avoid "
            f"the .107 userns-mapping leaf trap; got: {config_mount}"
        )

    def test_prometheus_no_longer_uses_prom_yml_b64_env(self) -> None:
        """Regression marker for issue #283. PROM_YML_B64 is gone — the
        bind-mount above replaces it. If a future refactor reintroduces
        the inline entrypoint + PROM_YML_B64 env, this test fires before
        the bind-mount one so the failure message points at the right
        cause.
        """
        svc = _load_compose()["services"]["prometheus"]
        # No PROM_YML_B64 in environment (env was the broken path).
        env = svc.get("environment", {})
        if isinstance(env, list):
            keys = {item.split("=", 1)[0] for item in env if isinstance(item, str) and "=" in item}
            keys |= {item for item in env if isinstance(item, str) and "=" not in item}
        else:
            keys = set(env.keys())
        assert "PROM_YML_B64" not in keys, (
            f"prometheus.environment must NOT declare PROM_YML_B64 "
            f"(replaced by bind-mount per issue #283); got: {sorted(keys)}"
        )
        # Entryptoint MAY exist (for chown of the writable named volume —
        # issue #209) but it MUST NOT base64-decode PROM_YML_B64. The config
        # is bind-mounted now, no inline base64 -d needed.
        ep = svc.get("entrypoint", [])
        if isinstance(ep, list):
            ep_str = "\n".join(str(p) for p in ep)
        else:
            ep_str = str(ep)
        assert "base64 -d" not in ep_str, (
            f"prometheus.entrypoint must NOT base64-decode PROM_YML_B64 "
            f"(config is bind-mounted per issue #283); got: {ep_str!r}"
        )

    def test_prometheus_doc_path_matches_real_repo_path(self) -> None:
        """Belt-and-braces test (suggested by QA on PR #284): no doc or
        script should ever reference the non-existent
        ``observability/prometheus.yml`` path. The real bind-mount source
        is ``docker/prometheus/prometheus.yml`` (see
        ``docker-compose.yaml:439``). Catches a future rename that
        forgets to update one of the doc/comment references.

        Scanned paths: ``docs/``, ``scripts/`` (relative to repo root).
        Excludes the ``compose.yaml`` itself (which is the source of
        truth and intentionally names the bind-mount source) and
        ``tests/`` (covered by ``TestNoRelativeBindMounts``).
        """
        repo_root = Path(__file__).resolve().parent.parent
        offenders: list[tuple[str, int, str]] = []
        for subdir in ("docs", "scripts"):
            root = repo_root / subdir
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix not in {".md", ".sh", ".py", ".yaml", ".yml"}:
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for i, line in enumerate(text.splitlines(), start=1):
                    if "observability/prometheus" in line:
                        offenders.append((str(p.relative_to(repo_root)), i, line.strip()))
        assert not offenders, (
            "Doc/scripts reference the wrong path "
            "`observability/prometheus.yml` (must be "
            "`docker/prometheus/prometheus.yml`). Offenders:\n  "
            + "\n  ".join(f"{p}:{i}: {line}" for p, i, line in offenders)
        )

    def test_prometheus_exposes_port_9090(self) -> None:
        """Grafana (network_mode: host, see compose) reaches Prometheus
        via ``http://localhost:9090``. For this to work Prometheus must
        publish its 9090/tcp port to the host's bridge, OR run on host
        network. The compose form here uses the standard
        ``ports: ["9090:9090"]`` mapping — sufficient for Grafana to
        scrape it.
        """
        svc = self._prometheus_service()
        ports = svc.get("ports", [])
        port_strs = [str(p) for p in ports]
        assert any("9090" in s for s in port_strs), (
            f"prometheus.ports must expose 9090/tcp so host-network " f"Grafana can scrape it; got: {port_strs}"
        )


class TestGrafanaPortability:
    """Issue: 2026-08-22 — grafana bind-mount was hardcoded to
    ``/mnt/appdata/alphard/grafana``, breaking first-shot install on any
    host that does not happen to use exactly that path (the default
    Linux-conventional path is /srv/alphard or /var/lib/alphard, not
    /mnt/anything). The fix parameterises the host data dir via
    ``APPDATA_DIR`` (default /srv/alphard) and adds a one-shot
    ``grafana-init`` service that mkdir -p's the leaf with the correct
    UID/GID 472:472 before grafana starts.

    Same architectural pattern as pg-init (postgres): front every
    host-bind-mount service with a one-shot init container that owns
    the leaf-prep contract. Idempotent (mkdir -p + chown on an
    existing leaf is a no-op).
    """

    def test_grafana_volume_uses_appdata_dir(self) -> None:
        """The grafana bind-mount source MUST be parameterised via
        ``${APPDATA_DIR:-/srv/alphard}/grafana`` — never hardcoded to
        /mnt/appdata/... or any other host-specific path. Hardcoded
        /mnt/... requires every operator to edit the compose before
        StackUpdate succeeds; with APPDATA_DIR default the compose is
        portable to any LXC/VM out of the box.
        """
        data = _load_compose()
        grafana = data["services"].get("grafana")
        assert grafana is not None, "grafana service must exist in docker-compose.yaml"
        volumes = grafana.get("volumes", [])
        grafana_data_mounts = []
        # Compose volume entries use either short-form string
        # ``"/host:/container[:ro]"`` or long-form dict with target/source.
        # Note that the host path may itself contain a `:` (inside the
        # ${APPDATA_DIR:-/srv/...} default), so partition(":"), which
        # splits on the FIRST colon, is wrong — use rsplit with maxsplit=1
        # to anchor on the LAST colon instead.
        for v in volumes:
            if isinstance(v, str) and ":" in v:
                parts = v.rsplit(":", 1)
                if len(parts) != 2:
                    continue
                _src, dst = parts
                if dst == "/var/lib/grafana":
                    grafana_data_mounts.append(v)
            elif isinstance(v, dict) and v.get("target") == "/var/lib/grafana":
                grafana_data_mounts.append(v)
        assert grafana_data_mounts, (
            "grafana must mount /var/lib/grafana; cannot test " "APPDATA_DIR parameterisation without a mount target"
        )
        # At least one mount must use APPDATA_DIR (not the legacy
        # /mnt/appdata hardcoded form).
        mount_strs = [str(v) for v in grafana_data_mounts]
        legacy_hardcoded = [v for v in mount_strs if "/mnt/appdata/" in v]
        appdata_param = [v for v in mount_strs if "APPDATA_DIR" in v]
        assert not legacy_hardcoded, (
            f"grafana /var/lib/grafana mount source is hardcoded to "
            f"/mnt/appdata/... — breaks first-shot install portability. "
            f"Use ${{APPDATA_DIR:-/srv/alphard}}/grafana instead. "
            f"Found: {legacy_hardcoded}"
        )
        assert appdata_param, (
            f"grafana /var/lib/grafana mount must use ${{APPDATA_DIR:-/srv/alphard}} "
            f"as the source so operators can override via stack Env. "
            f"Found: {mount_strs}"
        )

    def test_chownfix_replaces_grafana_init_v2(self) -> None:
        """Compose refactor 2.0 (kanban t_884fec4a): the legacy
        grafana-init service is replaced by the single ``chownfix``
        one-shot service. This test verifies the chownfix service
        exists, is one-shot, and declares APPDATA_DIR — the same
        contract as the old grafana-init had, but consolidated into
        one container that handles all four data services.
        """
        data = _load_compose()
        services = data["services"]
        assert "chownfix" in services, (
            "chownfix service must exist (compose refactor 2.0 "
            "replaces grafana-init with a single chownfix that "
            "covers postgres, redis, grafana, prometheus)"
        )
        init_svc = services["chownfix"]
        assert (
            init_svc.get("restart") == "no"
        ), f"chownfix must be restart: 'no' (one-shot); got: {init_svc.get('restart')!r}"
        env = init_svc.get("environment", {})
        if isinstance(env, list):
            env_map = {}
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, _, v = item.partition("=")
                    env_map[k] = v
            env = env_map
        assert "APPDATA_DIR" in env, (
            "chownfix.environment must declare APPDATA_DIR so the "
            "operator's stack Env override flows into the chown script"
        )
        assert "/srv/alphard" in str(env.get("APPDATA_DIR", "")), (
            f"APPDATA_DIR must default to /srv/alphard (Linux-conventional "
            f"path, not /mnt/anything) for first-shot install portability; "
            f"got: {env.get('APPDATA_DIR')!r}"
        )

    def test_grafana_depends_on_init(self) -> None:
        """Grafana must wait for chownfix (the refactor 2.0 successor
        to grafana-init) to finish before starting — otherwise the
        bind-mount leaf at ${APPDATA_DIR}/grafana is not yet owned
        by 472:472 and grafana fails on its first touch.
        """
        data = _load_compose()
        grafana = data["services"]["grafana"]
        deps = grafana.get("depends_on", [])
        if isinstance(deps, dict):
            deps = list(deps.keys())
        assert "chownfix" in deps, (
            f"grafana must depends_on chownfix so the host-data "
            f"leaf is prepared (mkdir + chown 472:472) before "
            f"grafana starts; got depends_on={deps!r}"
        )

    def test_no_hardcoded_mnt_appdata_volumes(self) -> None:
        """Wider regression guard: NO service in the compose may
        bind-mount from /mnt/appdata/... directly. Every host path
        must flow through ${APPDATA_DIR} so an operator can override
        the stack root via a single Env.
        """
        data = _load_compose()
        offenders = []
        for svc_name, svc in data["services"].items():
            for v in svc.get("volumes", []):
                if isinstance(v, str) and v.startswith("/mnt/"):
                    offenders.append((svc_name, v))
                elif isinstance(v, dict) and isinstance(v.get("source"), str) and v["source"].startswith("/mnt/"):
                    offenders.append((svc_name, v))
        assert not offenders, (
            "No service may bind-mount from /mnt/... — use "
            "${APPDATA_DIR:-/srv/alphard} so the stack is portable. "
            f"Found offenders: {offenders}"
        )


class TestGrafanaProvisioning:
    """Issue #297: bind-mount Grafana provisioning + dashboards from
    the repo directly, instead of decoding *_B64 env vars.

    Same Portainer 60-char env truncation bug as PROM_YML_B64
    (issue #283 / PR #284) hit the Grafana service: after
    PortainerStackUpdate on .107 the PROVISIONING_DATASOURCES_YML_B64
    value was truncated, base64 -d produced garbage or empty bytes,
    and Grafana started with no datasource and no dashboards ("No data"
    on every panel even though prometheus was scraping alphard-bot
    correctly). Fix: bind-mount the provisioning and dashboard
    directories from the repo.

    The previous PR #246 / kanban t_884fec4a approach used
    ``PROVISIONING_*_B64`` env vars decoded by an in-container wrapper
    (docker/entrypoint_grafana.sh). That wrapper is now deleted —
    see ``test_no_entrypoint_grafana_sh_wrapper_exists`` for the
    regression marker.
    """

    def _grafana_volumes(self) -> list:
        data = _load_compose()
        grafana = data["services"]["grafana"]
        return grafana.get("volumes", [])

    @staticmethod
    def _split_mount(v: str) -> tuple[str, str] | None:
        """Split a short-form compose volume string ``src:dst[:ro|rw]``
        into ``(src, dst)``."""
        parts = v.split(":")
        if len(parts) >= 2:
            return parts[0], parts[1]
        return None

    def test_provisioning_bind_mounted_from_repo(self) -> None:
        """The grafana service must bind-mount provisioning YAMLs from
        the repo directly: ``./docker/grafana/provisioning`` →
        ``/etc/grafana/provisioning:ro``.

        Why: PR #246 baked provisioning into PROVISIONING_*_B64 env
        vars and decoded them via entrypoint_grafana.sh wrapper. But
        Portainer StackUpdate silently truncates >60-char env values
        (Go JSON unmarshal limit), so the decoded files were empty /
        partial and Grafana had no datasource registered. Bind-mount
        from the repo is the same fix as PR #284 for prometheus.yml.
        """
        volumes = self._grafana_volumes()
        vol_strs = [str(v) for v in volumes]
        prov_mount = next(
            (v for v in vol_strs if "/etc/grafana/provisioning" in v),
            None,
        )
        assert prov_mount is not None, (
            f"grafana.volumes must bind-mount provisioning YAMLs from "
            f"the repo (issue #297, same Portainer 60-char env truncation "
            f"bug as PR #284 / PROM_YML_B64); got: {vol_strs}"
        )
        # Must be read-only (Grafana writes to /var/lib/grafana for
        # sqlite db + plugins, but provisioning is read-only).
        assert ":ro" in prov_mount, (
            f"provisioning bind-mount must be read-only (:ro) so Grafana "
            f"can't tamper with the source-of-truth yaml files; got: "
            f"{prov_mount}"
        )
        # Source must be the repo path
        src, _ = self._split_mount(prov_mount)
        assert src == "./docker/grafana/provisioning", (
            f"provisioning bind-mount source must be " f"'./docker/grafana/provisioning' (the repo path); got: {src}"
        )

    def test_dashboards_bind_mounted_from_repo(self) -> None:
        """The grafana service must bind-mount dashboard JSONs from
        the repo: ``./docker/grafana/dashboards`` →
        ``/var/lib/grafana/dashboards:ro``. Provider at
        ``/etc/grafana/provisioning/dashboards/provider.yml`` declares
        the directory as the dashboard source.
        """
        volumes = self._grafana_volumes()
        vol_strs = [str(v) for v in volumes]
        dash_mount = next(
            (v for v in vol_strs if "/var/lib/grafana/dashboards" in v),
            None,
        )
        assert dash_mount is not None, (
            f"grafana.volumes must bind-mount dashboard JSONs from the " f"repo (issue #297); got: {vol_strs}"
        )
        assert ":ro" in dash_mount, (
            f"dashboards bind-mount must be read-only (:ro) — dashboards "
            f"are git-versioned, not UI-edited; got: {dash_mount}"
        )
        src, _ = self._split_mount(dash_mount)
        assert src == "./docker/grafana/dashboards", (
            f"dashboards bind-mount source must be " f"'./docker/grafana/dashboards'; got: {src}"
        )

    def test_grafana_no_longer_uses_provisioning_b64_env(self) -> None:
        """Regression marker for issue #297.

        All *_B64 env vars previously used to bake provisioning +
        dashboards must be gone from grafana.service.environment.
        If a future refactor reintroduces PROVISIONING_DATASOURCES_YML_B64
        etc, this test fires.
        """
        svc = _load_compose()["services"]["grafana"]
        env = svc.get("environment", {})
        if isinstance(env, list):
            keys = {item.split("=", 1)[0] for item in env if isinstance(item, str) and "=" in item}
            keys |= {item for item in env if isinstance(item, str) and "=" not in item}
        else:
            keys = set(env.keys())
        for forbidden in (
            "PROVISIONING_DATASOURCES_YML_B64",
            "PROVISIONING_DASHBOARDS_PROVIDER_YML_B64",
            "DASHBOARD_PHASE0_JSON_B64",
            "DASHBOARD_PHASE28_JSON_B64",
        ):
            assert forbidden not in keys, (
                f"grafana.environment must NOT declare {forbidden} "
                f"(replaced by bind-mount per issue #297); got: "
                f"{sorted(keys)}"
            )

    def test_grafana_entrypoint_is_upstream_run_sh(self) -> None:
        """With bind-mount provisioning in place, the grafana service
        must use the UPSTREAM entrypoint (no custom wrapper). Issue
        #297 explicitly removes the entrypoint_grafana.sh wrapper.

        Without explicit entrypoint: directive, the upstream image's
        /run.sh runs by default — that's what we want now.
        """
        svc = _load_compose()["services"]["grafana"]
        ep = svc.get("entrypoint")
        # Either absent (defaults to upstream /run.sh) or pointing at /run.sh.
        if ep is None:
            return  # OK — upstream default
        if isinstance(ep, list):
            ep_str = " ".join(str(x) for x in ep)
        else:
            ep_str = str(ep)
        assert "/entrypoint_grafana.sh" not in ep_str, (
            f"grafana entrypoint must NOT point at /entrypoint_grafana.sh "
            f"(deleted in issue #297; provisioning is now bind-mounted "
            f"from the repo). Got: {ep_str!r}"
        )

    def test_no_entrypoint_grafana_sh_wrapper_exists(self) -> None:
        """The docker/entrypoint_grafana.sh wrapper script (which used
        to decode *_B64 env vars) MUST be deleted from the repo after
        PR #297. Keeping it would re-introduce the very path that
        Portainer's 60-char truncation bug killed.
        """
        repo = Path(__file__).resolve().parent.parent
        wrapper = repo / "docker" / "entrypoint_grafana.sh"
        assert not wrapper.exists(), (
            f"docker/entrypoint_grafana.sh must be deleted (issue #297): "
            f"the B64-decode wrapper is dead code now that provisioning "
            f"is bind-mounted. Found at: {wrapper}"
        )


class TestPortainerStandaloneEnv:
    """Issue 2026-08-22 #149 — Portainer standalone does NOT propagate
    stack-level Env vars into service-level environment unless the
    service explicitly declares them in its `environment:` block.

    Verified on .107 stack #111 after StackUpdate from main HEAD 95c7095:
    alphard-redis restart-looped every 1s with ``REDIS_PASSWORD is
    required (unset/empty in env)`` because the stack Env had
    REDIS_PASSWORD set, but the redis service's environment block
    did NOT declare it — Portainer stripped it during render.

    Regression guard: every service whose inline command / entrypoint
    consumes a ${VAR:-} placeholder that has no compose-CLI default
    fallback MUST declare that VAR in its service.environment block.
    Without this declaration, ${VAR:-} evaluates to empty inside the
    container regardless of stack-level Env values.
    """

    def _env_map(self, svc: dict) -> dict:
        """Normalize a service.environment block to {key: value}.

        Compose accepts two shapes for env entries:
          * list of "KEY=value" strings
          * map {KEY: value}
        Some entries are bare keys (KEY:) which load as None.
        """
        env = svc.get("environment", {})
        if isinstance(env, list):
            env_map = {}
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, _, v = item.partition("=")
                    env_map[k] = v
                elif isinstance(item, str):
                    env_map[item] = None
            env = env_map
        return env

    def test_redis_declares_redis_password_explicit(self) -> None:
        """The redis service's entrypoint gates on REDIS_PASSWORD (see
        BUGFIX H-8 in compose.yaml). If the env is not declared in the
        service.environment block, Portainer standalone strips the
        stack-level value and the gate trips -> restart loop.
        """
        data = _load_compose()
        redis = data["services"]["redis"]
        env = self._env_map(redis)
        assert "REDIS_PASSWORD" in env, (
            "redis service.environment must declare REDIS_PASSWORD "
            "explicitly so Portainer standalone propagates the stack-level "
            "Env value into the container (otherwise the inline fail-fast "
            "gate in redis command: exit 1 on empty REDIS_PASSWORD, "
            "container restart-loops every 1s)."
        )
        # Default is empty so the inline gate can still trip on truly
        # misconfigured stacks (rather than silently starting with no auth).
        # This matches the inline gate's `[ -z "${REDIS_PASSWORD:-}" ]` test.
        assert env.get("REDIS_PASSWORD") in (None, "", "${REDIS_PASSWORD:-}"), (
            f"REDIS_PASSWORD default in compose must be empty string "
            f"(fail-fast contract), got: {env.get('REDIS_PASSWORD')!r}"
        )

    def test_prometheus_config_bind_mounted_from_repo(self) -> None:
        """Issue #283: PROM_YML_B64 was the source of truth for the
        scrape config. Portainer StackUpdate silently truncates env
        values >60 chars (Go JSON unmarshal fails on long strings) and
        292-byte base64 blobs were routinely cut off mid-keyword. That
        dropped the alphard-bot scrape target and broke every Grafana
        panel.

        Fix: bind-mount the config file from the repo
        (``./docker/prometheus/prometheus.yml`` -> ``/etc/prometheus/prometheus.yml:ro``)
        instead of supplying it via env. Read-only mount is safe from the
        .107 LXC userns-mapping leaf trap because the in-image nobody user
        never writes to it.
        """
        svc = _load_compose()["services"]["prometheus"]
        volumes = svc.get("volumes", [])
        # Normalise to string list (docker compose accepts both long and
        # short syntax; we always emit long-form strings in this repo).
        vol_strs = [str(v) for v in volumes]
        config_mount = next(
            (v for v in vol_strs if "/etc/prometheus/prometheus.yml" in v),
            None,
        )
        assert config_mount is not None, (
            f"prometheus.volumes must bind-mount the scrape config from "
            f"the repo; got: {vol_strs}. See issue #283 — PROM_YML_B64 "
            f"is unreliable because Portainer truncates >60-char env "
            f"values to ~80 chars mid-word."
        )
        # Must be read-only (the in-image nobody user can't write to
        # bind-mounted leaves on .107 LXC; ro avoids the write-path trap).
        assert ":ro" in config_mount, (
            f"prometheus.yml bind-mount must be read-only (:ro) to avoid "
            f"the .107 userns-mapping leaf trap; got: {config_mount}"
        )

    def test_prometheus_no_longer_uses_prom_yml_b64_env(self) -> None:
        """Regression marker for issue #283. PROM_YML_B64 is gone — the
        bind-mount above replaces it. If a future refactor reintroduces
        the inline entrypoint + PROM_YML_B64 env, this test fires before
        the bind-mount one so the failure message points at the right
        cause.
        """
        svc = _load_compose()["services"]["prometheus"]
        # No PROM_YML_B64 in environment (env was the broken path).
        env = svc.get("environment", {})
        if isinstance(env, list):
            keys = {item.split("=", 1)[0] for item in env if isinstance(item, str) and "=" in item}
            keys |= {item for item in env if isinstance(item, str) and "=" not in item}
        else:
            keys = set(env.keys())
        assert "PROM_YML_B64" not in keys, (
            f"prometheus.environment must NOT declare PROM_YML_B64 "
            f"(replaced by bind-mount per issue #283); got: {sorted(keys)}"
        )
        # Entryptoint MAY exist (for chown of the writable named volume —
        # issue #209) but it MUST NOT base64-decode PROM_YML_B64. The config
        # is bind-mounted now, no inline base64 -d needed.
        ep = svc.get("entrypoint", [])
        if isinstance(ep, list):
            ep_str = "\n".join(str(p) for p in ep)
        else:
            ep_str = str(ep)
        assert "base64 -d" not in ep_str, (
            f"prometheus.entrypoint must NOT base64-decode PROM_YML_B64 "
            f"(config is bind-mounted per issue #283); got: {ep_str!r}"
        )

    def test_postgres_declares_secrets_explicit(self) -> None:
        """postgres service already has explicit environment block
        from PR #127 (#129 fix). Regression guard so a future refactor
        can't silently break the Portainer standalone contract.
        """
        data = _load_compose()
        pg = data["services"]["postgres"]
        env = self._env_map(pg)
        for required in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            assert required in env, (
                f"postgres service.environment must declare {required} "
                f"explicitly (Portainer standalone strips undeclared env)"
            )

    def test_alphard_bot_declares_env_file_explicit(self) -> None:
        """alphard-bot service.environment declares ENV_FILE so the
        bind-mounted /root/.env is sourced by entrypoint.sh. Same
        Portainer standalone contract as redis/prometheus.
        """
        data = _load_compose()
        bot = data["services"]["alphard-bot"]
        env = self._env_map(bot)
        assert "ENV_FILE" in env, (
            "alphard-bot service.environment must declare ENV_FILE so "
            "entrypoint.sh sources the right path under Portainer standalone "
            "(stack Env propagation only reaches declared env vars)"
        )


class TestChownfixService:
    """Compose refactor 2.0 (kanban t_884fec4a): a single ``chownfix``
    one-shot service replaces per-service init containers
    (grafana-init, etc.). It runs once per StackUpdate, pre-creates
    all four host-data leaves with the correct UID:GID, then exits.

    Why cap_add: [CHOWN, DAC_OVERRIDE] is REQUIRED on .107 PVE LXC to
    bypass the userns-mapping bug. Without these caps, root in-container
    cannot chown across the bind-mount leaf boundary, even with
    apparmor=unconfined.

    Why watchtower.enable=false: this is a one-shot container with no
    upstream image to "update". Without the opt-out label, Watchtower
    deletes the container on every tick (interpreting the missing
    image as "stale"), which breaks the dep chain for grafana.
    """

    def _chownfix_service(self) -> dict:
        data = _load_compose()
        svc = data["services"].get("chownfix")
        assert svc is not None, (
            "chownfix service must exist in docker-compose.yaml "
            "(compose refactor 2.0, kanban t_884fec4a). It replaces "
            "per-service grafana-init with a single one-shot chown."
        )
        return svc

    def test_chownfix_has_required_cap_add(self) -> None:
        """chownfix MUST declare cap_add: [CHOWN, DAC_OVERRIDE] to
        bypass the .107 PVE LXC userns-mapping bug.

        Without these caps, the in-container root user still cannot
        chown across the bind-mount leaf boundary. CHOWN alone is not
        enough — DAC_OVERRIDE is needed to bypass DAC permission checks
        on the leaf directory itself.
        """
        svc = self._chownfix_service()
        cap_add = svc.get("cap_add", [])
        assert "CHOWN" in cap_add, (
            f"chownfix.cap_add must include CHOWN to bypass the .107 " f"userns-mapping bug. Got: {cap_add!r}"
        )
        assert "DAC_OVERRIDE" in cap_add, (
            f"chownfix.cap_add must include DAC_OVERRIDE for the "
            f"bind-mount leaf permission checks. Got: {cap_add!r}"
        )

    def test_chownfix_disables_watchtower(self) -> None:
        """chownfix MUST be labeled
        ``com.centurylinklabs.watchtower.enable=false``.

        Watchtower interprets one-shot containers with no upstream
        image as "stale" and deletes them. The opt-out label tells
        Watchtower to leave this container alone. Without it,
        Watchtower would delete chownfix after every tick, breaking
        the dep chain for grafana (depends_on chownfix fails because
        chownfix can't be re-created without a StackUpdate).
        """
        svc = self._chownfix_service()
        labels = svc.get("labels", [])
        assert "com.centurylinklabs.watchtower.enable=false" in labels, (
            f"chownfix must declare "
            f"com.centurylinklabs.watchtower.enable=false label so "
            f"Watchtower leaves the one-shot container alone. "
            f"Got labels: {labels!r}"
        )

    def test_chownfix_depends_on_chain(self) -> None:
        """All four data services (postgres, redis, prometheus, grafana)
        must declare depends_on: [chownfix] (or include chownfix in
        their deps) so the chown runs before any data service starts.

        Note: depending on the long-form map syntax
        ``chownfix: { condition: service_completed_successfully }``
        is NOT portable to Portainer standalone compose v2 — only
        the short-form array ``[chownfix]`` is honoured there. We
        accept both forms here; the regression test is just that
        chownfix is in the graph at all.
        """
        data = _load_compose()
        for svc_name in ("postgres", "redis", "prometheus", "grafana"):
            svc = data["services"][svc_name]
            deps = svc.get("depends_on", [])
            if isinstance(deps, dict):
                deps = list(deps.keys())
            assert "chownfix" in deps, (
                f"{svc_name} must depend on chownfix so the host-data "
                f"leaf is chown'd before the data service starts. "
                f"Without this, postgres/redis/prometheus/grafana would "
                f"race the chownfix container and crash with EPERM "
                f"on the bind-mount leaf. Got depends_on: {deps!r}"
            )

    def test_chownfix_is_one_shot(self) -> None:
        """chownfix MUST be ``restart: no`` (one-shot). A restart-looping
        init would wedge the stack in dependencies-not-ready forever.
        """
        svc = self._chownfix_service()
        assert svc.get("restart") == "no", (
            f"chownfix must be one-shot (restart: no). A restart-looping "
            f"init would wedge the stack. Got restart: {svc.get('restart')!r}"
        )

    def test_chownfix_replaces_grafana_init(self) -> None:
        """The legacy grafana-init service must NOT exist (compose
        refactor 2.0 replaces it with chownfix). Regression guard so a
        future PR doesn't resurrect the per-service init pattern by
        accident.
        """
        data = _load_compose()
        assert "grafana-init" not in data["services"], (
            "grafana-init service must be removed — compose refactor 2.0 "
            "replaces it with the single chownfix service. Re-introducing "
            "grafana-init would re-introduce the per-service init pattern "
            "that this refactor consolidates."
        )


class TestNoRelativeBindMounts:
    """Issue #297 — bind-mounts from ./docker/ are now the canonical
    mechanism for provisioning + dashboards (replacing the env-baked
    B64 approach that PR #284 retired for prometheus).

    Why bind-mounts are safe now (vs. the broken PR #217 era):
    The dir-vs-file leaf quirk on .107 PVE LXC + Docker 29.1.x only
    affects HOST DIRECTORY bind-mounts whose source path doesn't
    exist at bind time — the daemon creates the leaf as a directory
    and the in-container user can't write through. Single-file binds
    (e.g. PR #284 prometheus.yml) and pre-existing directory binds
    (provisioning, dashboards) are unaffected because their leaf
    already exists in the repo.

    Allowed pattern:
      - ./docker/grafana/provisioning → /etc/grafana/provisioning:ro
      - ./docker/grafana/dashboards → /var/lib/grafana/dashboards:ro
      - ./docker/prometheus/prometheus.yml → /etc/prometheus/prometheus.yml:ro

    NOT allowed:
      - Any bind-mount whose source path doesn't exist (covered by
        docker-compose CLI validation, not in this test)
      - B64 env vars that depend on Portainer-side env propagation
        (issue #297 root cause; we removed all *_B64 vars from grafana
        service.environment)
    """

    def test_grafana_provisioning_bind_mount_present(self) -> None:
        """Regression for issue #297: grafana MUST bind-mount provisioning
        from ./docker/grafana/provisioning. This is the new source of
        truth after the *_B64 env approach was retired.
        """
        data = _load_compose()
        grafana = data["services"]["grafana"]
        prov_mount = None
        for v in grafana.get("volumes", []):
            v_str = str(v)
            if "/etc/grafana/provisioning" in v_str:
                prov_mount = v_str
                break
        assert prov_mount is not None, (
            "grafana.volumes must bind-mount provisioning YAMLs from "
            "./docker/grafana/provisioning (issue #297, fixes "
            "Portainer 60-char env truncation bug for PROVISIONING_*_B64)"
        )
        assert ":ro" in prov_mount, f"provisioning bind-mount must be read-only (:ro); got: {prov_mount}"

    def test_grafana_dashboards_bind_mount_present(self) -> None:
        """Regression for issue #297: grafana MUST bind-mount dashboards
        from ./docker/grafana/dashboards (matches the dashboard provider
        YAML at docker/grafana/provisioning/dashboards/provider.yml).
        """
        data = _load_compose()
        grafana = data["services"]["grafana"]
        dash_mount = None
        for v in grafana.get("volumes", []):
            v_str = str(v)
            if "/var/lib/grafana/dashboards" in v_str:
                dash_mount = v_str
                break
        assert dash_mount is not None, (
            "grafana.volumes must bind-mount dashboard JSONs from " "./docker/grafana/dashboards (issue #297)"
        )
        assert ":ro" in dash_mount, f"dashboards bind-mount must be read-only (:ro); got: {dash_mount}"

    def test_grafana_no_b64_env_vars(self) -> None:
        """Issue #297 — *_B64 env vars are gone. Any reintroduction
        re-triggers the Portainer 60-char truncation bug.
        """
        svc = _load_compose()["services"]["grafana"]
        env = svc.get("environment", {})
        if isinstance(env, list):
            keys = {item.split("=", 1)[0] for item in env if isinstance(item, str) and "=" in item}
            keys |= {item for item in env if isinstance(item, str) and "=" not in item}
        else:
            keys = set(env.keys())
        for forbidden in (
            "PROVISIONING_DATASOURCES_YML_B64",
            "PROVISIONING_DASHBOARDS_PROVIDER_YML_B64",
            "DASHBOARD_PHASE0_JSON_B64",
            "DASHBOARD_PHASE28_JSON_B64",
        ):
            assert forbidden not in keys, (
                f"grafana.environment must NOT declare {forbidden} "
                f"(replaced by bind-mount per issue #297); got: {sorted(keys)}"
            )

    def test_no_hardcoded_mnt_appdata_volumes_for_config(self) -> None:
        """Wider guard: no service may bind-mount ANY host config file
        (yaml, json, yml) under an APPDATA_DIR-style absolute path.
        The bind sources must be repo-relative paths.
        """
        data = _load_compose()
        offenders = []
        for svc_name, svc in data["services"].items():
            for v in svc.get("volumes", []):
                v_str = str(v)
                # Skip the canonical APPDATA_DIR-based DATA leaves
                if "/var/lib/" in v_str or "/prometheus" in v_str or "/var/log/" in v_str:
                    continue
                import re

                m = re.search(r"^/[^:]+\.(yaml|yml|json|conf|ini|toml):", v_str)
                if m and "/etc/" in v_str:
                    offenders.append((svc_name, v, m.group(0)))
        assert not offenders, (
            f"No service may bind-mount host config files into /etc/. " f"Found offenders: {offenders!r}"
        )
