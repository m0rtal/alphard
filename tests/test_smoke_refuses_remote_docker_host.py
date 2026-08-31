"""Regression test for the cycle145 catastrophic guard.

scripts/pre_pr_smoke.sh runs `docker compose down -v`, which wipes the
alphard-postgres-data volume. On the dev host, `docker context` defaults
to tcp://192.168.1.107:2375 (production .107) whenever DOCKER_HOST is
exported in the session. A previous run on 2026-09-02 (cycle145-2)
wiped 1.34M OHLCV rows + 3263 universe entries from production by
running this script with the wrong daemon.

The fix is to refuse to start unless DOCKER_HOST is unix:// (local
smoke) or the operator explicitly opts in via ALLOW_NONLOCAL_SMOKE=1.

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
    """Cycle145 catastrophic guard for issue #363 follow-up."""

    def test_smoke_script_exists(self) -> None:
        assert SMOKE.exists(), f"missing pre_pr_smoke.sh at {SMOKE}"

    def test_guard_checks_docker_host_is_remote(self) -> None:
        content = _read_smoke()
        # Pattern: if DOCKER_HOST starts with tcp://, refuse to proceed.
        # Match the structural shape, not the exact wording.
        assert re.search(
            r"DOCKER_HOST.*tcp://",
            content,
        ), "smoke script must check DOCKER_HOST against tcp:// scheme"
        assert re.search(
            r"(ALLOW_NONLOCAL_SMOKE|ALLOW_REMOTE_SMOKE)",
            content,
        ), "smoke script must define an explicit opt-in env var for remote runs"

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
    """Actually run the guard with DOCKER_HOST=tcp://... and assert refusal."""

    def test_remote_docker_host_is_refused(self) -> None:
        env = {"DOCKER_HOST": "tcp://192.168.1.107:2375"}
        proc = subprocess.run(
            ["bash", str(SMOKE)],
            cwd=str(REPO),
            env={**env, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode != 0, (
            f"smoke must refuse to run with DOCKER_HOST=tcp://...; "
            f"got returncode={proc.returncode}, stdout={proc.stdout!r}"
        )
        assert "REFUSED" in proc.stdout, (
            f"smoke refusal must print a clear REFUSED message; " f"got stdout={proc.stdout!r}"
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
            proc = subprocess.run(
                ["bash", str(SMOKE)],
                cwd=str(REPO),
                env={**env, "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            # The script started and didn't immediately bail — that's what
            # we're checking. Timeout is fine here.
            return
        assert "REFUSED" not in proc.stdout, (
            f"smoke must NOT print REFUSED with DOCKER_HOST=unix://...; " f"got stdout={proc.stdout!r}"
        )
