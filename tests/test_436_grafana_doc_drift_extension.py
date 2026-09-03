"""Regression tests for issue #436 — extended doc-drift cleanup past PR #423.

Issue #436 (Tech-Debt: High) is the follow-up audit to PR #423
(Closes #419). PR #423 cleaned 8 specific docs but the audit found
89 more active-prose references to the removed Grafana/Prometheus/
chownfix services in 14 docs — 4 of them NOT in the PR #423 scope
(README.md, docs/RUNBOOK.md, docs/TROUBLESHOOTING.md, docs/TESTING.md)
plus 4 already-covered files with NEW offenders that the PR #423
test_419 guards missed (SECURITY.md, PHASE2-ROADMAP.md, DEPLOY-ENV.md,
PHASE2-8-METRICS.md).

This test pins the post-#436 contract: every active-prose Grafana /
Prometheus / chownfix reference in the 14 operator-facing docs must
either be inside a code-fence, framed with an archaeology marker
referencing PR #399 or PR #426 (Redis), or absent entirely.

Same shape as ``tests/test_419_doc_drift_extension.py`` and
``tests/test_401_grafana_prometheus_doc_cleanup.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Scope per issue #436. The first 8 files were already covered by
# test_419_doc_drift_extension.py; the remaining 6 are new (or have
# new offenders per the issue body).
DOC_FILES: tuple[str, ...] = (
    "README.md",
    "API.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "DOCS-INDEX.md",
    "docs/DEPLOY-ENV.md",
    "docs/PHASE2-8-METRICS.md",
    "docs/PHASE2-ROADMAP.md",
    "docs/RUNBOOK.md",
    "docs/SECURITY.md",
    "docs/TESTING.md",
    "docs/TROUBLESHOOTING.md",
    "evidence/README.md",
    # docs/decisions/0006-position-sizing.md is a historical ADR — exempt.
)

# Pattern that triggers a fail. We match both forms of Grafana / Prometheus
# (capitalised and lowercase) and the chownfix sidecar.
OBSERVABILITY_RE = re.compile(r"\b(grafana|prometheus|chownfix)\b", re.IGNORECASE)

# Phrases that flag a line as explicit archaeology / historical.
# Mirrors the marker set in test_419_doc_drift_extension.py with one
# addition for the Redis cleanup (issue #426) — operators may search
# for old Grafana and need to find a positive archaeology statement.
ARCHAEOLOGY_MARKERS: tuple[str, ...] = (
    "removed in PR #399",
    "after PR #399",
    "PR #399",
    "(removed, PR #399)",
    "regression guard",
    "Regression guard",
    "regression-guard",
    "Regression-guard",
    "ARCHAEOLOGY",
    "archaeology",
    "Archaeology",
    "removed, PR #399",
    "Removed, PR #399",
)

# Single-line archaeology hints — used when an archaeology note spans
# multiple lines and the marker is on a different line than the
# observability reference (common when prose is wrapped in blockquote
# arrows `>` that break at line boundaries).
LINE_HINTS: tuple[str, ...] = (
    "archaeology",
    "Archaeology",
    "ARCHAEOLOGY",
    "regression-guard",
    "Regression-guard",
    "regression guard",
    "Regression guard",
    "removed PR #399",
    "Removed PR #399",
    "removed in PR #399",
    "Removed in PR #399",
)


def _strip_code_fences(text: str) -> str:
    """Remove ``` fenced blocks (mermaid, env-var tables, command samples)."""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _is_archaeology(line: str) -> bool:
    return any(marker in line for marker in ARCHAEOLOGY_MARKERS)


def _has_archaeology_hint(line: str) -> bool:
    """A line that mentions observability may still be archaeology if it
    also references the historical/removed state — used as a secondary
    filter so a multi-line archaeology note doesn't get flagged just
    because the marker sits on a neighbouring line.
    """
    return any(hint in line for hint in LINE_HINTS)


@pytest.mark.parametrize("doc_path", DOC_FILES)
def test_no_active_observability_refs(doc_path: str) -> None:
    """Active-prose observability refs must be archaeology-tagged or absent."""
    full = REPO_ROOT / doc_path
    if not full.exists():
        pytest.skip(f"{doc_path} does not exist")
    text = _strip_code_fences(full.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if OBSERVABILITY_RE.search(line) and not (_is_archaeology(line) or _has_archaeology_hint(line)):
            offenders.append((n, line.strip()))
    assert not offenders, (
        f"Issue #436: {doc_path} has active-prose references to the removed "
        "observability services (grafana/prometheus/chownfix). Wrap each "
        "offender with an archaeology marker referencing PR #399 or move it "
        "into an archaeology / regression-guard section.\n"
        "Offenders:\n" + "\n".join(f"  L{n}: {line}" for n, line in offenders)
    )


def test_compose_no_grafana_prometheus_chownfix() -> None:
    """Sanity: docker-compose.yaml must not declare the removed services."""
    compose = (REPO_ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    for forbidden in ("alphard-grafana", "alphard-prometheus", "alphard-chownfix"):
        assert forbidden not in compose, f"docker-compose.yaml must not declare {forbidden} (removed in PR #399)."


def test_no_obsolete_dirs_committed() -> None:
    """Sanity: docker/grafana/ and docker/prometheus/ must be gone from tree."""
    for d in ("docker/grafana", "docker/prometheus"):
        assert not (REPO_ROOT / d).exists(), f"{d}/ directory must not be in the tree (removed in PR #399)."
