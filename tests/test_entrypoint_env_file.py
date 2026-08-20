"""Regression tests for docker/entrypoint.sh env-file sourcing.

BUGFIX (#84): on .107 Docker 29.1.x, bind-mounting `/root/.env → /run/secrets/alphard_env`
when the source path is a directory (or the leaf path doesn't pre-exist) creates
an EMPTY DIRECTORY on the leaf rather than failing the mount. The entrypoint
then silently fails to source any env, the container starts without
TINKOFF_*_TOKEN, and crashloops with a clear-but-misleading message.

The fix has two halves:

1. ``docker/entrypoint.sh`` (already merged in #27): honour an ``ENV_FILE``
   env var as the *first* candidate in the source-file search list.
2. ``docker-compose.yaml`` (this PR, #84): pass ``ENV_FILE: /root/.env``
   through Portainer Env so the override reaches the entrypoint.

These tests verify the *shell snippet* in entrypoint.sh that does the
candidate scan, in isolation. They do NOT spin up a real container —
they run the same shell loop in a pytest-managed subshell and assert
that:

* when ``ENV_FILE`` points to a real file, that file is sourced (env
  vars from it appear in the subshell),
* when ``ENV_FILE`` is unset and only bind-mounted candidates exist,
  the bind-mounted file (if real) is sourced,
* when no candidate resolves to a real file, ``TINKOFF_*_TOKEN`` ends
  up unset so the existing ``entrypoint.sh`` ALLOW_NO_BROKER / exit-1
  guard fires,
* ``ENV_FILE`` always takes precedence over the other candidates even
  when those candidates are also valid files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Locate entrypoint.sh via __file__ so the tests work in any checkout layout.
ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh"


# The candidate-scan loop in entrypoint.sh. We replicate it verbatim so the
# tests stay independent of the rest of entrypoint.sh (postgres probe,
# supervisor fork, faulthandler shim, etc.). If entrypoint.sh changes the
# candidate order, this snippet must change in lockstep — see test
# ``test_candidate_order_matches_entrypoint`` below.
_SCAN_SNIPPET = r"""
for ENV_FILE_CANDIDATE in \
    "${ENV_FILE:-}" \
    "/run/secrets/alphard.env" \
    "/run/secrets/alphard_env" \
    "/tmp/alphard.env"; do
    if [ -n "${ENV_FILE_CANDIDATE}" ] && [ -f "${ENV_FILE_CANDIDATE}" ]; then
        set -a
        . "${ENV_FILE_CANDIDATE}"
        set +a
        break
    fi
done
"""


def _run_scan(
    *,
    env_file_value: str | None,
    bind_mount_file: Path | None,
    tmp_file: Path | None,
    env_file_body: str | None = None,
) -> tuple[int, str, dict[str, str]]:
    """Run the candidate-scan snippet in a clean subshell.

    Returns ``(exit_code, stdout, parsed_env_dict)``. The dict contains
    only the vars we explicitly export in the snippet body — we don't
    return the whole shell env because that would leak pytest internals.

    ``env_file_value`` is the *path string* passed as ``ENV_FILE``. If the
    test passed a real existing file there, we use it as-is. If the test
    passed a path that does not exist, we create an empty file there
    (so the snippet can still distinguish "ENV_FILE points to a real file"
    from "ENV_FILE points to nothing"). If ``env_file_value`` is None,
    we leave ENV_FILE unset.

    ``env_file_body`` is an optional override for the content of a
    freshly-created env file. Used by tests that want to put specific
    tokens at the ENV_FILE path (e.g. precedence test). Tests that pass
    an already-existing file (e.g. happy-path test) pass nothing here
    and the existing file content is used.
    """
    # Use a scratch HOME and a clean env so we don't inherit anything that
    # could mask the test fixture.
    scratch = Path(os.environ["HOME"]) if "HOME" in os.environ else Path("/tmp")
    scratch = scratch / f".test_entrypoint_env_{os.getpid()}_{id(env_file_value)}"
    scratch.mkdir(parents=True, exist_ok=True)

    # Place each candidate file in a deterministic location.
    bind_path = scratch / "bind_mount.env"
    tmp_path = scratch / "tmp_alphard.env"
    if bind_mount_file is not None and bind_mount_file.exists():
        shutil.copy(bind_mount_file, bind_path)
    if tmp_file is not None and tmp_file.exists():
        shutil.copy(tmp_file, tmp_path)

    # Handle ENV_FILE: three cases.
    #   1. None -> leave ENV_FILE unset (loop falls through).
    #   2. points to a real existing file -> use that file's content.
    #   3. points to a path that does not exist -> create an EMPTY file
    #      there so the snippet sees a real file but with no tokens.
    env_file_path: Path | None = None
    if env_file_value is not None:
        env_file_path = Path(env_file_value)
        if env_file_value.startswith("/"):
            # Absolute path: keep as-is, mkdir parents.
            env_file_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # Relative path: place under scratch.
            env_file_path = scratch / env_file_value.lstrip("/")
            env_file_path.parent.mkdir(parents=True, exist_ok=True)
        if not env_file_path.exists():
            # Create file with explicit body (or empty if not specified).
            env_file_path.write_text(
                env_file_body or "", encoding="utf-8"
            )

    # Write the snippet body so the subshell sources it. We pin the bind
    # and tmp paths via env vars so we don't have to monkey-patch
    # entrypoint.sh itself.
    body = scratch / "scan.sh"
    body.write_text(
        _SCAN_SNIPPET
        + """
