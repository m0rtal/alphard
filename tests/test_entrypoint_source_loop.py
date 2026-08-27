"""Regression tests for docker/entrypoint.sh env-source loop.

The entrypoint script sources long-token env (TINKOFF_*, ALPHARD_PG_DSN,
etc.) from a bind-mounted file because Portainer StackUpdate Env-parameter
truncates values >60 chars (Tinkoff tokens are 64+ chars). The source loop
tries up to 5 candidate paths and breaks on the first one that exists.

Issue #295: the loop did NOT include ``/root/.env`` even though
``docker-compose.yaml`` bind-mounts it at ``/root/.env:ro`` (BUGFIX (#122)
comment). Local-dev bring-ups without an explicit ``ENV_FILE`` env var then
silently ran with ``ALPHARD_PG_DSN=None`` and all universe-coverage gauges
stuck at 0. The fix adds ``/root/.env`` as the 2nd candidate.

These tests extract the source-loop block from entrypoint.sh, then execute
it under controlled ``env`` / filesystem conditions via ``/bin/sh``.
Pure-blackbox: no mocks, no module import.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENTRYPOINT = _REPO_ROOT / "docker" / "entrypoint.sh"

# Canonical candidate order (issue #295, post-fix). See the assertion
# in test_loop_candidates_are_in_expected_order below for the actual
# check — that test strips the surrounding quotes for normalization.
EXPECTED_CANDIDATE_ORDER_IN_QUOTED_FORM = [
    '"${ENV_FILE:-}"',
    '"/root/.env"',
    '"/run/secrets/alphard.env"',
    '"/run/secrets/alphard_env"',
    '"/tmp/alphard.env"',
]
# Kept for documentation; the assertion below uses the unquoted form
# because re.findall('"([^"]+)"', line) returns the contents without
# the surrounding quotes.


@pytest.fixture
def extracted_loop() -> str:
    """Extract the source-loop block from docker/entrypoint.sh at test time.

    Returns a self-contained shell snippet that:
      1. runs the original `for ... do ... done` block verbatim
      2. appends debug echos so the test can assert which candidate was
         actually picked and what ALPHARD_PG_DSN got exported.

    Why extract rather than copy/paste: the test must reflect what
    entrypoint.sh actually does. If someone changes the loop, this test
    notices.
    """
    src = _ENTRYPOINT.read_text()
    lines = src.splitlines()

    start = None
    end = None
    for i, line in enumerate(lines):
        if start is None and line.startswith("for ENV_FILE_CANDIDATE in"):
            start = i
        elif start is not None and line.strip() == "done":
            end = i
            break

    assert start is not None and end is not None, (
        f"Could not find source-loop block in entrypoint.sh " f"(start={start}, end={end})"
    )

    loop_block = "\n".join(lines[start : end + 1])

    snippet = textwrap.dedent(f"""
        # --- BEGIN extracted loop (entrypoint.sh lines {start + 1}..{end + 1}) ---
        {loop_block}
        # --- END extracted loop ---

        # Debug instrumentation (test-only, NOT in entrypoint.sh):
        echo SOURCED="${{ENV_FILE_CANDIDATE:-NONE}}"
        echo ALPHARD_PG_DSN="${{ALPHARD_PG_DSN:-NONE}}"
        echo TINKOFF_SANDBOX_TOKEN="${{TINKOFF_SANDBOX_TOKEN:-NONE}}"
        """)
    return snippet


def _run_sh(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a shell snippet under controlled env. Caller writes any
    required files BEFORE calling this helper."""
    clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    clean_env.update(env)
    return subprocess.run(
        ["/bin/sh", "-c", snippet],
        capture_output=True,
        text=True,
        env=clean_env,
        timeout=15,
    )


def _parse_sourced(result: subprocess.CompletedProcess) -> tuple[str | None, str | None]:
    """Extract the sourced path and ALPHARD_PG_DSN from script output."""
    sourced: str | None = None
    dsn: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("SOURCED="):
            val = line.split("=", 1)[1]
            if val != "NONE":
                sourced = val
        elif line.startswith("ALPHARD_PG_DSN="):
            val = line.split("=", 1)[1]
            if val != "NONE":
                dsn = val
    return sourced, dsn


