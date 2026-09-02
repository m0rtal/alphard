"""Regression test for issue #407 — post-merge INFLIGHT_PRS pruning.

PR #399 introduced `tests/test_changelog_pr_refs_resolve.py` with an
`INFLIGHT_PRS` allowlist containing its own number (`{"394", "399"}`). When
PR #399 squash-merged into `main` (cfa7b9b), the allowlist was not pruned of
self, and `test_inflight_allowlist_is_pruned` failed on the post-merge push
event. The PR-level CI was green because a branch commit carries no
`(#NNN)` merge suffix yet — only the merge commit on `main` surfaces the
stale entry.

This test pins the post-merge contract: any merged PR number that appears
in `INFLIGHT_PRS` makes the suite red. It exists separately from
`test_changelog_pr_refs_resolve.py::test_inflight_allowlist_is_pruned` so
the contract is visible at first glance to the next contributor who adds a
PR to the allowlist, and so a future refactor that moves the allowlist does
not silently bypass this assertion.

The test re-derives the merged set from git history (same source
`_merged_pr_numbers()` uses) and asserts the intersection is empty. The
assertion message names the file to edit, mirroring the upstream test's
diagnostic.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INFLIGHT_PRS_FILE = REPO_ROOT / "tests" / "test_changelog_pr_refs_resolve.py"

# Squash-merge subjects end with the PR number GitHub appends: "... (#399)".
_MERGE_PR_SUFFIX = re.compile(r"\(#(\d+)\)$")
_MERGE_SCAN_DEPTH = "400"


def _git(*args: str) -> str:
    """Run git in the repo root; skip if not a checkout."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"git unavailable: {exc}")

    if r.returncode != 0:
        pytest.skip(f"git {' '.join(args)} failed: {r.stderr.strip()[:200]}")

    return r.stdout.strip()


def _merged_pr_numbers() -> frozenset[str]:
    """PR numbers with a squash-merge commit reachable from HEAD."""
    out = _git("log", f"-{_MERGE_SCAN_DEPTH}", "--format=%s")
    if not out:
        pytest.skip("no commits in this checkout (shallow clone?)")

    found: set[str] = set()
    for subject in out.splitlines():
        m = _MERGE_PR_SUFFIX.search(subject)
        if m:
            found.add(m.group(1))
    return frozenset(found)


def test_postmerge_inflight_allowlist_is_pruned() -> None:
    """After merging a PR whose number was in INFLIGHT_PRS, that number must
    be removed from the allowlist — otherwise it becomes a permanent bypass.

    Issue #407 root cause: PR #399 added itself to INFLIGHT_PRS and the
    squash-merge commit landed on main with the number still in the set,
    causing CI to fail on the post-merge push.
    """
    # Parse the INFLIGHT_PRS frozenset literal from the source file so we
    # always reflect the live config, not a copy.
    src = INFLIGHT_PRS_FILE.read_text(encoding="utf-8")
    m = re.search(r"INFLIGHT_PRS\s*=\s*frozenset\(\{([^}]*)\}\)", src)
    assert m is not None, f"Could not parse INFLIGHT_PRS frozenset literal from {INFLIGHT_PRS_FILE}"
    inflight = frozenset(re.findall(r'"(\d+)"', m.group(1)))

    merged = _merged_pr_numbers()
    if not merged:
        pytest.skip("no squash-merge commits reachable from HEAD")

    stale = sorted(inflight & merged, key=int)
    assert not stale, (
        "these PRs are merged but still in INFLIGHT_PRS — remove them from "
        f"{INFLIGHT_PRS_FILE.name}: {', '.join('#' + pr for pr in stale)}. "
        "Issue #407: PR #399 left itself in the allowlist and CI went red "
        "on the post-merge push to main."
    )