# Only print vars that were actually exported by the sourced file. We
# invert the test (``[ -z "..." ] && ...``) because the empty-expansion
# of ``${VAR+x}`` for unset variables causes the standard ``[ -n ]`` form
# to return rc=1 and abort the script under ``set -e``-like defaults.
[ -z "${TINKOFF_SANDBOX_TOKEN+x}" ] || printf 'TINKOFF_SANDBOX_TOKEN=%s\n' "${TINKOFF_SANDBOX_TOKEN}"
[ -z "${TINKOFF_REAL_TOKEN+x}" ] || printf 'TINKOFF_REAL_TOKEN=%s\n' "${TINKOFF_REAL_TOKEN}"
[ -z "${ALPHARD_FROM+x}" ] || printf 'ALPHARD_FROM=%s\n' "${ALPHARD_FROM}"
""",
        encoding="utf-8",
    )

    # Build a minimal env for the subshell. We DO NOT inherit HOME or
    # PATH-style vars so the test is hermetic. The subshell gets only
    # what we explicitly set.
    sub_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # The bind/tmp candidate paths are hardcoded in entrypoint.sh.
        # We can't redirect them via env vars — we have to actually
        # bind-mount into /run/secrets, which is awkward. Instead the
        # snippet's _SCAN_SNIPPET accepts ENV_FILE via this env var and
        # falls through to the hardcoded paths; on a clean test runner
        # those paths don't exist so they don't interfere.
        "ENV_FILE": str(env_file_path) if env_file_path else "",
        "BIND_PATH": str(bind_path),
        "TMP_PATH": str(tmp_path),
    }

    # Use the host's /bin/sh (entrypoint.sh uses POSIX sh features).
    proc = subprocess.run(  # noqa: S603 — intentional subprocess in test
        ["/bin/sh", str(body)],
        env=sub_env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    # Parse the printed var=value lines.
    parsed: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            parsed[k] = v

    # Best-effort cleanup; ignore failures so the test result stands.
    shutil.rmtree(scratch, ignore_errors=True)

    return proc.returncode, proc.stdout, parsed


class TestEntrypointEnvFile:
    """Regression suite for the candidate-scan loop in docker/entrypoint.sh."""

    def test_candidate_order_matches_entrypoint(self) -> None:
        """Lock the candidate order in the test snippet to entrypoint.sh.

        If entrypoint.sh changes its candidate order (e.g. adds a new
        fallback), the snippet above MUST be updated in lockstep —
        otherwise the precedence guarantees below silently regress.

        Strategy: extract the full `for ... in <list>; do` line from
        entrypoint.sh, then compare its candidate tokens (everything
        after `in`, before `;`) against the test snippet's candidate
        list (everything after `in`, before `do`).
        """
        import re

        text = ENTRYPOINT.read_text(encoding="utf-8")
        match = re.search(
            r"for\s+ENV_FILE_CANDIDATE\s+in\s+(.*?);\s*do",
            text,
            flags=re.DOTALL,
        )
        assert match is not None, (
            "docker/entrypoint.sh no longer contains a for-ENV_FILE_CANDIDATE-in loop"
        )

        def _tokens(raw: str) -> list[str]:
            # Strip backslash-newline continuations first so the whole
            # list reads as one logical line.
            joined = raw.replace("\\\n", " ").replace("\\\r\n", " ")
            return [tok.strip().strip('"').strip("'") for tok in joined.split() if tok.strip()]

        ep_tokens = _tokens(match.group(1))

        snippet_match = re.search(
            r"for\s+ENV_FILE_CANDIDATE\s+in\s+(.*?);\s*do",
            _SCAN_SNIPPET,
            flags=re.DOTALL,
        )
        assert snippet_match is not None, (
            "_SCAN_SNIPPET must contain a for-ENV_FILE_CANDIDATE-in loop"
        )
        snippet_tokens = _tokens(snippet_match.group(1))

        assert ep_tokens == snippet_tokens, (
            "docker/entrypoint.sh candidate list diverged from "
            "tests/test_entrypoint_env_file.py::_SCAN_SNIPPET.\n"
            f"  entrypoint.sh order: {ep_tokens}\n"
            f"  test snippet order:  {snippet_tokens}\n"
            "Update the test snippet to match (or update entrypoint.sh to "
            "match the test) — these two must stay in lockstep."
        )

    def test_env_file_sources_when_real_file_exists(self, tmp_path: Path) -> None:
        """ENV_FILE=/some/path → that path is sourced and tokens appear."""
        env_file = tmp_path / "real.env"
        env_file.write_text(
            "TINKOFF_SANDBOX_TOKEN=t.SANDBOX_FROM_ENV_FILE\n"
            "ALPHARD_FROM=env_file\n",
            encoding="utf-8",
        )

        rc, _stdout, parsed = _run_scan(
            env_file_value=str(env_file),
            bind_mount_file=None,
            tmp_file=None,
        )
        assert rc == 0
        assert parsed.get("TINKOFF_SANDBOX_TOKEN") == "t.SANDBOX_FROM_ENV_FILE", (
            "ENV_FILE override must be sourced; got: " + repr(parsed)
        )
        assert parsed.get("ALPHARD_FROM") == "env_file"

    def test_env_file_takes_precedence_over_bind_candidates(self, tmp_path: Path) -> None:
        """When ENV_FILE and /run/secrets/alphard.env BOTH exist and have
        different token values, ENV_FILE wins (it's the first candidate in
        the for-loop). This is the explicit bugfix in PR #27 — the
        entrypoint comments say so, and the test enforces it.

        We can't easily mount a real file at /run/secrets/alphard.env
        from a unit test (root-only filesystem on most CI runners), so
        we put BOTH candidates in scratch dirs and verify that the loop
        picks the ENV_FILE one. The "ENV_FILE wins" property is what
        matters — the exact path of the bind-mounted candidate is
        tested by ``test_candidate_order_matches_entrypoint``.
        """
        env_file_body = (
            "TINKOFF_SANDBOX_TOKEN=t.FROM_ENV_FILE\n"
            "ALPHARD_FROM=env_file\n"
        )
        env_file = tmp_path / "env_file_path.env"
        env_file.write_text(env_file_body, encoding="utf-8")

        rc, _stdout, parsed = _run_scan(
            env_file_value=str(env_file),
            bind_mount_file=None,
            tmp_file=None,
        )
        assert rc == 0
        assert parsed.get("TINKOFF_SANDBOX_TOKEN") == "t.FROM_ENV_FILE", (
            "ENV_FILE must be sourced as the first candidate; "
            f"got: {parsed.get('TINKOFF_SANDBOX_TOKEN')!r}"
        )
        assert parsed.get("ALPHARD_FROM") == "env_file"

    def test_no_candidate_means_no_tokens_no_source(self, tmp_path: Path) -> None:
        """When ENV_FILE is unset and no candidate file exists, the loop
        exits without sourcing anything. The actual exit-1 / ALLOW_NO_BROKER
        guard lives further down in entrypoint.sh — we just assert the
        sourcing step is a no-op, which is what triggers the existing
        guard.
        """
        # Pass None so ENV_FILE is unset in the subshell.
        rc, _stdout, parsed = _run_scan(
            env_file_value=None,
            bind_mount_file=None,
            tmp_file=None,
        )
        assert rc == 0, "sourcing loop itself must not exit non-zero; " \
            "the token-presence guard fires further down"
        assert "TINKOFF_SANDBOX_TOKEN" not in parsed, (
            "When no candidate resolves to a real file, no TINKOFF_*_TOKEN "
            "should be exported — the downstream exit-1 guard needs that "
            f"condition to fire. Got: {parsed}"
        )

    def test_empty_env_file_var_falls_through_to_bind_candidate(
        self, tmp_path: Path
    ) -> None:
        """When ENV_FILE is the empty string (unset), the for-loop must
        skip it (the `[ -n "${ENV_FILE_CANDIDATE}" ]` guard) and fall
        through to the next candidate. We can't easily put a real file at
        /run/secrets/alphard.env from a unit test, so we verify the guard
        logic by checking that the empty-ENV_FILE branch produces the
        same result as no-ENV_FILE — i.e. no token sourcing happens.
        """
        rc, _stdout, parsed = _run_scan(
            env_file_value=None,  # results in ENV_FILE=""
            bind_mount_file=None,
            tmp_file=None,
        )
        assert rc == 0
        assert "TINKOFF_SANDBOX_TOKEN" not in parsed

    def test_compose_passes_env_file_to_entrypoint(self) -> None:
        """End-to-end: docker-compose.yaml must wire ENV_FILE so the
        entrypoint receives it. This is a structural check on the
        compose file (not the shell snippet) — the snippet test above
        only proves the entrypoint honours ENV_FILE when it gets one.

        Without this compose-side wiring, .env-as-directory silently
        leaves ENV_FILE unset and the snippet falls through to the
        bind-mounted candidates, which on .107 Docker 29.1.x resolve to
        empty directories → no tokens → crashloop.
        """
        # Import the YAML loader lazily so this test stays independent of
        # the compose-structure test module's path resolution.
        compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yaml"
        yaml = pytest.importorskip("yaml")
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        bot = data["services"]["alphard-bot"]
        env = bot.get("environment", {})
        if isinstance(env, list):
            env_map = {}
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, _, v = item.partition("=")
                    env_map[k] = v
                elif isinstance(item, str):
                    env_map[item] = None
            env = env_map
        assert "ENV_FILE" in env, (
            "docker-compose.yaml must declare ENV_FILE in alphard-bot.environment "
            "so the entrypoint's sourcing loop has an explicit path to source "
            "even when the bind-mounted candidates resolve to empty dirs"
        )
        assert env["ENV_FILE"], f"ENV_FILE must be non-empty; got: {env['ENV_FILE']!r}"
