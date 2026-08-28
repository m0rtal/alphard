"""Regression guard for issue #310.

PR #308 originally listed `src/audit/postgres_audit.py` and
`src/data/moex_iss.py` in `CHANGELOG.md` even though neither file
exists on `main`. This test extracts every backtick-quoted repo path
from `CHANGELOG.md` and asserts each one resolves on disk so a future
PR cannot reintroduce the same class of mistake.

Only paths inside the repo (under `src/`, `scripts/`, `docs/`, `tests/`,
`tools/`) are checked — bare filenames in narrative paragraphs (e.g.
`` `pyproject.toml` ``, `` `CHANGELOG.md` ``) are intentionally not
asserted so prose stays readable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Backtick-quoted path under one of these roots: src/, scripts/, docs/,
# tests/, tools/. Captures the path inside the backticks.
PATH_RE = re.compile(r"`((?:src|scripts|docs|tests|tools)/[A-Za-z0-9_./-]+)`")


@pytest.fixture(scope="module")
def claimed_paths() -> list[str]:
    assert CHANGELOG.exists(), "CHANGELOG.md must exist for this guard"
    text = CHANGELOG.read_text(encoding="utf-8")
    # Sort + dedupe so the failure message is deterministic.
    return sorted(set(PATH_RE.findall(text)))


def test_changelog_only_references_real_paths(
    claimed_paths: list[str],
) -> None:
    """Every backtick-quoted repo path in CHANGELOG.md must resolve."""
    missing = [p for p in claimed_paths if not (REPO_ROOT / p).exists()]
    assert not missing, f"CHANGELOG.md refs missing paths (#310): {missing}"


def test_changelog_paths_are_unique(claimed_paths: list[str]) -> None:
    """Sanity: the extraction regex should not produce duplicate paths."""
    assert len(claimed_paths) == len(set(claimed_paths)), f"dup paths in CHANGELOG.md: {claimed_paths}"


def test_changelog_is_nonempty() -> None:
    """Sanity: the file must have some content."""
    assert CHANGELOG.stat().st_size > 0, "CHANGELOG.md is empty"
