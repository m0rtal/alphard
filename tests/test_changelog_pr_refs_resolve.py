"""Regression test for issue #400 — CHANGELOG cites a PR number that never merged.

`test_changelog_drift_window.py` (#386) guards one direction: every PR merged
into the window must be *mentioned* in `CHANGELOG.md`. It cannot detect the
inverse defect — an entry that mentions a PR number which does not exist.

That inverse defect is what #400 caught on PR #399: the entry describing the
Grafana/Prometheus removal referenced itself as "PR #396" in four places (#396
is an *issue*, not a PR). Nothing failed pre-merge, because a branch commit
carries no `(#NNN)` suffix yet, so the drift-window test had an empty window and
passed vacuously. Post-merge the suffix becomes `(#399)`, the drift-window test
looks for `#399`, finds only `#396`, and CI goes red on `main` — the exact
regression class #386 was introduced to prevent.

Contract enforced here: every `PR #NNN` citation inside `[Unreleased]` resolves
to a PR with a squash-merge commit reachable from HEAD, unless the number is
declared in one of two explicit allowlists:

  - `INFLIGHT_PRS`   — the PR whose branch is checked out, plus any PR whose
                       merge is a stated prerequisite. Not yet in git, so the
                       author asserts it; `test_inflight_allowlist_is_pruned`
                       forces removal once git can prove it.
  - `LEGACY_MISLABELS` — pre-#400 entries that wrote "PR #NNN" where #NNN is an
                       issue or an abandoned PR. Frozen, not grandfathering:
                       the set may only shrink.

The test is a no-op outside a git checkout (sdist / CI archive installs).
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

UNRELEASED_HEADING = "## [Unreleased]"

# Squash-merge subjects end with the PR number GitHub appends: "... (#399)".
MERGE_PR_SUFFIX = re.compile(r"\(#(\d+)\)$")

# "PR #399", "PRs #385, #388" and "(PR #399, Closes #395.)" all cite PRs.
PR_CITATION = re.compile(r"\bPRs?\s+((?:#\d+(?:\s*,\s*)?)+)")
PR_NUMBER = re.compile(r"#(\d+)")

# Open at the time their entry lands, so git cannot confirm them yet.
# #394 just merged via `--no-ff` (alphard convention) which does not append
# `(#394)` to the merge subject, so the suffix-driven `_merged_pr_numbers()`
# cannot prove the merge from git history alone. The Changelog allowlist
# guard workflow (.github/workflows/changelog-allowlist-guard.yml) plus
# `tests/test_407_postmerge_inflight_allowlist.py` keep this list honest:
# the next no-ff merge will be detected via the test, and a follow-up PR
# drops the entry. #399 is the prior example of an entry that needed
# pruning — see cycle165 QA review (issue #407).
INFLIGHT_PRS = frozenset({"394", "420"})

# Entries written before this guard existed that say "PR #NNN" for a number
# that is an issue (#290, #349) or a closed-unmerged PR (#378, superseded by
# #380). Left as-is because rewriting shipped release prose loses the audit
# trail. This set must never grow — a new number here means a new #400.
LEGACY_MISLABELS = frozenset({"290", "349", "378"})

# How far back to scan for merge commits. `[Unreleased]` only cites work since
# the last tag, so a bounded window keeps the test fast on deep histories while
# still covering every PR the section may legitimately name.
MERGE_SCAN_DEPTH = "400"


def _git(*args: str) -> str:
    """Run git in the repo root; skip the test if this is not a checkout."""
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
    out = _git("log", f"-{MERGE_SCAN_DEPTH}", "--format=%s")
    if not out:
        pytest.skip("no commits in this checkout (shallow clone?)")

    found = set()
    for subject in out.splitlines():
        m = MERGE_PR_SUFFIX.search(subject)
        if not m:
            continue

        found.add(m.group(1))

    return frozenset(found)


def _unreleased_pr_citations() -> dict[str, list[int]]:
    """PR numbers cited in [Unreleased], mapped to their 1-based line numbers."""
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()

    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith(UNRELEASED_HEADING))
    except StopIteration:
        pytest.fail(f"CHANGELOG.md has no '{UNRELEASED_HEADING}' section")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if not lines[i].startswith("## "):
            continue

        end = i
        break

    cited: dict[str, list[int]] = {}
    for offset, line in enumerate(lines[start:end]):
        for group in PR_CITATION.findall(line):
            for pr in PR_NUMBER.findall(group):
                cited.setdefault(pr, []).append(start + offset + 1)

    return cited


def test_unreleased_pr_citations_resolve() -> None:
    """Every `PR #NNN` in [Unreleased] is merged, in-flight, or a known mislabel.

    #400's root cause: the #399 entry cited "PR #396" for its own work. #396 is
    an issue, so no merge commit ever matched it, and the drift-window guard
    could not see the mismatch until after merge.
    """
    merged = _merged_pr_numbers()
    if not merged:
        pytest.skip("no squash-merge commits reachable from HEAD")

    known = merged | INFLIGHT_PRS | LEGACY_MISLABELS

    unresolved = [
        f"CHANGELOG.md:{min(lines)} cites PR #{pr} — no merge commit, and not " f"declared in INFLIGHT_PRS"
        for pr, lines in sorted(_unreleased_pr_citations().items(), key=lambda kv: int(kv[0]))
        if pr not in known
    ]

    assert not unresolved, "unresolvable PR references in [Unreleased]:\n" + "\n".join(unresolved)


def test_inflight_allowlist_is_pruned() -> None:
    """A merged PR must be dropped from INFLIGHT_PRS.

    Without this, the allowlist becomes a permanent bypass: a stale entry would
    keep admitting a number long after git could prove or disprove it.
    """
    stale = sorted(INFLIGHT_PRS & _merged_pr_numbers(), key=int)

    assert not stale, (
        "these PRs are merged — remove them from INFLIGHT_PRS in "
        f"{Path(__file__).name}: {', '.join('#' + pr for pr in stale)}"
    )


def test_legacy_mislabels_are_still_cited() -> None:
    """LEGACY_MISLABELS may only shrink.

    An entry that no longer appears in the changelog was either fixed or
    deleted; either way its exemption is dead weight that would silently
    re-admit the number if a future entry reused it.
    """
    cited = set(_unreleased_pr_citations())

    unused = sorted(LEGACY_MISLABELS - cited, key=int)

    assert not unused, (
        "these numbers are no longer cited — drop them from LEGACY_MISLABELS in "
        f"{Path(__file__).name}: {', '.join('#' + pr for pr in unused)}"
    )
