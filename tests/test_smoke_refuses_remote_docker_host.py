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
        return subprocess.run(
            ["bash", str(SMOKE)],
            cwd=str(REPO),
            env={**env, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=timeout,
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
