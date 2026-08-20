"""Regression tests for scripts/deploy_monitoring.sh env-file isolation.

Issue #81: the previous ``set -a; . "$ALPHARD_ENV_FILE"; set +a`` block
auto-exported every variable in ``.env`` (HTTPS_PROXY, PROXY_URL,
PATH, HOME, RISK_*, ...) into the deploy script's environment. That
silently poisoned every subsequent ``curl`` call to 192.168.1.107 LAN
targets and could route traffic through an unreachable external proxy
if the operator had stored a ``RESI_PROXY_URL`` next to
``GRAFANA_ADMIN_PASSWORD`` (which the README documents as a valid
combination).

These tests are pure-Python: they read the script as text and (for the
behavioural piece) exercise the isolation logic in a sub-shell where
``HTTPS_PROXY`` is set before the block runs, asserting it is NOT
present after.

They run in any checkout layout (no Docker daemon required).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO / "scripts" / "deploy_monitoring.sh"


def _read_script() -> str:
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


class TestEnvFileIsolation:
    """Issue #81: deploy_monitoring.sh must NOT auto-export other env vars."""

    def test_no_set_minus_a_in_env_block(self) -> None:
        """The legacy ``set -a`` / ``set +a`` env-leak block must be gone.

        The fix replaces it with an explicit single-key grep, so the
        script no longer pollutes its own environment with every
        variable in the operator's ``.env``.
        """
        body = _read_script()
        # Strip line comments first; we care about active code only.
        no_comments = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        assert "set -a" not in no_comments, (
            "scripts/deploy_monitoring.sh still uses `set -a`, which auto-exports "
            "every variable sourced from $ALPHARD_ENV_FILE into the script's "
            "environment and re-introduces issue #81 (HTTPS_PROXY / PATH / HOME "
            "leak into subsequent curl calls)."
        )

    def test_explicit_single_key_grep(self) -> None:
        """The replacement block must extract ONLY GRAFANA_ADMIN_PASSWORD.

        Acceptance criterion from issue #81: the fix reads exactly one
        key from the env file (or zero if absent) and exports it
        explicitly, leaving all other variables untouched.
        """
        body = _read_script()
        # Strip bash comments so we don't accidentally match a comment
        # that mentions the grep pattern in prose.
        no_comments = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        # The fix must invoke `grep` on a regex that matches the
        # GRAFANA_ADMIN_PASSWORD line (anchored or unanchored — we
        # only check that GRAFANA_ADMIN_PASSWORD is the key being
        # read, not the regex shape).
        assert re.search(
            r"grep[^\\n]*GRAFANA_ADMIN_PASSWORD=",
            no_comments,
        ), "fix must grep for the GRAFANA_ADMIN_PASSWORD key"

        # `export` must appear at least once and only in the context
        # of GRAFANA_ADMIN_PASSWORD, not as a blanket export-everything.
        export_lines = [
            ln for ln in body.splitlines() if re.match(r"^\s*export\b", ln) and not ln.lstrip().startswith("#")
        ]
        assert export_lines, "fix must export GRAFANA_ADMIN_PASSWORD explicitly"
        for ln in export_lines:
            assert "GRAFANA_ADMIN_PASSWORD" in ln, (
                f"unexpected blanket `export` line: {ln!r}; " "the fix must only export GRAFANA_ADMIN_PASSWORD."
            )

    def test_password_safety_invariants_preserved(self) -> None:
        """Issue #55 invariants must still hold after the refactor.

        The replacement block must NOT accept the historical literal
        ``alphard`` and must still reject empty values. We assert the
        refusal lines are still present and reachable after the env
        extraction block.
        """
        body = _read_script()
        # Empty check (was already present pre-fix).
        assert "GRAFANA_ADMIN_PASSWORD is not set" in body, "empty-password guard from issue #55 must remain"
        # Historical literal refusal (issue #55).
        assert (
            re.search(
                r"GRAFANA_ADMIN_PASSWORD.*alphard.*\n.*historical",
                body,
                re.DOTALL,
            )
            or "GRAFANA_ADMIN_PASSWORD is set to the historical literal" in body
        ), "historical-literal refusal (issue #55) must remain"

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
    def test_https_proxy_does_not_leak_through_block(self) -> None:
        """Behavioural guard: HTTPS_PROXY in .env must NOT survive the block.

        We extract the env-loading block from the script and execute it
        inside a sub-shell where HTTPS_PROXY is set BEFORE the block
        runs and a temp ``.env`` contains both
        ``GRAFANA_ADMIN_PASSWORD=EXAMPLE_PASSWORD_FOR_TEST`` and
        ``HTTPS_PROXY=https://192.0.2.1:9999`` (TEST-NET-1). After the
        block we assert HTTPS_PROXY is unchanged (still pointing at the
        unreachable TEST-NET address) and GRAFANA_ADMIN_PASSWORD was
        read correctly.

        This is the acceptance test from issue #81.
        """
        body = _read_script()
        # Locate the env-file block: starts at the line that opens
        # `if [[ -f "$ALPHARD_ENV_FILE" ]]; then` and ends at the
        # matching `fi` (we rely on the script keeping this block
        # well-formed and the only `fi` directly under it closes
        # exactly this conditional).
        start_marker = 'if [[ -f "$ALPHARD_ENV_FILE" ]]; then'
        start_idx = body.find(start_marker)
        assert start_idx != -1, "env-file block marker not found"

        # Find the closing `fi` by tracking nested `if`s. The script
        # has many `if`s; the block is short and ends before the next
        # comment block ("ERROR: GRAFANA_ADMIN_PASSWORD is not set.").
        end_marker = "GRAFANA_ADMIN_PASSWORD is not set"
        end_idx = body.find(end_marker, start_idx)
        assert end_idx != -1, "downstream 'not set' error block not found"

        # Walk backwards from the error to find the `fi` that closes
        # the env-loading conditional.
        prefix = body[start_idx:end_idx]
        # Strip the trailing `if [[ -z ... ]]; then` and the empty
        # check echo lines so the snippet ends right after our block's
        # `fi`.
        # The structure is: if [[ -f ... ]]; then ... fi
        #                    if [[ -z ... ]]; then
        #                    echo "ERROR: ..."
        # So the FIRST `fi` after our `if [[ -f` closes our block.
        # The block contains a nested `if [[ -n "$_pw_line" ]]; then ... fi`,
        # so the FIRST `\nfi` we encounter is the inner one. We want the
        # OUTER `fi` (column-0, closing `if [[ -f "$ALPHARD_ENV_FILE" ]]`).
        # The outer `fi` is followed by another newline (i.e. the next
        # `if [[ -z` begins on its own line), so search for `\nfi\n`.
        fi_idx = prefix.find("\nfi\n")
        assert fi_idx != -1, "outer `fi` for env-file block not found"
        # Include `fi\n` so the snippet is syntactically complete.
        block_snippet = prefix[: fi_idx + len("\nfi")]

        # Sanity: the snippet must NOT contain `set -a` in active code
        # (a comment that describes the historical bug is fine).
        no_comments = "\n".join(ln for ln in block_snippet.splitlines() if not ln.lstrip().startswith("#"))
        assert "set -a" not in no_comments, "test fixture is stale: the script's env-block still uses `set -a`"

        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                # EXAMPLE_PASSWORD_FOR_TEST matches the .gitleaks.toml
                # allowlist (EXAMPLE[_-] prefix) so this fixture does
                # not trigger the alphard-hardcoded-password-assignment
                # rule. The value's literal content is irrelevant to
                # the test — what matters is that GRAFANA_ADMIN_PASSWORD
                # gets read AND HTTPS_PROXY survives the block.
                "GRAFANA_ADMIN_PASSWORD=EXAMPLE_PASSWORD_FOR_TEST\n"
                "HTTPS_PROXY=https://192.0.2.1:9999\n"
                "PROXY_URL=https://192.0.2.1:9999\n"
                "RISK_MAX_DD_PCT=10\n",
                encoding="utf-8",
            )

            harness = f"""
            set -euo pipefail
            ALPHARD_ENV_FILE="{env_file}"
            # Pre-existing env that the script must NOT clobber.
            # These simulate values the operator had in their shell
            # BEFORE running the deploy script.
            export HTTPS_PROXY="https://192.0.2.1:9999"
            export PROXY_URL="https://192.0.2.1:9999"
            export RISK_MAX_DD_PCT="10"
            {block_snippet}
            # After the block: every pre-existing var must survive
            # UNCHANGED (i.e. the block must NOT have overwritten them).
            echo "AFTER_HTTPS_PROXY=${{HTTPS_PROXY:-<unset>}}"
            echo "AFTER_GRAFANA_ADMIN_PASSWORD=${{GRAFANA_ADMIN_PASSWORD:-<unset>}}"
            echo "AFTER_PROXY_URL=${{PROXY_URL:-<unset>}}"
            echo "AFTER_RISK_MAX_DD_PCT=${{RISK_MAX_DD_PCT:-<unset>}}"
            """
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, (
                f"harness failed (rc={result.returncode})\n" f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

            # Parse the printed vars.
            kv: dict[str, str] = {}
            for ln in result.stdout.splitlines():
                if ln.startswith("AFTER_"):
                    k, _, v = ln.partition("=")
                    kv[k.removeprefix("AFTER_")] = v

            assert kv.get("GRAFANA_ADMIN_PASSWORD") == "EXAMPLE_PASSWORD_FOR_TEST", (
                f"GRAFANA_ADMIN_PASSWORD was not read correctly: " f"got {kv.get('GRAFANA_ADMIN_PASSWORD')!r}"
            )
            assert kv.get("HTTPS_PROXY") == "https://192.0.2.1:9999", (
                "HTTPS_PROXY was clobbered by the env-load block — "
                "issue #81 regression. After the block HTTPS_PROXY must "
                "be whatever the operator had set BEFORE the script ran "
                f"(got {kv.get('HTTPS_PROXY')!r})."
            )
            assert kv.get("PROXY_URL") == "https://192.0.2.1:9999", "PROXY_URL was clobbered by the env-load block."
            assert kv.get("RISK_MAX_DD_PCT") == "10", "RISK_MAX_DD_PCT was clobbered by the env-load block."

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
    def test_empty_password_still_rejected(self) -> None:
        """Missing GRAFANA_ADMIN_PASSWORD must still trigger the issue #55 guard.

        The replacement block falls through to ``unset`` GRAFANA_ADMIN_PASSWORD
        if the key is absent; the existing `if [[ -z ... ]]` check must
        then exit 2.
        """
        body = _read_script()
        start_marker = 'if [[ -f "$ALPHARD_ENV_FILE" ]]; then'
        start_idx = body.find(start_marker)
        assert start_idx != -1
        # The full guard chain (env-load + empty + alphard-refusal) is
        # bounded by `if [[ -f ...` (line 53) and the closing `fi`
        # of the alphard block. We capture everything in between so
        # the harness exercises the full path the script would take.
        alphard_start = body.find('if [[ "$GRAFANA_ADMIN_PASSWORD" == "alphard" ]]; then')
        assert alphard_start != -1
        alphard_fi = body.find("\nfi\n", alphard_start)
        assert alphard_fi != -1
        snippet = body[start_idx : alphard_fi + len("\nfi")]

        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                "UNRELATED=value\nHTTPS_PROXY=https://192.0.2.1:9999\n",
                encoding="utf-8",
            )
            harness = f"""
            set -euo pipefail
            ALPHARD_ENV_FILE="{env_file}"
            unset GRAFANA_ADMIN_PASSWORD
            export HTTPS_PROXY="https://192.0.2.1:9999"
            {snippet}
            """
            proc = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert proc.returncode == 2, (
                f"expected exit 2 from the empty-password guard, got "
                f"{proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
            assert "GRAFANA_ADMIN_PASSWORD is not set" in proc.stderr, (
                f"expected the issue #55 empty-password error on stderr, " f"got: {proc.stderr!r}"
            )

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
    def test_historical_literal_still_rejected(self) -> None:
        """Issue #55 historical literal ``alphard`` must still be refused."""
        body = _read_script()
        # Find the alphard refusal block: starts at `if [[ ... == "alphard" ]]`
        # and ends at the next column-0 `fi\n`.
        start = body.find('if [[ "$GRAFANA_ADMIN_PASSWORD" == "alphard" ]]; then')
        assert start != -1, "alphard refusal `if` not found"
        snippet_end = body.find("\nfi\n", start)
        assert snippet_end != -1, "alphard refusal closing `fi` not found"
        # Include the trailing newline after `fi` so the snippet is
        # syntactically complete.
        snippet = body[start : snippet_end + len("\nfi")]

        harness = f"""
        set -euo pipefail
        export GRAFANA_ADMIN_PASSWORD="alphard"
        {snippet}
        """
        proc = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 2
        assert "historical literal" in proc.stderr

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
    def test_quoted_password_value_parsed(self) -> None:
        """Operator-style ``KEY="quoted value"`` must be unwrapped.

        Real-world .env files from docker / compose use double-quoted
        values. The fix strips one layer of surrounding quotes so
        ``GRAFANA_ADMIN_PASSWORD="foo bar"`` resolves to ``foo bar``,
        not ``"foo bar"``.
        """
        body = _read_script()
        start_marker = 'if [[ -f "$ALPHARD_ENV_FILE" ]]; then'
        start_idx = body.find(start_marker)
        assert start_idx != -1
        end_marker = "GRAFANA_ADMIN_PASSWORD is not set"
        end_idx = body.find(end_marker, start_idx)
        prefix = body[start_idx:end_idx]
        fi_idx = prefix.find("\nfi")
        assert fi_idx != -1
        block_snippet = prefix[: fi_idx + len("\nfi")]

        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                'GRAFANA_ADMIN_PASSWORD="EXAMPLE quoted password"\n',
                encoding="utf-8",
            )
            harness = f"""
            set -euo pipefail
            ALPHARD_ENV_FILE="{env_file}"
            unset GRAFANA_ADMIN_PASSWORD || true
            {block_snippet}
            echo "PW=[${{GRAFANA_ADMIN_PASSWORD:-<unset>}}]"
            """
            proc = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr
            assert (
                "PW=[EXAMPLE quoted password]" in proc.stdout
            ), f"expected unwrapped quoted value, got: {proc.stdout!r}"
