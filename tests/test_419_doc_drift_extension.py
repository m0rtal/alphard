"""Regression tests for issue #419 — doc-drift extension past PR #408.

Issue #419 (Tech-Debt: High) is the follow-up audit to PR #408
(Closes #401). PR #408's regression guard only covered 4 of 12
operator-facing docs:

  - README.md
  - docs/QUICKSTART.md
  - docs/TROUBLESHOOTING.md
  - docs/TESTING.md

8 other tracked docs were left with active-prose references to the
removed services (``grafana``, ``prometheus``, ``chownfix``):

  - API.md
  - ARCHITECTURE.md
  - CONTRIBUTING.md
  - DOCS-INDEX.md
  - docs/DEPLOY-ENV.md
  - docs/PHASE2-8-METRICS.md
  - docs/PHASE2-ROADMAP.md
  - docs/SECURITY.md

These tests pin the post-#419 doc contract: every active-prose
Grafana/Prometheus/chownfix mention in those 8 docs must be wrapped
in an archaeology marker (e.g. ``(removed, PR #399)``) or moved
inside a code-fence / archaeology section so the doc-drift class
stays closed.

The helpers (``_strip_markdown_noise``, ``_active_offenders``,
``_is_archaeology``) are re-used from
``tests/test_401_grafana_prometheus_doc_cleanup.py`` via local
re-implementation to keep the test file self-contained (PR #408
lesson: each issue gets its own regression test file).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

API = REPO_ROOT / "API.md"
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
DOCS_INDEX = REPO_ROOT / "DOCS-INDEX.md"
DEPLOY_ENV = REPO_ROOT / "docs" / "DEPLOY-ENV.md"
PHASE2_METRICS = REPO_ROOT / "docs" / "PHASE2-8-METRICS.md"
PHASE2_ROADMAP = REPO_ROOT / "docs" / "PHASE2-ROADMAP.md"
SECURITY = REPO_ROOT / "docs" / "SECURITY.md"

# Matches standalone mentions of the removed services in active prose.
_SERVICES = ("grafana", "prometheus", "chownfix")

# Lines explicitly marked as archaeology (removed/superseded in #399).
# Same set as test_401, with one extension: "(post-#399, removed in PR #399)"
# covers the SECURITY.md archaeology banner phrasing. ``pre-#399`` and
# ``post-#399`` mark historical / replacement framing.
_ARCHAR_PHRASES = (
    "removed, pr #399",
    "removed in pr #399",
    "pr #399",
    "(superseded by pr #399)",
    "post-#399",
    "pre-#399",
    "снесены в pr #399",
    "regression guard",
    "ре-интродуцит",
    "ре-интродукции",
)

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Mermaid code-blocks (```mermaid ... ```) are stripped already by the
# generic code-fence regex; this constant is here for documentation.
_MERMAID_RE = re.compile(r"```mermaid.*?```", re.DOTALL)
# Markdown table row marker. Tables are NOT stripped wholesale because
# a row can carry an archaeology marker inline (e.g. `_(removed, PR #399)_`).
_TABLE_ROW_RE = re.compile(r"^\s*\|")


def _strip_markdown_noise(text: str) -> str:
    """Strip code fences and HTML comments so we don't false-positive
    on a ``grafana`` mention inside a code sample or audit comment.
    """
    text = _CODE_FENCE_RE.sub("", text)
    text = _HTML_COMMENT_RE.sub("", text)
    return text


def _is_archaeology(line: str, neighbours: tuple[str, ...] = ()) -> bool:
    """Return True if the line is an intentional archaeology /
    regression-guard breadcrumb (per issue #401 AC #1 + issue #419).

    ``neighbours`` carries the previous WINDOW lines (backward context)
    so multi-line breadcrumb phrasings like ``regression\\nguard``
    (split across two lines by the markdown soft-wrap) are still
    recognised. Test #419's docs often name a removed service on
    one line and add the ``_(PR #399)_`` archaeology marker on the
    next line — the parent ``_active_offenders`` call passes both
    backward AND forward context as neighbours so this case is
    covered.
    """
    blob = "\n".join((line, *neighbours)).lower()
    return any(phrase in blob for phrase in _ARCHAR_PHRASES)


def _active_offenders(text: str) -> list[tuple[int, str]]:
    """Return (lineno, line) for each line that mentions a removed
    service in ACTIVE prose (not archaeology, not code-fence, not
    HTML comment, not a section header).

    Tables are scanned line-by-line (so an archaeology row's
    ``_(removed, PR #399)_`` marker suppresses the rest of that row's
    mentions) and NOT stripped wholesale.

    The archaeology check looks at both the previous WINDOW lines
    (backward context) and the next FORWARD_WINDOW lines (forward
    context). The forward window is what catches the
    "name the removed service → archaeology marker on the next
    line" pattern that several #419 fixes use (e.g. ``> through
    Postgres and emitting metrics on :8765 (text-format endpoint
    at ... primary reader is alphard-web, PR #394, on :8081).
    _(Prometheus scraper removed in PR #399.)_``).
    """
    lines = text.splitlines()
    offenders: list[tuple[int, str]] = []
    WINDOW = 8
    FORWARD_WINDOW = 4
    HEADER_RE = re.compile(r"^#{2,6}\s")
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        if HEADER_RE.match(line):
            continue
        backward = tuple(lines[max(0, idx - WINDOW) : idx])
        forward = tuple(lines[idx + 1 : min(len(lines), idx + 1 + FORWARD_WINDOW)])
        neighbours = backward + forward
        if _is_archaeology(line, neighbours=neighbours):
            continue
        if any(service in line.lower() for service in _SERVICES):
            offenders.append((idx + 1, line.strip()))
    return offenders


# ---------------------------------------------------------------------------
# Per-file active-prose regression guards
# ---------------------------------------------------------------------------

# (path, label, scope_filter)
#   scope_filter: optional callable that takes the list of (lineno, line)
#   offenders and returns the filtered list. Use it to suppress lines
#   that are explicitly framed as historical (e.g. the SECURITY.md
#   archaeology banner that needs to NAME the removed services).
_DOC_FILES: list[tuple[Path, str]] = [
    (API, "API.md"),
    (ARCHITECTURE, "ARCHITECTURE.md"),
    (CONTRIBUTING, "CONTRIBUTING.md"),
    (DOCS_INDEX, "DOCS-INDEX.md"),
    (DEPLOY_ENV, "docs/DEPLOY-ENV.md"),
    (PHASE2_METRICS, "docs/PHASE2-8-METRICS.md"),
    (PHASE2_ROADMAP, "docs/PHASE2-ROADMAP.md"),
    (SECURITY, "docs/SECURITY.md"),
]


def _assert_no_active_observability_refs(path: Path, label: str) -> None:
    """Issue #419 AC #1: ``label`` must not have active-prose references
    to grafana / prometheus / chownfix outside archaeology markers.
    """
    text = _strip_markdown_noise(path.read_text(encoding="utf-8"))
    offenders = _active_offenders(text)
    assert not offenders, (
        f"Issue #419: {label} has active-prose references to the removed "
        "observability services (grafana/prometheus/chownfix). PR #408 only "
        "covered README + QUICKSTART + TROUBLESHOOTING + TESTING. Wrap each "
        "offender with ``_(removed, PR #399)_`` or move it into an "
        "archaeology / regression-guard section.\n"
        "Offenders:\n" + "\n".join(f"  L{ln}: {s}" for ln, s in offenders)
    )


def test_api_no_active_grafana_prometheus() -> None:
    """Issue #419: API.md L235/311 — agent-emit Prometheus counters +
    Grafana panels (planned) readers. Both must be reworded to point
    at alphard-web (PR #394) as the actual reader post-#399.
    """
    _assert_no_active_observability_refs(API, "API.md")


def test_architecture_no_active_grafana_prometheus() -> None:
    """Issue #419: ARCHITECTURE.md L28 (Prometheus metrics emission
    phrasing), §5 (failure-mode table Prometheus + Grafana rows),
    §7.1 (mermaid diagram PROM + GF nodes), §7.2 (port map 9090/3300
    rows), §7.3 (bind-mounts ./docker/grafana/ + ./docker/prometheus/
    rows that point at deleted directories).
    """
    _assert_no_active_observability_refs(ARCHITECTURE, "ARCHITECTURE.md")


def test_contributing_no_active_grafana_prometheus() -> None:
    """Issue #419: CONTRIBUTING.md L44 — agent SHOULD emit Prometheus
    counters. The consumer is now alphard-web (PR #394), not
    Prometheus.
    """
    _assert_no_active_observability_refs(CONTRIBUTING, "CONTRIBUTING.md")


def test_docs_index_no_active_grafana_prometheus() -> None:
    """Issue #419: DOCS-INDEX.md L33 + L112 — search-jump table rows
    naming Prometheus / Grafana. The replacement surface is
    alphard-web (PR #394) on :8081.
    """
    _assert_no_active_observability_refs(DOCS_INDEX, "DOCS-INDEX.md")


def test_deploy_env_no_active_grafana_prometheus() -> None:
    """Issue #419: docs/DEPLOY-ENV.md L147 — post-deployment
    verification step 4 names a Grafana panel that no longer exists.
    The replacement step must point at alphard-web on :8081.
    """
    _assert_no_active_observability_refs(DEPLOY_ENV, "docs/DEPLOY-ENV.md")


def test_phase2_metrics_no_active_grafana_prometheus() -> None:
    """Issue #419: docs/PHASE2-8-METRICS.md L7 + L51-63 — claims a
    Prometheus + Grafana stack runs under the ``observability``
    profile (which has zero services post-#399). The doc must
    reframe: alphard-web (PR #394) reads the same metrics directly.
    """
    _assert_no_active_observability_refs(PHASE2_METRICS, "docs/PHASE2-8-METRICS.md")


def test_phase2_roadmap_no_active_grafana_prometheus() -> None:
    """Issue #419: docs/PHASE2-ROADMAP.md L11 + L217 — Phase 2.7
    roadmap row claims deployment of alphard-prometheus +
    alphard-grafana. Both services were removed in PR #399.
    """
    _assert_no_active_observability_refs(PHASE2_ROADMAP, "docs/PHASE2-ROADMAP.md")


def test_security_no_active_grafana_prometheus_outside_archaeology() -> None:
    """Issue #419: docs/SECURITY.md §4.1 (L171-222) is a Level 4.1
    threat model for a Prometheus + Grafana stack that no longer
    exists. The section must be retained as a historical
    threat-model entry with an archaeology banner pointing at
    PR #399, but the active-prose rows must be wrapped in
    archaeology markers so a fresh-clone operator isn't misled.
    """
    _assert_no_active_observability_refs(SECURITY, "docs/SECURITY.md")


# ---------------------------------------------------------------------------
# Cross-file sanity: the operator UI replacement is documented everywhere
# ---------------------------------------------------------------------------


def test_alphard_web_documented_in_extended_key_docs() -> None:
    """Issue #419 AC #2: every doc that used to point operators at
    Grafana must now point them at ``alphard-web`` (PR #394) or
    ``port 8081``. Without this, a fresh-clone operator searching
    for "metrics" / "monitoring" / "dashboard" lands on docs that
    name the old surface and not the replacement.
    """
    files = [
        (API, "API.md"),
        (ARCHITECTURE, "ARCHITECTURE.md"),
        (CONTRIBUTING, "CONTRIBUTING.md"),
        (DOCS_INDEX, "DOCS-INDEX.md"),
        (DEPLOY_ENV, "docs/DEPLOY-ENV.md"),
        (PHASE2_METRICS, "docs/PHASE2-8-METRICS.md"),
        (PHASE2_ROADMAP, "docs/PHASE2-ROADMAP.md"),
        (SECURITY, "docs/SECURITY.md"),
    ]
    missing = []
    for path, label in files:
        text = path.read_text(encoding="utf-8")
        if "alphard-web" not in text and "8081" not in text:
            missing.append(label)
    assert not missing, (
        "Issue #419 AC #2: every doc that referenced Grafana/Prometheus "
        "must now reference alphard-web (PR #394) or port 8081 so "
        "operators searching for 'metrics' / 'monitoring' land on the "
        f"right tool. Missing: {missing}"
    )


# ---------------------------------------------------------------------------
# ARCHITECTURE.md §7.3 bind-mounts — directory-existence regression guard
# ---------------------------------------------------------------------------


def test_architecture_bind_mounts_do_not_reference_deleted_dirs() -> None:
    """Issue #419 AC #1 (specific): ARCHITECTURE.md §7.3 bind-mounts
    table MUST NOT list ``./docker/grafana/`` or ``./docker/prometheus/``
    in active rows — those directories were deleted in PR #399 and
    the rows now point at non-existent paths. Lines that name the
    deleted paths in an archaeology context (e.g. a regression-guard
    footnote) are allowed.
    """
    text = ARCHITECTURE.read_text(encoding="utf-8")
    deleted_paths = ("./docker/grafana/", "./docker/prometheus/")
    lines = text.splitlines()
    HEADER_RE = re.compile(r"^#{2,6}\s")
    WINDOW = 8
    FORWARD_WINDOW = 4
    hits: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if HEADER_RE.match(line):
            continue
        if not any(p in line for p in deleted_paths):
            continue
        backward = tuple(lines[max(0, idx - WINDOW) : idx])
        forward = tuple(lines[idx + 1 : min(len(lines), idx + 1 + FORWARD_WINDOW)])
        blob = "\n".join((line, *backward, *forward)).lower()
        if "pr #399" in blob and ("removed" in blob or "deleted" in blob or "снес" in blob):
            continue  # archaeology context — allowed
        hits.append((idx + 1, line.strip()))
    assert not hits, (
        "Issue #419: ARCHITECTURE.md §7.3 lists bind-mounts from "
        "directories deleted in PR #399 (./docker/grafana/, "
        "./docker/prometheus/) in active rows. Drop the rows.\n"
        "Offenders:\n" + "\n".join(f"  L{ln}: {s}" for ln, s in hits)
    )


# ---------------------------------------------------------------------------
# SECURITY.md archaeology banner — required for the threat-model to be honest
# ---------------------------------------------------------------------------


def test_security_level_4_1_has_archaeology_banner() -> None:
    """Issue #419 AC #1: SECURITY.md §4.1 (L171) MUST carry an
    archaeology banner stating the Prometheus + Grafana stack was
    removed in PR #399. Without the banner, the section reads as
    a current threat model for a non-existent surface.
    """
    text = SECURITY.read_text(encoding="utf-8").lower()
    # Locate Level 4.1 section header.
    m = re.search(r"^####\s*level 4\.1", text, re.MULTILINE)
    assert m is not None, "SECURITY.md must have a '#### Level 4.1' section"
    # Take the next 30 lines of the section.
    start = m.end()
    rest = text[start:]
    next_header = re.search(r"^####\s", rest, re.MULTILINE)
    section = rest[: next_header.start() if next_header else len(rest)]
    # Archaeology banner: must mention PR #399 AND "removed" (or
    # "deleted" / "снес") to count as a real archaeology marker.
    assert "pr #399" in section and ("removed" in section or "deleted" in section or "снес" in section), (
        "Issue #419: SECURITY.md §4.1 (Monitoring profile Prometheus + "
        "Grafana) must carry an archaeology banner stating the stack was "
        "removed in PR #399. Pre-#419 the section reads as a current "
        "threat model for a non-existent surface.\n"
        f"Section:\n{section[:1500]}"
    )
