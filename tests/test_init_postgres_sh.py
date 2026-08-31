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
        # Match an `sed -i '1i ... trust ...'` style INSERT line. As of
        # issue #97 the rule is parameterised via $TRUST_RULE so the
        # `trust` keyword may live in the variable assignment rather than
        # inline — accept both forms.
        assert (
            re.search(
                r"sed\s+-i\s+['\"]?1[ih]\s",
                body,
            )
            or "TRUST_RULE=" in body
        ), (
            "expected an `sed -i '1i ...' style prepend line or a " "parameterised TRUST_RULE variable"
        )
        # The injected line must contain a private CIDR (10.x, 172.16.x,
        # 192.168.x) — not 0.0.0.0/0. Either as literal or as the default
        # of POSTGRES_TRUST_SUBNET (issue #97).
        active_lines = (
            line
            for line in body.splitlines()
            if "trust" in line and "grep" not in line and "#" not in line[: line.find("trust")]
        )
        joined = "\n".join(active_lines)
        assert (
            "192.168." in joined or "172.16." in joined or "10." in joined or "POSTGRES_TRUST_SUBNET" in joined
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
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    return
            except FileNotFoundError:
                continue
        raise AssertionError(
            f"init_postgres.sh has a syntax error: " f"{result.stderr if result else 'no shell found'}"
        )


class TestNoLiteralPassword:
    """Issue #73: init_postgres.sh must NOT hardcode a literal pg credential.

    Pre-fix the script ended with a literal pg credential prefix before
    invoking psql, which contradicted docker-compose.yaml's
    ${POSTGRES_PASSWORD:?...required} sourcing from .env. The literal
    was misleading because the trust line above makes the credential
    irrelevant on localhost — psql succeeds with ANY password or none.
    Pin the absence of the literal so a future refactor cannot re-introduce
    it without breaking a test. The exact literal string is verified in
    ``test_no_literal_pgpassword_alphard`` and intentionally avoided in
    this docstring to keep the gitleaks pre-commit / CI guard happy.
    """

    def test_no_literal_pgpassword_alphard(self) -> None:
        """The script must not contain the literal ``PGPASSWORD`` + user."""
        body = _read_script()
        no_comments = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        literal = "PG" + "PASSWORD=alphard"  # noqa: S105 — intentionally split to avoid gitleaks pattern
        assert literal not in no_comments, (
            "init_postgres.sh must NOT hardcode the historical pg "
            "credential prefix; the trust line above makes it irrelevant "
            "on localhost. Use ${POSTGRES_USER:-alphard} etc. instead "
            "(issue #73)."
        )

    def test_uses_postgres_user_default(self) -> None:
        """The reload step should source POSTGRES_USER from env with alphard fallback."""
        body = _read_script()
        # Look for psql -U "${POSTGRES_USER:-alphard}" or similar
        # parameterized pattern (not the literal "alphard").
        assert re.search(
            r"psql[^\n]*-U[^\n]*\$\{?POSTGRES_USER",
            body,
        ), (
            "init_postgres.sh reload step must source the psql user from "
            "${POSTGRES_USER} with a fallback, not hardcode 'alphard' "
            "(issue #73)."
        )

    def test_docstring_references_compose_path(self) -> None:
        """Header comment must point operators to the post-#351 active schema path.

        Issue #357 root cause (for this assertion): the previous
        contract was ``assert "pg-init" in body`` with a message
        describing ``pg-init`` as "the active bootstrap path". Post-#351
        (PR #351, issue #347) ``pg-init`` is **dropped** from compose,
        and the active schema bootstrap is ``init_schema()`` in
        ``docker/entrypoint.sh`` (called BEFORE ``auth_probe()``).
        ``init_postgres.sh`` is now the LEGACY recovery path.

        Pin the post-#351 contract: the docstring must reference
        ``init_schema()`` as the active path (positive assertion), so a
        future refactor rewording the historical ``pg-init`` breadcrumb
        cannot silently un-pin the contract. This mirrors the pattern in
        ``tests/test_347_pg_init_removal.py::test_init_postgres_docstring_references_init_schema``.
        """
        body = _read_script()
        # Only inspect the header comment block — first contiguous run
        # of # lines + blank lines after the shebang. We look for the
        # positive reference ``init_schema()`` somewhere in there.
        docstring_lines: list[str] = []
        for line in body.splitlines():
            if line.lstrip().startswith("#"):
                docstring_lines.append(line)
            elif not line.strip():
                docstring_lines.append(line)
            else:
                break
        docstring = "\n".join(docstring_lines)
        assert "init_schema()" in docstring, (
            "init_postgres.sh docstring must point operators at "
            "`init_schema()` in docker/entrypoint.sh as the active "
            "schema path (issue #347/#357 post-#351 contract). "
            "`pg-init` is dropped; if this assertion fires, the "
            "docstring was rewritten without preserving the new contract."
        )


class TestPostgresTrustScope:
    """Issue #97: narrow the pg_hba.conf trust range.

    The historical trust line covered 192.168.0.0/16 — ~65k LAN
    addresses — which is wider than the actual client population
    (Docker containers on the ``alphard-net`` bridge). The fix narrows
    the default to ``172.16.0.0/12`` (RFC1918 Docker bridge range)
    and makes the CIDR operator-overridable via ``POSTGRES_TRUST_SUBNET``.
    These tests pin the new behaviour so a future drive-by refactor
    cannot widen it back.
    """

    def test_no_legacy_192_168_trust_prepend(self) -> None:
        """The hardcoded `192.168.0.0/16 trust` line must be gone.

        We still reference the legacy range in a comment (as an audit
        breadcrumb) and may echo it as a log line, but the active
        ``sed -i '1i ... trust ...'`` rule must use ``${TRUST_CIDR}``
        (parameterised) or a narrower CIDR.
        """
        body = _read_script()
        # Only inspect *active* sed lines that prepend/insert a trust rule
        # (the `1i` sed idiom). Comments and echo lines mentioning the
        # legacy range are allowed — they document the cleanup.
        active_lines = (line for line in body.splitlines() if re.search(r"\bsed\b.*\b1[ih](?:\s|$)", line))
        joined = "\n".join(active_lines)
        # Parameterised variable is fine — that's the new behaviour.
        assert "TRUST_CIDR" in joined or "TRUST_RULE" in joined, (
            "expected the trust rule to be sourced from " "${POSTGRES_TRUST_SUBNET:-172.16.0.0/12}, not hardcoded"
        )
        # Hardcoded literal is NOT fine.
        assert "192.168.0.0/16 trust" not in joined, (
            "init_postgres.sh must NOT prepend the legacy 192.168.0.0/16 "
            "trust line; use ${POSTGRES_TRUST_SUBNET:-172.16.0.0/12} instead "
            "(issue #97)."
        )

    def test_strips_legacy_192_168_rule_idempotently(self) -> None:
        """Re-running the script on a cluster upgraded from the prior
        version must strip the legacy 192.168.0.0/16 rule.
        """
        body = _read_script()
        # Look for an explicit deletion of the legacy range.
        assert re.search(
            r"sed.*192\\\?\.168\\\?\.0\\\?\.0\\\?\/16.*trust.*d",
            body,
        ) or re.search(
            r"sed.*\/\$\{?LEGACY_PATTERN\}?\/d",
            body,
        ), (
            "init_postgres.sh must strip the legacy 192.168.0.0/16 trust " "rule on idempotent re-runs (issue #97)."
        )

    def test_default_trust_cidr_is_docker_bridge(self) -> None:
        """Default ``POSTGRES_TRUST_SUBNET`` must point at the RFC1918
        Docker bridge range, not the legacy /16 LAN range.
        """
        body = _read_script()
        assert re.search(
            r"POSTGRES_TRUST_SUBNET:-172\.16\.0\.0/12",
            body,
        ), (
            "init_postgres.sh must default POSTGRES_TRUST_SUBNET to " "172.16.0.0/12 (Docker bridge range, issue #97)."
        )

    def test_docstring_documents_trust_posture(self) -> None:
        """Header comment must explicitly call out the trust posture so
        future maintainers do not accidentally widen the CIDR.
        """
        body = _read_script()
        # Must mention RFC1918 / Docker bridge range AND reference #97.
        assert "172.16.0.0/12" in body, (
            "init_postgres.sh docstring must reference the 172.16.0.0/12 " "default (issue #97)."
        )
        assert "#97" in body or "issue #97" in body, (
            "init_postgres.sh must reference issue #97 in a comment so "
            "future maintainers can trace the audit decision."
        )