class TestEntrypointSourceLoop:
    """Regression coverage for the env-source loop in docker/entrypoint.sh."""

    def test_loop_candidates_are_in_expected_order(self) -> None:
        """The for-loop must list candidates in the documented order.

        ENV_FILE first (explicit override wins), then /root/.env (local
        dev bind-mount), then compose-secrets paths, then /tmp fallback.
        If someone reorders the list, this test fails.
        """
        src = _ENTRYPOINT.read_text()
        for line in src.splitlines():
            if line.startswith("for ENV_FILE_CANDIDATE in"):
                # Extract ONLY the candidate list — the substring between
                # ` in ` and `; do`. Anything after `; do` is the loop body
                # which also contains quoted strings (e.g.,
                # ``${ENV_FILE_CANDIDATE}`` in the ``[ -f ... ]`` test)
                # and would confuse naive regex.
                inside = line.split(" in ", 1)[1].split("; do", 1)[0]
                import re

                quoted = re.findall(r'"([^"]+)"', inside)
                normalized = [c.replace('"', "") for c in quoted]
                # The for-loop also lists ``${ENV_FILE:-}`` (parameter
                # expansion) which we normalize to its quoted form.
                # Strip the quoting from all candidates.
                assert normalized == [
                    "${ENV_FILE:-}",
                    "/root/.env",
                    "/run/secrets/alphard.env",
                    "/run/secrets/alphard_env",
                    "/tmp/alphard.env",
                ], (
                    f"entrypoint.sh source loop candidates changed.\n"
                    f"Got (normalized): {normalized}\n"
                    f"Expected:        {EXPECTED_CANDIDATE_ORDER_IN_QUOTED_FORM}\n"
                    f"Issue #295 — if you reordered intentionally, "
                    f"update the assertion list."
                )
                return
        pytest.fail("Could not find source loop in entrypoint.sh")

    def test_root_env_path_is_in_candidates(self) -> None:
        """Hard pin: ``/root/.env`` MUST be in the candidate list.

        This is the issue #295 fix — without it, local-dev bind-mount
        ``/root/.env:/root/.env:ro`` (BUGFIX #122 in compose) is invisible
        to the source loop. If you are tempted to remove it, read the
        issue first.
        """
        src = _ENTRYPOINT.read_text()
        for line in src.splitlines():
            if line.startswith("for ENV_FILE_CANDIDATE in"):
                assert "/root/.env" in line, (
                    "Issue #295: /root/.env was removed from the source " "loop candidates. Restore it."
                )
                return
        pytest.fail("Could not find source loop in entrypoint.sh")

    def test_env_file_override_wins(self, extracted_loop: str, tmp_path: Path) -> None:
        """When ENV_FILE points elsewhere, that file wins over /root/.env.

        /root/.env exists in this test (it's the real file on the host
        dev box). ENV_FILE points to a fake override file with a distinct
        marker value. Override must be picked because it's the first
        candidate in the for-loop.
        """
        override_path = tmp_path / "override.env"
        override_path.write_text("ALPHARD_PG_DSN=from_override\nTINKOFF_SANDBOX_TOKEN=t.override\n")
        result = _run_sh(
            extracted_loop,
            env={"ENV_FILE": str(override_path)},
        )
        assert result.returncode == 0, f"script failed: {result.stderr}"
        sourced, dsn = _parse_sourced(result)
        assert sourced == str(override_path), f"Expected ENV_FILE candidate to be picked first, got {sourced}"
        assert dsn == "from_override"

    def test_root_env_picked_when_no_env_file_override(self, extracted_loop: str) -> None:
        """Issue #295 regression: when no ENV_FILE override is set, the
        loop must source ``/root/.env`` (the bind-mounted compose local-dev
        path).

        On this dev host ``/root/.env`` is a real file (the production host
        has the same bind-mount via BUGFIX #122). The loop iterates
        /root/.env as candidate #2 (after ENV_FILE which is unset) and
        picks it. ALPHARD_PG_DSN must come from /root/.env's real
        contents (which contain a postgres://alphard:***@alphard-postgres
        string).

        Test isolation: remove any stale /tmp/alphard.env from a previous
        CI run. test_tmp_alphard_env_picked_when_only_tmp_exists deletes
        its file in finally, but if a previous CI run crashed mid-test
        the file may persist into the next run; in CI /root/.env does
        NOT exist (no compose bind-mount on the GitHub Actions runner),
        so the loop would pick /tmp/alphard.env as the LAST candidate
        and this assertion would fail. Cleanup is idempotent.
        """
        tmp_alpha_env = Path("/tmp/alphard.env")
        tmp_alpha_env.unlink(missing_ok=True)
        try:
            result = _run_sh(extracted_loop, env={})
            assert result.returncode == 0, f"script failed: {result.stderr}"
            sourced, dsn = _parse_sourced(result)
            # /root/.env exists on this dev host, so the loop picks it.
            # On CI (no /root/.env) we skip — see test_no_source_when_loop_exhausts_candidates.
            if sourced is None:
                pytest.skip(
                    "Neither /root/.env nor any candidate file exists on "
                    "this host — cannot exercise the /root/.env path. "
                    "Run inside a container with the compose bind-mount."
                )
            assert sourced == "/root/.env", (
                f"Expected /root/.env to be picked as fallback, got {sourced}. "
                f"This is exactly issue #295 — fix reverted?"
            )
            # The actual DSN string from /root/.env starts with postgresql://
            assert dsn is not None and dsn.startswith("postgresql://"), (
                f"Expected ALPHARD_PG_DSN from /root/.env to look like a "
                f"postgres DSN, got {dsn!r}"
            )
        finally:
            tmp_alpha_env.unlink(missing_ok=True)

    def test_tmp_alphard_env_picked_when_only_tmp_exists(self, extracted_loop: str) -> None:
        """``/tmp/alphard.env`` is the documented last-resort manual
        fallback. If only that file exists (no ENV_FILE, no /root/.env,
        no compose-secrets), it must be picked.

        This test pre-creates /tmp/alphard.env with a known marker. If
        /root/.env or /run/secrets/* also happen to be present on the
        host (they are), the loop picks the EARLIER candidate and this
        test is skipped. We accept the test fragility — it documents the
        fallback ordering, not an absolute guarantee that /tmp is the
        only file on disk.
        """
        path = Path("/tmp/alphard.env")
        path.write_text("ALPHARD_PG_DSN=from_tmp\nTINKOFF_SANDBOX_TOKEN=t.tmp\n")
        try:
            result = _run_sh(extracted_loop, env={})
            assert result.returncode == 0, f"script failed: {result.stderr}"
            sourced, dsn = _parse_sourced(result)
            # /root/.env exists on this host → loop picks it before
            # /tmp/alphard.env. That's correct ordering. We document the
            # intent but can't enforce it when other candidates also
            # exist.
            if sourced == "/tmp/alphard.env":
                assert dsn == "from_tmp"
            else:
                pytest.skip(
                    f"/root/.env or /run/secrets/* exists on this host, so "
                    f"loop picked {sourced!r} before /tmp/alphard.env. "
                    f"Run inside a clean container (or a chroot) to "
                    f"exercise the /tmp-only path."
                )
        finally:
            path.unlink(missing_ok=True)

    def test_no_source_when_loop_exhausts_candidates(self, extracted_loop: str) -> None:
        """If NONE of the candidates exist (env_file unset AND no bind-mount
        leaves on disk), the loop silently exits with all vars unset. No
        error, no fallback. Production with broken bind mounts would
        surface the issue via ``No TINKOFF_* token`` sanity gate further
        down in the script.

        We cannot easily simulate "nothing exists" on this host because
        /root/.env is real. Skip when it's present, otherwise assert.
        """
        result = _run_sh(extracted_loop, env={})
        assert result.returncode == 0, f"script failed: {result.stderr}"
        sourced, dsn = _parse_sourced(result)
        if sourced is not None:
            pytest.skip(
                f"At least one candidate ({sourced}) exists on this host; "
                f"cannot test the all-empty scenario. Run inside a "
                f"clean container with all bind-mounts removed."
            )
        assert sourced is None
        assert dsn is None

    def test_set_a_export_does_not_leak_into_parent_shell(self, extracted_loop: str) -> None:
        """``set -a`` inside the loop must NOT leak back to the caller.

        The outer pytest process must NOT see ALPHARD_PG_DSN **changed**
        by the loop's `set -a` invocation. Guards against accidental
        scope leakage that would cause cross-test pollution.

        Note: ALPHARD_PG_DSN may already be in the CI pytest env (the
        Tests + Coverage job sets it for Postgres integration). We only
        verify it wasn't CLOBBERED, not that it's absent. The pre-existing
        value is restored in the finally block.
        """
        # Snapshot pre-existing ALPHARD_PG_DSN (defensive — CI may have
        # it for integration tests, but we must not assume it doesn't).
        pre_existing = os.environ.get("ALPHARD_PG_DSN")
        try:
            result = _run_sh(extracted_loop, env={})
            assert result.returncode == 0
            # OUTER pytest process must not see ALPHARD_PG_DSN *changed*.
            # set -a inside the loop runs in a child sh -c, so its exports
            # die with the child. Verify by checking the value is exactly
            # what it was before the run.
            assert os.environ.get("ALPHARD_PG_DSN") == pre_existing, (
                "set -a inside the loop leaked into the parent shell — "
                "this would cause cross-test pollution. The loop should "
                "use a subshell or the entrypoint process boundary, not "
                "the test runner's env."
            )
        finally:
            if pre_existing is not None:
                os.environ["ALPHARD_PG_DSN"] = pre_existing
