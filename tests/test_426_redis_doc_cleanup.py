"""Regression tests for issue #429 — Redis doc-drift cleanup.

Issue #429 (Tech-Debt: High) is the doc-drift companion to PR #426
(which deleted the ``alphard-redis`` compose service but left active-prose
references to it in 6 operator-facing docs). Post-#426 a fresh-clone
operator who reads ``docs/PHASE2-ROADMAP.md`` or ``docs/SECURITY.md``
will be told about a Redis cache layer that no longer exists.

Same pattern as ``tests/test_401_grafana_prometheus_doc_cleanup.py``:
pure-fs scans, no docker daemon, strip code-fences + comments to allow
historical breadcrumbs, fail loudly on active-prose regressions.

The pin: every Redis mention in operator-facing docs must be either
inside a code-fence (mermaid, env-var table, command sample) or
explicitly framed as ``removed in PR #426`` / ``n/a after PR #426``
archaeology.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Operator-facing docs that must not describe Redis as an active path.
DOC_FILES: tuple[str, ...] = (
    "README.md",
    "API.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "DOCS-INDEX.md",
    "docs/QUICKSTART.md",
    "docs/DEPLOY-ENV.md",
    "docs/PHASE2-8-METRICS.md",
    "docs/PHASE2-ROADMAP.md",
    "docs/RUNBOOK.md",
    "docs/SECURITY.md",
    "docs/TESTING.md",
    "docs/TROUBLESHOOTING.md",
)

# Lines that explicitly contain a Redis reference in prose (case-insensitive).
# A "Redis reference" is any line containing "redis" after stripping:
#   - code fences (``` ... ```)
#   - markdown link targets / image refs
#   - env-var table cells where the key is the reference itself
REDIS_LINE_RE = re.compile(r"\b(redis|REDIS)\b", re.IGNORECASE)

# Phrases that flag a line as explicit archaeology / n/a context.
# If a Redis-mention line contains one of these, it's allowed.
ARCHAEOLOGY_MARKERS = (
    "removed in PR #426",
    "after PR #426",
    "PR #426",
    "n/a",
    "REMOVED",
    "archaeology",
    "Archaeology",
    "pre-PR #426",
)


def _strip_code_fences(text: str) -> str:
    """Remove ``` fenced blocks so historical mermaid/env-var tables don't trip."""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _is_archaeology(line: str) -> bool:
    return any(marker in line for marker in ARCHAEOLOGY_MARKERS)


@pytest.mark.parametrize("doc_path", DOC_FILES)
def test_no_active_redis_prose(doc_path: str) -> None:
    """Active-prose Redis mentions must be archaeology-tagged or absent."""
    full = REPO_ROOT / doc_path
    if not full.exists():
        pytest.skip(f"{doc_path} does not exist")
    text = _strip_code_fences(full.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if REDIS_LINE_RE.search(line) and not _is_archaeology(line):
            offenders.append((n, line.strip()))
    assert not offenders, (
        f"{doc_path}: active-prose Redis reference(s) must be framed as "
        f"'removed in PR #426' archaeology. Offending lines:\n" + "\n".join(f"  L{n}: {line}" for n, line in offenders)
    )


def test_docker_compose_has_no_alphard_redis_service() -> None:
    """The alphard-redis service must not be in docker-compose.yaml at all."""
    compose = (REPO_ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    assert "alphard-redis" not in compose, (
        "docker-compose.yaml must not define an alphard-redis service — "
        "PR #426 removed it; if you really need it back, open an issue "
        "first explaining why the in-process token bucket is insufficient."
    )


def test_no_volume_alphard_redis_data() -> None:
    """The named volume alphard-redis-data must not be in compose."""
    compose = (REPO_ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    assert "alphard-redis-data" not in compose, (
        "docker-compose.yaml must not declare an alphard-redis-data volume " "(removed by PR #426)."
    )


def test_alphabet_web_token_documented_as_alive() -> None:
    """Sanity: alphard-web is the post-#399/#426 replacement UI on 8081."""
    arch = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "alphard-web" in arch, (
        "ARCHITECTURE.md must name alphard-web as the operator UI — "
        "otherwise a fresh-clone reader will look for the removed Grafana UI."
    )
    assert ":8081" in arch, "ARCHITECTURE.md must include the :8081 port for alphard-web."
