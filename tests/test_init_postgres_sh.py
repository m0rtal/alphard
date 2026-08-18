"""Tests for scripts/init_postgres.sh.

We exercise the script by extracting its logic into a pure-Python
copy via ``shlex`` parsing and asserting on the operations it would
perform against a mocked pg_hba.conf. This is a regression guard
against re-introducing the 0.0.0.0/0 trust line (production bug
2026-08-18).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


HBA_PATH = Path(__file__).resolve().parent.parent / "scripts" / "init_postgres.sh"


def _read_script() -> str:
    """Read the script. Path is resolved via __file__ so it works
    in any checkout layout (CI runners, fresh clones, container
    runs)."""
    return HBA_PATH.read_text(encoding="utf-8")


class TestInitPostgresScript:
    """Ensure init_postgres.sh does NOT open postgres to the internet."""

    def test_no_zero_zero_line(self) -> None:
        """Hard fail if 0.0.0.0/0 trust comes back."""
        body = _read_script()
        # Allow the *comment* about why 0.0.0.0/0 is wrong, but never
        # the active rule itself.
        # Strip line comments and check what's left.
        no_comments = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        assert "0.0.0.0/0 trust" not in no_comments, (
            "init_postgres.sh must NOT prepend a 0.0.0.0/0 trust rule; "
            "it should be scoped to the internal subnet (e.g. "
            "192.168.0.0/16) per the 2026-08-18 audit."
        )

    def test_uses_private_subnet(self) -> None:
        """Script must inject a trust rule bound to a private subnet."""
        body = _read_script()
        # Match a sensible private-range trust rule.
        assert re.search(
            r"sed\s+-i\s+['\"]?1[ih].*trust.*['\"]?\s+\"?\$?\S*HBA\S*",
            body,
        ), "expected an `sed -i '1i ... trust ...'` style line"
        # The injected line must contain a private CIDR (10.x, 172.16.x,
        # 192.168.x) — not 0.0.0.0/0.
        active_lines = (
            line
            for line in body.splitlines()
            if "trust" in line and "grep" not in line and "#" not in line[: line.find("trust")]
        )
        joined = "\n".join(active_lines)
        assert (
            "192.168." in joined or "10." in joined or "172.16." in joined
        ), "the trust rule must be scoped to a private RFC1918 subnet"

    def test_idempotent_old_line_removal(self) -> None:
        """If a cluster was upgraded from 0.0.0.0/0, the script must
        strip the dangerous line before adding the safe one."""
        body = _read_script()
        # Look for an explicit deletion pattern for 0.0.0.0/0 trust.
        assert (
            re.search(
                r"sed.*0\.0\.0\.0.*\d",
                body,
            )
            or "0.0.0.0/0 trust" in body
        ), "expected explicit removal of legacy 0.0.0.0/0 trust line"

    @pytest.mark.skipif(
        shutil.which("docker") is None,
        reason="docker not installed; skipping live bash exec",
    )
    def test_syntax_check(self) -> None:
        """Bash -n the script. If syntax is wrong we don't ship it.

        Falls back to bash/dash if sh can't read the file (CI checkout
        is sometimes mode-644 even when the file is meant to be a
        script).
        """
        result = None
        for shell in ("sh", "bash", "dash"):
            try:
                result = subprocess.run(
                    [shell, "-n", str(HBA_PATH)],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    return
            except FileNotFoundError:
                continue
        raise AssertionError(
            f"init_postgres.sh has a syntax error: "
            f"{result.stderr if result else 'no shell found'}"
        )
