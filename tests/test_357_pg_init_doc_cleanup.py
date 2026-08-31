"""Regression tests for issue #357 — extend #355 cleanup to docs/ + README + test_init_postgres_sh.

Issue #357 is the follow-up to the cycle126 lesson (issue #355, PR #356).
PR #356 applied the lesson narrowly to ``scripts/quickstart.sh`` and
``scripts/init_postgres.sh`` (the false-positive-warning files). It missed
four other locations that still describe ``pg-init`` as the active path:

  1. ``README.md:248`` — operator-facing table row says
     ``cron/pg-init not deployed`` (Phase 1.6 watchdog); reality post-#351
     is that ``pg-init`` is DROPPED from compose entirely, and ``init_schema()``
     in ``docker/entrypoint.sh`` is the active schema path.

  2. ``docs/QUICKSTART.md:52`` — one-shot services list still names
     ``alphard-pg-init``; post-#356 the ``ONE_SHOT`` array is
     ``("alphard-chownfix")``.

  3. ``docs/QUICKSTART.md:157`` — troubleshooting table mentions ``pg-init``
     hangs on ``apk add postgresql-client``; no service exists to fix.

  4. ``docs/SECURITY.md:89,114`` — security narrative still names
     ``pg-init`` as the active schema+trust service.

Plus a stale regression test in ``tests/test_init_postgres_sh.py``
(``test_docstring_references_compose_path``, line 156) whose assertion
message claims ``pg-init`` is "the active bootstrap path" — post-#351 this
is factually wrong. The test passes by accident because the literal
substring ``pg-init`` still appears in the new docstring (as a historical
mention). Mirror PR #356's positive-assertion pattern from
``tests/test_347_pg_init_removal.py::test_init_postgres_docstring_references_init_schema``.

These tests pin the post-#351 / post-#356 doc contract so a future
"let me re-document pg-init" PR is blocked unless it also re-introduces
the service via #347 (which we don't want).

Tests are pure-fs (no docker daemon). They scan only the active
(non-comment, non-audit) text to allow historical breadcrumbs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
QUICKSTART = REPO_ROOT / "docs" / "QUICKSTART.md"
SECURITY = REPO_ROOT / "docs" / "SECURITY.md"
INIT_POSTGRES = REPO_ROOT / "scripts" / "init_postgres.sh"


def _strip_md_code_fences(text: str) -> str:
    """Strip ``` fenced code blocks from markdown so we don't false-positive on
    a ``pg-init`` inside a code sample (e.g. troubleshooting recipes).
    """
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _active_lines(text: str) -> list[str]:
    """Drop comment lines (#-prefixed) and blank lines so historical breadcrumb
    comments in scripts don't fire the active-path check.
    """
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


# ---------------------------------------------------------------------------
# README.md
# ---------------------------------------------------------------------------


def test_readme_no_pg_init_described_as_active() -> None:
    """README.md MUST NOT name ``pg-init`` as an active operational path
    (issue #357).

    Post-#351 (``pg-init`` DROPPED from docker-compose.yaml), the table
    row at line 248 (``cron/pg-init not deployed | Phase 1.6 in-process
    watchdog``) is factually wrong: pg-init does not exist in compose at
    all. The active schema+trust bootstrap is ``init_schema()`` in
    ``docker/entrypoint.sh`` (called before ``auth_probe()``).

    Allow audit/historical mentions in HTML comments (``<!-- ... -->``)
    and in fenced code blocks. Block:
      - Active table cells that name ``pg-init`` as the thing being
        watched / not deployed / etc.
      - Any list item or table cell that lists ``pg-init`` in the
        present tense as if it were a deployable service.
    """
    text = README.read_text(encoding="utf-8")
    # Strip fenced code blocks so audit breadcrumb examples don't count.
    text_no_code = _strip_md_code_fences(text)
    # Strip HTML comments (``<!-- ... -->``) — these are intentionally
    # audit-only breadcrumbs that should NOT trigger an active-path check.
    text_no_comments = re.sub(r"<!--.*?-->", "", text_no_code, flags=re.DOTALL)

    bad: list[tuple[int, str]] = []
    for line_no, line in enumerate(text_no_comments.splitlines(), start=1):
        if "pg-init" not in line.lower():
            continue
        # Active prose: table cells (|), list items (- or * at line start).
        is_active = line.lstrip().startswith(("|", "-", "*"))
        if not is_active:
            continue
        bad.append((line_no, line.strip()))

    block_tail = "\n".join(f"  L{ln}: {ln_text}" for ln, ln_text in bad)
    assert not bad, (
        "README.md still names `pg-init` as an active operational "
        "service (issue #357). pg-init was DROPPED from "
        "docker-compose.yaml by PR #351 / issue #347; the active "
        "schema+trust bootstrap is init_schema() in "
        "docker/entrypoint.sh. Lines:\n" + block_tail
    )


# ---------------------------------------------------------------------------
# docs/QUICKSTART.md
# ---------------------------------------------------------------------------


def test_quickstart_one_shot_no_alphard_pg_init() -> None:
    """docs/QUICKSTART.md MUST NOT name ``alphard-pg-init`` in the one-shot
    services example (issue #357).

    Post-#356 the real ``ONE_SHOT`` array in ``scripts/quickstart.sh:311``
    is ``("alphard-chownfix")`` (pg-init removed). The doc text at
    line 51-52 still names ``alphard-pg-init`` alongside
    ``alphard-chownfix`` as a checked one-shot service. Block any
    active prose in the doc that mentions ``alphard-pg-init`` as if it
    were still a service. Allow HTML comments (``<!-- ... -->``) for
    audit breadcrumbs.
    """
    text = QUICKSTART.read_text(encoding="utf-8")
    no_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    # Walk the doc and track whether each non-blank line is inside an
    # active list context (numbered, bullet, or table cell). A line
    # inside such a context that names ``alphard-pg-init`` is a bug.
    bad: list[tuple[int, str]] = []
    in_active_list = False
    for line_no, line in enumerate(no_comments.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            # Blank lines DO NOT break list continuation; only a
            # non-list line at column 0 does.
            continue
        if re.match(r"^\s*(\d+\.|-|\*)\s", line):
            in_active_list = True
        elif stripped.startswith("|"):
            in_active_list = True
        elif not line.startswith((" ", "\t")):
            # Non-indented, non-list line — closes any active list.
            in_active_list = False
        if in_active_list and "alphard-pg-init" in line:
            bad.append((line_no, line.rstrip()))

    tail = "\n".join(f"  L{ln}: {ln_text}" for ln, ln_text in bad)
    assert not bad, (
        "docs/QUICKSTART.md still names `alphard-pg-init` as an active "
        "one-shot service in list context (issue #357). Post-#356 the "
        "real ONE_SHOT array in scripts/quickstart.sh is "
        'ONE_SHOT=("alphard-chownfix"). Lines:\n' + tail
    )


def test_quickstart_no_pg_init_troubleshooting_entry() -> None:
    """docs/QUICKSTART.md MUST NOT include a pg-init troubleshooting row
    (issue #357).

    The "Why does this exist?" table at line 157 had a row about
    ``pg-init`` hanging on ``apk add postgresql-client``. Since pg-init
    no longer exists, the row is misleading — there is no service to fix.
    Replace with a post-#351 entry pointing at ``init_schema()`` and
    ``auth_probe()``.
    """
    text = QUICKSTART.read_text(encoding="utf-8")
    no_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    bad: list[tuple[int, str]] = []
    for line_no, line in enumerate(no_comments.splitlines(), start=1):
        # Active table cell (pipe-prefixed) that mentions ``pg-init`` as
        # a thing to fix / as a cause of failure.
        if (
            line.lstrip().startswith("|")
            and "pg-init" in line.lower()
            and re.search(
                r"\b(hangs|fails|breaks|fix|cause|on `apk|`alpine)\b",
                line,
                flags=re.IGNORECASE,
            )
        ):
            bad.append((line_no, line.strip()))

    fix_tail = "\n".join(f"  L{ln}: {ln_text}" for ln, ln_text in bad)
    assert not bad, (
        "docs/QUICKSTART.md troubleshooting table still references "
        "`pg-init` (issue #357). pg-init no longer exists in compose; "
        "replace the row with init_schema()+auth_probe() context. "
        "Lines:\n" + fix_tail
    )


# ---------------------------------------------------------------------------
# docs/SECURITY.md
# ---------------------------------------------------------------------------


def test_security_no_pg_init_as_active_service() -> None:
    """docs/SECURITY.md MUST NOT describe ``pg-init`` as an active compose
    service (issue #357).

    Two locations:

      - Line 89: ``docker-compose.yaml has a one-shot `pg-init` service
        that prepends a `host all all <CIDR> trust` rule to pg_hba.conf``
        — factually wrong post-#351: compose no longer has pg-init.

      - Line 114: ``docker-compose.yaml `pg-init` and
        scripts/init_postgres.sh now default POSTGRES_TRUST_SUBNET=...``
        — also references the dropped service.

    Both sentences need to be rewritten to point at ``init_schema()`` in
    ``docker/entrypoint.sh`` as the active schema path.
    """
    text = SECURITY.read_text(encoding="utf-8")
    no_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    bad: list[tuple[int, str]] = []
    for line_no, line in enumerate(no_comments.splitlines(), start=1):
        # Active bullet point (- foo:) that names pg-init with active voice
        # ("has a ... service", "default ... =", "now strip").
        if not line.lstrip().startswith("-"):
            continue
        if "pg-init" not in line.lower():
            continue
        # Allow past-tense / "removed by" / "dropped" breadcrumbs:
        # ``(removed by PR #351)``, ``(was dropped)``.
        # Block active voice: ``has a one-shot``, ``prepends``, ``now default``.
        if re.search(
            r"\b(has a|now default|prepends|is the active|runs on startup|strips the)\b",
            line,
            flags=re.IGNORECASE,
        ):
            bad.append((line_no, line.strip()))

    sec_tail = "\n".join(f"  L{ln}: {ln_text}" for ln, ln_text in bad)
    assert not bad, (
        "docs/SECURITY.md still describes `pg-init` as an active "
        "compose service (issue #357). pg-init was DROPPED "
        "(PR #351, issue #347); the active schema+trust bootstrap is "
        "init_schema() + auth_probe() in docker/entrypoint.sh. "
        "Lines:\n" + sec_tail
    )


# ---------------------------------------------------------------------------
# init_postgres.sh docstring (drives the broken test in test_init_postgres_sh.py)
# ---------------------------------------------------------------------------


def test_init_postgres_docstring_references_init_schema() -> None:
    """scripts/init_postgres.sh docstring MUST mention ``init_schema()`` as the
    active schema path (issue #357, post-#351 contract).

    This is the positive assertion that supersedes the broken
    ``tests/test_init_postgres_sh.py::test_docstring_references_compose_path``
    (which asserted "pg-init" was the active bootstrap path — factually
    wrong post-#351; test passed by accident on substring match).

    Strip comment lines so historical breadcrumb mentions of pg-init
    ("the script only runs in recovery when pg-init cannot be re-invoked")
    don't satisfy the check — we want a positive reference to
    ``init_schema()`` somewhere in the docstring, not just absence of
    ``pg-init``.
    """
    text = INIT_POSTGRES.read_text(encoding="utf-8")
    # Look only at the docstring: first contiguous block of comment
    # lines at the top of the file.
    docstring_lines: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            docstring_lines.append(line)
        elif not line.strip():
            docstring_lines.append(line)
        else:
            break
    docstring = "\n".join(docstring_lines)

    assert "init_schema()" in docstring, (
        "scripts/init_postgres.sh docstring must mention `init_schema()` "
        "as the active schema path (issue #357 / post-#351 contract). "
        "If a future refactor rewrites this comment, it must continue to "
        "point operators at init_schema() in docker/entrypoint.sh rather "
        "than at the dropped `pg-init` service."
    )
