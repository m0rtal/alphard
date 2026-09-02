"""Regression test for issue #384 — CHANGELOG drift-window completeness.

`CHANGELOG.md` is the aggregated release view introduced by #289, so that
release history need not be reconstructed from `git log`. The existing
guards (`test_check_changelog_paths.py`, `test_check_md_links.py`) validate
that paths and links *inside* the file resolve — they cannot detect an entry
that was never written.

Both #357 (PR #356 fixed 2 of 8 stale refs) and #384 (PR #383 logged 4 of 6
drift-window PRs) share one root cause: the backfill enumerated PRs from a
human-authored cycle range in the issue title instead of from the
authoritative commit range. This test derives the window from git.

Contract: every squash-merge commit on `main` between the two most recent
CHANGELOG-touching commits carries a trailing `(#NNN)`, and each such PR
number must appear somewhere in `CHANGELOG.md`.

The test is a no-op outside a git checkout (sdist / CI archive installs).
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Squash-merge subjects end with the PR number GitHub appends: "... (#383)".
MERGE_PR_SUFFIX = re.compile(r"\(#(\d+)\)$")

# Commits that legitimately need no CHANGELOG entry of their own: a backfill
# commit documents OTHER PRs, so requiring it to reference itself would make
# every backfill self-blocking.
CHANGELOG_EXEMPT_PRS = frozenset(
    {
        "360",  # docs(changelog): backfill cycle146 PRs #368 #369
        "383",  # 381: docs(changelog): backfill cycle147-150
        "386",  # 384: docs(changelog): backfill #373 #367 + drift guard (this PR)
        "404",  # docs(changelog): backfill cycle159 PRs #385 #388 #391 (#404)
    }
)

# PRs whose entries this test was written to pin. Listed explicitly so the
# test still fails loudly if a future edit deletes them, even when the git
# window has moved past their merge commits.
PINNED_PRS = ("373", "367")


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


def _drift_window_prs() -> list[tuple[str, str]]:
    """PR numbers merged between the last two CHANGELOG-touching commits.

    Returns (pr_number, subject) pairs, newest first. The window opens at the
    PREVIOUS CHANGELOG commit rather than the latest one, because the latest
    is normally the backfill under review — its own additions must be inside
    the window it claims to close.

    The LATEST CHANGELOG commit is also exempted dynamically: its own
    `(#NNN)` suffix is appended only at squash-merge time, so the branch-tip
    CI cannot see it. Once on `main`, the drift-window check would reject
    the backfill for missing its own citation — even when the entry on the
    line above explicitly references it. This is the recursion that #447 /
    #448 / #450 / #449 surfaced. Fix: parse the latest CHANGELOG commit's
    squash-merge subject and add its PR number to a per-run exemption set.
    See issue #449 option 2 for the rationale.
    """
    history = _git("log", "-2", "--format=%H%x00%s", "--", "CHANGELOG.md").splitlines()
    if len(history) < 2:
        pytest.skip("fewer than 2 CHANGELOG commits in this checkout (shallow clone?)")

    latest_line = history[0]
    _latest_sha, _, latest_subject = latest_line.partition("\x00")
    latest_pr_match = MERGE_PR_SUFFIX.search(latest_subject)

    runtime_exempt: frozenset[str] = CHANGELOG_EXEMPT_PRS | (
        frozenset({latest_pr_match.group(1)}) if latest_pr_match else frozenset()
    )

    window_start = history[-1].split("\x00", 1)[0]

    out = _git("log", "--format=%H%x00%s", f"{window_start}..HEAD")
    if not out:
        return []

    found: list[tuple[str, str]] = []
    for line in out.splitlines():
        _sha, _, subject = line.partition("\x00")
        m = MERGE_PR_SUFFIX.search(subject)
        if not m:
            continue

        found.append((m.group(1), subject))

    return [(pr, subject) for pr, subject in found if pr not in runtime_exempt]


def test_drift_window_prs_are_all_in_changelog() -> None:
    """Every (#NNN) merge commit in the drift window is referenced.

    This is the gate that #384 says was missing: it derives the window from
    git, so a backfill cannot pass by logging only the PRs a human happened
    to name in an issue title.

    The recursion guard for self-citing PRs is implemented in
    `_drift_window_prs` (see issue #449).
    """
    text = CHANGELOG.read_text(encoding="utf-8")

    missing = [f"PR #{pr} — {subject}" for pr, subject in _drift_window_prs() if f"#{pr}" not in text]

    assert not missing, "merged to main but absent from CHANGELOG.md:\n" + "\n".join(missing)


def test_latest_changelog_commit_is_self_cite_exempt() -> None:
    """Regression test for issue #449 — recursion guard.

    The latest CHANGELOG-touching commit's own `(#NNN)` suffix is appended
    only at squash-merge time. Branch-tip CI cannot see it, so the test
    would falsely flag the backfill for missing its own citation once it
    lands on `main`. `_drift_window_prs` must dynamically exempt that PR.
    """
    history = _git("log", "-1", "--format=%s", "--", "CHANGELOG.md").splitlines()
    if not history:
        pytest.skip("no CHANGELOG.md commit reachable from HEAD")
    latest_subject = history[0]
    latest_pr_match = MERGE_PR_SUFFIX.search(latest_subject)
    if not latest_pr_match:
        pytest.skip("latest CHANGELOG commit is not a squash-merge (#NNN suffix)")
    latest_pr = latest_pr_match.group(1)

    window_prs = [pr for pr, _subject in _drift_window_prs()]
    assert latest_pr not in window_prs, (
        f"latest CHANGELOG commit (PR #{latest_pr}) leaked into the drift "
        f"window — self-cite recursion is back (#449 regression)"
    )


@pytest.mark.parametrize("pr", PINNED_PRS)
def test_pinned_backfill_entries_survive(pr: str) -> None:
    """#373 and #367 stay referenced even after the git window moves on."""
    text = CHANGELOG.read_text(encoding="utf-8")

    assert f"#{pr}" in text, f"PR #{pr} entry was removed from CHANGELOG.md (issue #384 regression)"


def test_pr_373_entry_names_skip_cache_semantics() -> None:
    """#384 AC: the #373 entry must name the class, the cache, and its
    process-lifetime reset — a bare PR reference is not enough, because the
    changelog exists so readers need not open the diff.
    """
    text = CHANGELOG.read_text(encoding="utf-8")

    entry = next((ln for ln in text.splitlines() if "#373" in ln), "")
    assert entry, "no CHANGELOG line references #373"

    for token in ("FallbackDataLoader", "_skip"):
        assert token in entry, f"#373 entry must name {token}; got:\n{entry}"

    assert (
        "subprocess" in entry or "process-lifetime" in entry
    ), f"#373 entry must state the cache's process-lifetime reset semantics; got:\n{entry}"


def test_pr_367_entry_is_not_filed_under_fixed() -> None:
    """#384 AC: a panel removal is Changed/Removed, not Fixed."""
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()

    section = ""
    for line in lines:
        if line.startswith("### "):
            section = line[4:].strip()
            continue

        if "#367" not in line:
            continue

        assert section in {"Changed", "Removed"}, (
            f"#367 is a dashboard panel removal and belongs under Changed/Removed, " f"not '{section}'"
        )
        assert (
            "Heartbeat rate" in line and "Heartbeat lag" in line
        ), f"#367 entry must name both removed panels; got:\n{line}"
        return

    pytest.fail("no CHANGELOG line references #367")
