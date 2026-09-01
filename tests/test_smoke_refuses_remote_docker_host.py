"""Regression test for the cycle145/146 DOCKER_HOST guard.

scripts/pre_pr_smoke.sh runs `docker compose down -v`, which wipes the
alphard-postgres-data volume on whatever daemon the active docker
context points at. On a host where `docker context` defaults to a
remote endpoint (e.g. production), any non-unix:// DOCKER_HOST silently
targets production. This guard pins the contract: refuse any
non-unix:// DOCKER_HOST unless ALLOW_NONLOCAL_SMOKE=1.

Cycle145 (issue #363 follow-up) added the guard but matched only
DOCKER_HOST=tcp://...; cycle146 (issue #371) inverted the predicate
so SSH contexts (DOCKER_HOST=ssh://user@host), fd://, npipe://, and
any future scheme are also covered.

This test pins the contract by reading pre_pr_smoke.sh as text and
asserting the guard's structure. Pure pytest; no docker, no LXC/ZFS.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SMOKE = REPO / "scripts" / "pre_pr_smoke.sh"


def _read_smoke() -> str:
    return SMOKE.read_text(encoding="utf-8")


class TestSmokeRefusesRemoteDockerHost:
    """Cycle145/146 catastrophic guard for issues #363/#371."""

    def test_smoke_script_exists(self) -> None:
        assert SMOKE.exists(), f"missing pre_pr_smoke.sh at {SMOKE}"

    def test_guard_allowlists_unix_scheme(self) -> None:
        content = _read_smoke()
        # Pattern: only DOCKER_HOST=unix://... is allowed through without
        # opt-in; everything else (unset, tcp://, ssh://, fd://, npipe://,
        # future schemes) must require ALLOW_NONLOCAL_SMOKE=1.
        assert re.search(
            r"DOCKER_HOST.*unix://",
            content,
        ), "smoke script must allow-list DOCKER_HOST=unix://... as local"
        assert re.search(
            r"(ALLOW_NONLOCAL_SMOKE|ALLOW_REMOTE_SMOKE)",
            content,
        ), "smoke script must define an explicit opt-in env var for non-local runs"

    def test_guard_does_not_whitelist_tcp_only(self) -> None:
        """Regression for issue #371: cycle145 matched only tcp://.

        The cycle146 guard inverts the predicate (only unix:// is local).
        A bare `[[ =~ ^tcp://` would still miss ssh://. Assert the
        inverted `[[ ! =~ ^unix://` form (deny-by-default) is present.
        """
        content = _read_smoke()
        # The guard predicate must explicitly negate against unix://
        # (deny-by-default). Bare tcp:// allow/deny regresses #371.
        assert re.search(
            r"!\s*[\"']?\$\{?DOCKER_HOST",
            content,
        ) and re.search(
            r"=~\s*\^unix://",
            content,
        ), (
            "cycle146 guard must invert the predicate (deny-by-default: "
            "only unix:// is local). A bare `^tcp://` regex regresses "
            "issue #371."
        )

    def test_guard_exits_nonzero_on_remote(self) -> None:
        content = _read_smoke()
        # The guard block must `exit <nonzero>` so callers can detect the refusal.
        # Accept any of: exit 1, exit 9, exit 99. We use 9 to keep it distinct
        # from the existing fatal gates (1 stack unhealthy | 2 pytest | 3 dry-run).
        assert re.search(
            r"exit\s+[1-9][0-9]?",
            content,
        ), "smoke script guard must exit with a non-zero code"

    def test_guard_runs_before_cleanup_trap_down_v(self) -> None:
        """The DOCKER_HOST guard must execute BEFORE the cleanup trap.

        The cleanup() function (which runs `docker compose down -v` on
        EXIT) is registered as a trap early in the script. The guard
        must check DOCKER_HOST BEFORE any code path can reach the trap
        — otherwise a `DOCKER_HOST=tcp://...` session will still wipe
        the remote volume when the trap fires on EXIT.
        """
        content = _read_smoke()
        guard_pos = content.find("DOCKER_HOST")
        # The actual docker compose down -v command (not the comment).
        down_call_pos = content.find('COMPOSE[@]}" down -v')
        assert guard_pos > 0, "smoke script must have the DOCKER_HOST guard"
        assert down_call_pos > 0, "smoke script must still tear down the stack via the cleanup trap"
        assert guard_pos < down_call_pos, (
            f"DOCKER_HOST guard at offset {guard_pos} must run BEFORE "
            f"the cleanup-trap docker compose down -v at offset "
            f"{down_call_pos}; otherwise a session with DOCKER_HOST="
            f"tcp://... will still wipe the remote volume when the trap "
            f"fires on EXIT."
        )


