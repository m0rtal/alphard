"""Regression tests for issue #401 — Grafana/Prometheus/chownfix doc-drift cleanup.

Issue #401 (Tech-Debt: High) is the doc-drift companion to PR #399 (which
deleted the ``alphard-grafana``, ``alphard-prometheus``, and
``alphard-chownfix`` compose services but left 20+ active-prose references
to them in README.md and docs/). Post-#399 a fresh-clone operator who
follows ``docs/QUICKSTART.md`` will be told to look for containers that
no longer exist, hit ports that no longer bind, and read troubleshooting
rows that reference code paths that no longer exist.

These tests pin the post-#399 doc contract:

1. **README.md and docs/QUICKSTART.md** describe a 3-container stack
   (alphard-bot, postgres, redis) plus ``alphard-web`` (PR #394) as
   the operator UI.
2. **docs/TROUBLESHOOTING.md and docs/TESTING.md** no longer describe
   Grafana/Prometheus services as an active observability path. The
   surviving references are explicitly framed as ``regression guard``
   (the CI gates are intentionally retained so a future PR that
   accidentally re-introduces Grafana is caught) or ``_(removed, PR #399)``
   archaeology rows.
3. The ``alphard-web`` replacement path is documented in the same files
   so an operator who searches for "metrics" or "monitoring" lands on
   the right tool.

Same pattern as ``tests/test_357_pg_init_doc_cleanup.py`` and
``tests/test_347_pg_init_removal.py``: pure-fs scans, no docker daemon,
strip code-fences + comments to allow historical breadcrumbs, fail
loudly on active-prose regressions.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
QUICKSTART = REPO_ROOT / "docs" / "QUICKSTART.md"
TROUBLESHOOTING = REPO_ROOT / "docs" / "TROUBLESHOOTING.md"
TESTING = REPO_ROOT / "docs" / "TESTING.md"

# Matches standalone mentions of the removed services in active prose.
# Case-insensitive to catch Grafana / grafana / GRAFANA.
_SERVICES = ("grafana", "prometheus", "chownfix")

# Regex for code-fence stripping (matches ``` ... ``` blocks).
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# Regex for HTML comments in markdown (<!-- ... -->).
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Lines explicitly marked as archaeology (removed/superseded in #399).
# These are intentional historical breadcrumbs; per AC #1 they stay.
_ARCHAR_PHRASES = (
    "removed, pr #399",
    "removed in pr #399",
    # Bare PR #NNN refs — these show up in archaeology contexts
    # like "(PR #399 его снёс)" where the closing paren is far from
    # the number. Matches "pr #399" as a substring.
    "pr #399",
    "(superseded by pr #399)",
    "post-#399",
    "снесены в pr #399",
    "regression guard",
    "ре-интродуцит",
    "ре-интродукции",
)


def _strip_markdown_noise(text: str) -> str:
    """Strip code fences and HTML comments so we don't false-positive on a
    ``grafana`` mention inside a code sample or audit comment.
    """
    text = _CODE_FENCE_RE.sub("", text)
    text = _HTML_COMMENT_RE.sub("", text)
    return text


def _is_archaeology(line: str, neighbours: tuple[str, ...] = ()) -> bool:
    """Return True if the line is an intentional archaeology / regression-guard
    breadcrumb (per issue #401 acceptance criterion #1).

    ``neighbours`` carries the previous 2-3 lines so multi-line breadcrumb
    phrasings like ``regression\\nguard`` (split across two lines by the
    markdown soft-wrap) are still recognised.
    """
    blob = "\n".join((line, *neighbours)).lower()
    return any(phrase in blob for phrase in _ARCHAR_PHRASES)


def _active_offenders(text: str) -> list[tuple[int, str]]:
    """Return (lineno, line) for each line that mentions a removed service
    in ACTIVE prose (not archaeology, not code-fence, not HTML comment,
    not a section header — those name CI gates / row labels and may carry
    the legacy service name intentionally).

    To recognise multi-line archaeology phrasing (e.g. ``regression`` on one
    line and ``guard`` on the next), we look at the previous 8 lines too.
    The section 8.6 body has the cause paragraph mentioning
    "regression guard", then the fix subsection immediately after — the fix
    block describes what the regex looks for, not active Grafana
    troubleshooting, so it must inherit the archaeology context.
    """
    lines = text.splitlines()
    offenders: list[tuple[int, str]] = []
    WINDOW = 8
    HEADER_RE = re.compile(r"^#{2,6}\s")
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        # Section / sub-section headers name CI gates and may carry the
        # removed-service name as a label (``### 8.6 Grafana secrets guard``).
        # Those are structural, not active prose.
        if HEADER_RE.match(line):
            continue
        neighbours = tuple(lines[max(0, idx - WINDOW) : idx])
        if _is_archaeology(line, neighbours=neighbours):
            continue
        if any(service in line.lower() for service in _SERVICES):
            offenders.append((idx + 1, line.strip()))
    return offenders


# ---------------------------------------------------------------------------
# README.md — operator-facing overview
# ---------------------------------------------------------------------------


def test_readme_no_active_observability_stack_claim() -> None:
    """Issue #401 AC #1: README.md architecture overview must NOT describe
    Grafana + Prometheus as the current observability stack.

    Pre-fix line 39 said: ``состояние в Postgres, метрики в Prometheus + Grafana``.
    Post-#399 the truth is: ``alphard-web`` (PR #394) reads metrics
    directly from Postgres via SQL. The replacement sentence is in place.
    """
    text = _strip_markdown_noise(README.read_text(encoding="utf-8"))
    # Find the architecture overview section (the short paragraph that
    # appears after "## Архитектура").
    arch_idx = text.find("## Архитектура")
    assert arch_idx >= 0, "README.md must have an '## Архитектура' section"
    # Scan the next 12 lines (the overview paragraph is short).
    snippet = "\n".join(text.splitlines()[arch_idx : arch_idx + 12])
    offenders = []
    for service in _SERVICES:
        if service in snippet.lower():
            offenders.append(service)
    assert not offenders, (
        "Issue #401: README.md '## Архитектура' section still names the "
        f"removed observability services as the active path: {offenders}.\n"
        "After PR #399 the operator UI is alphard-web (PR #394) — it reads "
        "metrics directly from Postgres.\n"
        f"Snippet:\n{snippet}"
    )


def test_readme_documents_alphard_web_as_operator_ui() -> None:
    """Issue #401 AC #2: README.md must reference ``alphard-web`` so an
    operator searching for "monitoring" / "metrics" lands on the right
    tool.
    """
    text = README.read_text(encoding="utf-8")
    assert "alphard-web" in text, (
        "Issue #401: README.md must reference alphard-web as the operator "
        "UI so operators know where to look for live metrics post-#399."
    )


# ---------------------------------------------------------------------------
# docs/QUICKSTART.md — onboarding
# ---------------------------------------------------------------------------


def test_quickstart_no_active_grafana_prometheus() -> None:
    """Issue #401 AC #2: docs/QUICKSTART.md must NOT mention the removed
    services in active onboarding prose.

    Pre-fix the file mentioned grafana/prometheus/chownfix at 18 lines.
    Post-#399 + cycle163 maintainer-cron cleanup the file is clean; this
    test pins that.
    """
    text = _strip_markdown_noise(QUICKSTART.read_text(encoding="utf-8"))
    offenders = _active_offenders(text)
    assert not offenders, (
        "Issue #401: docs/QUICKSTART.md has active-prose references to the "
        "removed services. Post-#399 onboarding describes a 3-container "
        "stack (alphard-bot, postgres, redis) plus alphard-web.\n"
        "Offenders:\n" + "\n".join(f"  L{ln}: {s}" for ln, s in offenders)
    )


# ---------------------------------------------------------------------------
# docs/TROUBLESHOOTING.md — operator-facing fix recipes
# ---------------------------------------------------------------------------


def test_troubleshooting_no_active_grafana_prometheus_outside_archaeology() -> None:
    """Issue #401 AC #1: docs/TROUBLESHOOTING.md must NOT describe
    Grafana/Prometheus/chownfix as an active fix target except in
    explicit archaeology / regression-guard contexts.

    The two pre-existing rows at L111-112 already carry the
    ``_(removed, PR #399)`` prefix and so are excluded by
    ``_is_archaeology``. Sections 8.6 and 8.7 (the CI gate
    troubleshooting rows) must be reworded to call themselves
    *regression guards* — see ``tests`` below for the explicit asserts.
    """
    text = _strip_markdown_noise(TROUBLESHOOTING.read_text(encoding="utf-8"))
    offenders = _active_offenders(text)
    assert not offenders, (
        "Issue #401: docs/TROUBLESHOOTING.md has active-prose references "
        "to the removed services outside archaeology / regression-guard "
        "contexts. Wrap them with ``_(removed, PR #399)_`` or with the "
        "phrase ``regression guard`` so the doc-drift class stays closed.\n"
        "Offenders:\n" + "\n".join(f"  L{ln}: {s}" for ln, s in offenders)
    )


def test_troubleshooting_grafana_secrets_section_is_regression_guard() -> None:
    """Issue #401 AC #1: section 8.6 (Grafana secrets guard) must say
    explicitly that it is a regression guard, not active troubleshooting.
    The CI gate was retained post-#399 to catch future re-introductions.
    """
    text = TROUBLESHOOTING.read_text(encoding="utf-8")
    # Locate the 8.6 header.
    m = re.search(r"^### 8\.6.*$", text, re.MULTILINE)
    assert m is not None, "TROUBLESHOOTING.md must have an '### 8.6' section"
    # Take the section body (until the next ### header).
    start = m.end()
    rest = text[start:]
    next_header = re.search(r"^### ", rest, re.MULTILINE)
    section = rest[: next_header.start() if next_header else len(rest)]
    # Markdown soft-wrap can split "regression\nguard" across two lines; collapse
    # whitespace before substring-matching so the assertion doesn't false-fail.
    section_normalized = re.sub(r"\s+", " ", section.lower())
    assert "regression guard" in section_normalized, (
        "Issue #401: section 8.6 'Grafana secrets guard' in "
        "TROUBLESHOOTING.md must explicitly say it is a regression guard "
        "(CI gate retained after PR #399 to catch accidental "
        "re-introductions). Pre-#401 this section read as active "
        "troubleshooting for a live Grafana service, which it no longer is.\n"
        f"Section:\n{section}"
    )


# ---------------------------------------------------------------------------
# docs/TESTING.md — CI strategy
# ---------------------------------------------------------------------------


def test_testing_ops_policy_row_is_regression_guard() -> None:
    """Issue #401 AC #1: the ``Ops policy`` row in docs/TESTING.md's CI
    table must say explicitly that it is a regression guard retained
    after PR #399.

    Pre-fix it read "legacy Grafana checks; PR #399 dropped the gate" —
    misleading, because the gate is still live. Post-fix it reads
    "regression guard: PR #399 dropped Grafana/Prometheus services, но
    CI gate оставлен на случай ре-интродукции".
    """
    text = TESTING.read_text(encoding="utf-8")
    # Find the row that mentions both "Ops policy" and Grafana.
    rows = [line for line in text.splitlines() if "Ops policy" in line and "Grafana" in line]
    assert rows, "TESTING.md must have an 'Ops policy' row mentioning Grafana"
    row = rows[0].lower()
    assert "regression guard" in row, (
        "Issue #401: docs/TESTING.md 'Ops policy' row must say "
        "'regression guard' — the CI gate is still live after PR #399 "
        "(catches future accidental re-introductions of Grafana).\n"
        f"Row: {rows[0]}"
    )


# ---------------------------------------------------------------------------
# Cross-file sanity: the operator UI replacement is documented everywhere
# ---------------------------------------------------------------------------


def test_alphard_web_documented_as_operator_ui_in_all_key_files() -> None:
    """Issue #401 AC #2: every doc that used to point operators at
    Grafana must now point them at ``alphard-web`` (PR #394).
    """
    files = [
        (README, "README.md"),
        (TROUBLESHOOTING, "docs/TROUBLESHOOTING.md"),
        (TESTING, "docs/TESTING.md"),
    ]
    missing = []
    for path, label in files:
        text = path.read_text(encoding="utf-8")
        if "alphard-web" not in text and "8081" not in text:
            missing.append(label)
    assert not missing, (
        "Issue #401 AC #2: after PR #399, every operator-facing doc must "
        "point operators at alphard-web (or port 8081) — these files "
        "still reference the old observability surface without naming the "
        f"replacement: {missing}"
    )
