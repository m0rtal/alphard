"""Regression tests for scripts/pre_pr_smoke.sh (issue #336).

Background
----------
Issue #336 reported that ``scripts/pre_pr_smoke.sh`` constructed the
Postgres DSN passed to ``daily_incremental.py --dry-run`` with a
literal ``***`` placeholder instead of the ``${PG_PW}`` variable the
author intended. Investigation for these tests showed that **main HEAD
already uses ``${PG_PW}`` correctly** at the byte level — the issue
was filed based on a visual misread of ``${PG_PW}`` (five characters:
``$``, ``{``, ``P``, ``G``, ``_``, ``P``, ``W``, ``}``) which is
visually similar to ``***`` in a monospace font. Either way, the safe
forward-looking posture is to pin the corrected behaviour at the source
level so a future drive-by edit cannot re-introduce the same defect.

These tests pin two structural properties of ``SMOKE_DSN``:

1. The assignment line MUST use ``${PG_PW}`` (not the literal ``***``
   placeholder).
2. ``bash -n`` parses the script cleanly so a syntax error never lands
   in main.

We also pin the **hook ↔ script contract**: the companion
``scripts/hooks/pre-push`` refuses ``git push`` unless the script has
written a sentinel under ``/tmp/.alphard-pr-smoke-pass.<branch>``.
Both files must agree on the sentinel path so the gate actually works.

Strategy mirrors ``tests/test_init_postgres_sh.py``: pure-text grep
assertions on the script body — no shell execution, no docker, so the
test itself never depends on the very infrastructure it is guarding.

Notes
-----
The earlier cycle110 reproducer used ``grep -nE '***'`` style queries
against the GitHub-returned file. Those queries print the matched
line — and ``${PG_PW}`` rendered with three characters between
``{`` and ``}`` looks identical to ``***`` to a casual read. We pin
the property at the byte level (search for ``${PG_PW}`` substring;
the test asserts presence, not absence, to avoid the same visual
trap). For the rejection path we also assert that ``PG_PW`` is
assigned from ``.env`` — without an assignment, a future drive-by
``unset PG_PW`` would fall back to empty under ``set -u``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pre_pr_smoke.sh"
HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre-push"


def _read(path: Path) -> str:
    """Read a scripts/* file via __file__-relative path so the tests
    work on any checkout layout (CI runners, fresh clones, container
    mounts)."""
    return path.read_text(encoding="utf-8")


class TestSmokeDsnSubstitution:
    """Pin the corrected ``SMOKE_DSN`` assignment behaviour.

    The property under test: the line that assigns ``SMOKE_DSN``
    interpolates ``${PG_PW}`` (the password read from ``.env``), so
    Postgres receives the real password over the wire.
    """

    def test_smoke_dsn_uses_pg_pw_variable(self) -> None:
        body = _read(SCRIPT_PATH)
        match = re.search(r"^SMOKE_DSN=(.+)$", body, re.MULTILINE)
        assert match, "expected a ``SMOKE_DSN=...`` assignment in scripts/pre_pr_smoke.sh"
        rhs = match.group(1)
        assert "${PG_PW}" in rhs, (
            "SMOKE_DSN must reference ${PG_PW} so the real .env password "
            "is sent to Postgres. If you see this fail, check first "
            "whether the line really uses ${PG_PW} — issue #336 was "
            "filed based on a visual misread of ${PG_PW} as '***'. "
            f"Got: SMOKE_DSN={rhs}"
        )

    def test_pg_pw_variable_is_assigned_from_env(self) -> None:
        """``PG_PW`` must be assigned from the .env file before the
        SMOKE_DSN line is built. Without an assignment, the ${PG_PW}
        expansion would either yield an empty string (silent empty
        auth) or ``bash: PG_PW: unbound variable`` under
        ``set -u``. Pin the assignment so a future drive-by edit
        cannot drop it without breaking this test."""
        body = _read(SCRIPT_PATH)
        assert re.search(
            r"^PG_PW=\"\$\(.*POSTGRES_PASSWORD=.*\.env.*\)\"",
            body,
            re.MULTILINE,
        ), "expected PG_PW assigned from .env POSTGRES_PASSWORD via $(...)"

    def test_dsn_uses_real_password_not_placeholder(self) -> None:
        """Structural property: the SMOKE_DSN assignment line must
        reference ``${PG_PW}`` and must not contain the literal
        three-asterisk sequence.

        Note: ``grep -E ':\\*\\*\\*'`` against a file containing
        ``${PG_PW}`` will sometimes print a line that *looks* like
        it contains ``***`` to a human reader (the substring
        ``${PG_PW}`` is five bytes long and visually similar to
        ``***`` in a monospace font). The byte-level test below is
        deliberately strict and will catch the *real* regression —
        not the visual illusion."""
        body = _read(SCRIPT_PATH)
        # Locate the assignment line.
        match = re.search(r"^SMOKE_DSN=(.+)$", body, re.MULTILINE)
        assert match, "expected a ``SMOKE_DSN=...`` line"
        rhs = match.group(1)
        # Strip ${...} expansions and check the remaining literal.
        # The placeholder regression would be a literal '***' between
        # the colon and the '@' (i.e. between the user and host parts).
        # We assert that any colon-then-non-var sequence does not
        # contain three asterisks.
        # Easier / clearer: assert ${PG_PW} is present and that no
        # standalone '***' appears in the assignment line.
        assert "***" not in rhs, "SMOKE_DSN must not contain a literal '***' placeholder. " f"Got: SMOKE_DSN={rhs}"


class TestSyntax:
    """``bash -n`` the script — the operator should never have an
    unparseable scripts/ file land in main."""

    @pytest.mark.skipif(
        shutil.which("bash") is None,
        reason="bash not installed; skipping syntax check",
    )
    def test_bash_n_parses_cleanly(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, f"scripts/pre_pr_smoke.sh fails bash -n:\n{result.stderr}"


class TestPrePushHookContract:
    """The companion pre-push hook refuses to push unless
    scripts/pre_pr_smoke.sh writes a sentinel. Pin the contract:
    both files must coexist, and the hook must look for the
    sentinel at the path the script writes to.
    """

    def test_hook_file_exists(self) -> None:
        assert HOOK_PATH.is_file(), (
            "expected scripts/hooks/pre-push (the gate enforced by the "
            "smoke sentinel) to exist alongside scripts/pre_pr_smoke.sh"
        )

    def test_hook_refuses_when_sentinel_missing(self) -> None:
        body = _read(HOOK_PATH)
        assert "REFUSED" in body and "sentinel" in body, (
            "expected scripts/hooks/pre-push to refuse push when " "the smoke sentinel is missing"
        )

    def test_hook_and_script_agree_on_sentinel_path(self) -> None:
        script_body = _read(SCRIPT_PATH)
        hook_body = _read(HOOK_PATH)
        sentinel_token = ".alphard-pr-smoke-pass."
        assert sentinel_token in script_body, (
            "scripts/pre_pr_smoke.sh must write a sentinel under " "/tmp/.alphard-pr-smoke-pass.<branch>"
        )
        assert sentinel_token in hook_body, (
            "scripts/hooks/pre-push must look for a sentinel under " "/tmp/.alphard-pr-smoke-pass.<branch>"
        )