class TestSmokeRemoteGuardRuntime:
    """Actually run the guard with non-local DOCKER_HOST values and assert refusal.

    Cycle146 (issue #371) broadened the guard from tcp:// to deny-by-default.
    Each test pins one remote scheme to the REFUSED contract.
    """

    def _run_smoke(self, env: dict[str, str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
        proc = subprocess.Popen(
            ["bash", str(SMOKE)],
            cwd=str(REPO),
            env={**env, "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # BUGFIX (cycle148, issue #374): the smoke script registers a
            # cleanup() trap that runs `docker compose down -v` on EXIT.
            # If we let TimeoutExpired drop the child without killing it,
            # the trap fires later and tears down whatever stack the
            # operator happens to be running on the same daemon. Kill the
            # child, drain its stdout, then re-raise so the caller's
            # except branch decides whether the timeout is acceptable.
            proc.kill()
            try:
                stdout, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout = ""  # best-effort: child ignored SIGKILL, nothing useful to capture
            # Re-raise with the captured stdout attached so callers can
            # assert against it. exc.stdout is normally bytes | None on
            # TimeoutExpired; coerce to whatever we captured.
            exc.stdout = stdout  # type: ignore[assignment]
            raise
        return subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout=stdout,
            stderr="",
        )

    def test_tcp_docker_host_is_refused(self) -> None:
        env = {"DOCKER_HOST": "tcp://192.168.1.107:2375"}
        proc = self._run_smoke(env)
        assert proc.returncode != 0, (
            f"smoke must refuse to run with DOCKER_HOST=tcp://...; "
            f"got returncode={proc.returncode}, stdout={proc.stdout!r}"
        )
        assert "REFUSED" in proc.stdout, (
            f"smoke refusal must print a clear REFUSED message; " f"got stdout={proc.stdout!r}"
        )

    def test_ssh_docker_host_is_refused(self) -> None:
        """Regression for issue #371: cycle145 regex missed ssh://.

        An SSH Docker context (DOCKER_HOST=ssh://user@host) must trip
        the guard and exit non-zero with a REFUSED message.
        """
        env = {"DOCKER_HOST": "ssh://user@192.168.1.107"}
        proc = self._run_smoke(env)
        assert proc.returncode != 0, (
            f"smoke must refuse to run with DOCKER_HOST=ssh://...; "
            f"got returncode={proc.returncode}, stdout={proc.stdout!r}"
        )
        assert "REFUSED" in proc.stdout, (
            f"smoke refusal must print a clear REFUSED message for ssh://; " f"got stdout={proc.stdout!r}"
        )
        assert "ssh://user@192.168.1.107" in proc.stdout, (
            f"smoke refusal message must echo the actual DOCKER_HOST value "
            f"so the operator can see which context tripped the guard; "
            f"got stdout={proc.stdout!r}"
        )

    def test_fd_docker_host_is_refused(self) -> None:
        """Regression for issue #371: future-scheme defence.

        fd:// (Docker Desktop's file-descriptor transport on some
        platforms) and any non-unix:// scheme must trip the guard.
        """
        env = {"DOCKER_HOST": "fd://"}
        proc = self._run_smoke(env)
        assert proc.returncode != 0, (
            f"smoke must refuse to run with DOCKER_HOST=fd://...; "
            f"got returncode={proc.returncode}, stdout={proc.stdout!r}"
        )
        assert "REFUSED" in proc.stdout, (
            f"smoke refusal must print a clear REFUSED message for fd://; " f"got stdout={proc.stdout!r}"
        )

    def test_allow_nonlocal_smoke_opt_in_proceeds(self) -> None:
        """ALLOW_NONLOCAL_SMOKE=1 must bypass the REFUSED gate for any scheme.

        We only assert the WARNING fires and the guard does NOT print
        REFUSED. Downstream `docker compose up` may still fail for
        unrelated reasons (no daemon, no image); that's fine — the
        contract is that the guard no longer blocks.
        """
        env = {
            "DOCKER_HOST": "ssh://user@host",
            "ALLOW_NONLOCAL_SMOKE": "1",
        }
        try:
            proc = self._run_smoke(env, timeout=15)
        except subprocess.TimeoutExpired:
            # Script ran past the guard — that's exactly what we want.
            return
        assert "REFUSED" not in proc.stdout, (
            f"smoke must NOT print REFUSED when ALLOW_NONLOCAL_SMOKE=1; " f"got stdout={proc.stdout!r}"
        )
        assert "WARNING" in proc.stdout, (
            f"smoke must print a clear WARNING when ALLOW_NONLOCAL_SMOKE=1 "
            f"permits a non-local DOCKER_HOST; got stdout={proc.stdout!r}"
        )

    def test_local_docker_host_is_allowed_through(self) -> None:
        """With DOCKER_HOST=unix://... the script must NOT print REFUSED.

        Note: this still runs the full smoke (or fails downstream for
        other reasons); we only assert that the REFUSED block did not
        fire. The test is short-timeout-bounded because the script will
        either succeed, hit its own fatal gate, or run for the full
        HEALTH_TIMEOUT — we only care about the first few lines of output.
        """
        env = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
        try:
            proc = self._run_smoke(env, timeout=10)
        except subprocess.TimeoutExpired:
            # The script started and didn't immediately bail — that's what
            # we're checking. Timeout is fine here.
            return
        assert "REFUSED" not in proc.stdout, (
            f"smoke must NOT print REFUSED with DOCKER_HOST=unix://...; " f"got stdout={proc.stdout!r}"
        )


class TestSmokeProjectIsolation:
    """Regression for issue #374: scope the smoke stack to a per-PID project name.

    Without `--project-name alphard-smoke-<PID>`, the smoke script's
    `docker compose down -v` reuses whatever alphard-* containers happen
    to exist on the daemon (because docker-compose.yaml hardcodes
    container_name:). On a host where the operator is running their own
    alphard stack, the smoke's cleanup trap wipes the operator's
    alphard-postgres-data volume — destructive collision. The fix is to
    give every smoke run a unique compose project name so the cleanup
    trap is scoped to the smoke's own resources only.
    """

    def test_smoke_script_uses_per_pid_project_name(self) -> None:
        content = _read_smoke()
        # The compose invocation must include `-p alphard-smoke-...` so
        # every run gets a unique project name (and therefore unique
        # container names). A bare `docker compose -f ...` regresses
        # issue #374.
        assert re.search(
            r"COMPOSE=.*-p\s+\"?\$\{?COMPOSE_PROJECT_NAME",
            content,
        ) or re.search(
            r"-p\s+\"?\$\{?COMPOSE_PROJECT_NAME",
            content,
        ), (
            "smoke script must pass `-p $COMPOSE_PROJECT_NAME` (a per-PID "
            "compose project name) on every compose invocation so the "
            "cleanup trap's `down -v` is scoped to the smoke's own "
            "containers and never reuses the operator's alphard-* stack."
        )

    def test_smoke_script_derives_project_name_from_pid(self) -> None:
        content = _read_smoke()
        # The project name must be derived from `$$` (the script's PID)
        # so concurrent runs don't collide and the operator's stack
        # (project name `alphard`) is untouched.
        assert re.search(
            r"COMPOSE_PROJECT_NAME\s*=\s*[\"']?alphard-smoke-\$\$",
            content,
        ), (
            "smoke script must derive COMPOSE_PROJECT_NAME from $$ so each "
            "run gets a unique project name; otherwise concurrent smoke "
            "runs collide on the same alphard-* container names."
        )

    def test_smoke_script_does_not_hardcode_alphard_bot_in_docker_exec(self) -> None:
        content = _read_smoke()
        # After the project-rename fix, `docker exec alphard-bot ...` and
        # `docker inspect alphard-postgres ...` would target the operator's
        # stack instead of the smoke's. The script must use the per-PID
        # project-scoped aliases everywhere it touches a container by name.
        assert "docker exec alphard-bot" not in content, (
            "smoke must not hardcode `docker exec alphard-bot`; use "
            "$SMOKE_BOT (derived from $COMPOSE_PROJECT_NAME) so the exec "
            "targets the smoke's own container, not the operator's."
        )
        assert "docker inspect alphard-postgres" not in content, (
            "smoke must not hardcode `docker inspect alphard-postgres`; "
            "use $SMOKE_PG (derived from $COMPOSE_PROJECT_NAME) so the "
            "inspect targets the smoke's own container."
        )

    def test_test_helper_kills_orphan_subprocess_on_timeout(self) -> None:
        """The pytest helper must kill its subprocess on TimeoutExpired.

        Regression for issue #374: when the smoke script's 10s timeout
        expires during `test_local_docker_host_is_allowed_through`, the
        `subprocess.run(... timeout=...)` call returns normally without
        killing the child. The child's EXIT trap then fires `docker
        compose down -v` ~90s later and tears down the operator's stack.
        The fix in `_run_smoke` uses `subprocess.Popen` and explicitly
        `proc.kill()`s the child in the TimeoutExpired branch.
        """
        import inspect

        # `_run_smoke` lives on TestSmokeRemoteGuardRuntime, not on this
        # class — grab it from there. inspect.getsource() works on
        # unbound methods retrieved via the class attribute.
        helper = getattr(TestSmokeRemoteGuardRuntime, "_run_smoke", None)
        assert helper is not None, (
            "TestSmokeRemoteGuardRuntime must define _run_smoke; the "
            "test helper that runs the smoke script as a subprocess."
        )
        source = inspect.getsource(helper)
        assert "subprocess.Popen" in source, (
            "test helper _run_smoke must use subprocess.Popen (not "
            "subprocess.run(... timeout=...)) so the helper retains a "
            "handle to the child and can kill it on TimeoutExpired."
        )
        assert "proc.kill()" in source, (
            "test helper _run_smoke must proc.kill() the smoke subprocess "
            "on TimeoutExpired so the smoke's cleanup trap (which runs "
            "`docker compose down -v`) cannot fire after the test returns. "
            "Regression for issue #374."
        )
