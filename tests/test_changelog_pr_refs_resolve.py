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

# `--no-ff` merges (alphard convention for long-lived feature branches) carry
# the number in the subject instead: "Merge pull request #394 from ...".
NO_FF_MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+)\b")

# "PR #399", "PRs #385, #388" and "(PR #399, Closes #395.)" all cite PRs.
PR_CITATION = re.compile(r"\bPRs?\s+((?:#\d+(?:\s*,\s*)?)+)")
PR_NUMBER = re.compile(r"#(\d+)")

# "Closes #411, PR #414" — the issue the entry closes, and the PR that did it.
CLOSES_PR_PAIR = re.compile(r"Closes\s+#(\d+),\s*PR\s+#(\d+)")

# Open at the time their entry lands, so git cannot confirm them yet. An entry
# here is an author assertion, so two guards keep it honest:
# `test_inflight_allowlist_is_pruned` forces removal once a merge commit proves
# the number, and `test_inflight_prs_are_not_closed_issues` rejects a number at
# or below the newest merged PR — such a number is spent (an issue, or a long
# ago merge) and cannot be in flight. That second guard is what #422 needed:
# #394 was allowlisted forever because `_merged_pr_numbers()` only read squash
# suffixes and could not see its `--no-ff` merge subject; #419 was allowlisted
# because nothing proved an in-flight number was a PR rather than an issue.
INFLIGHT_PRS = frozenset()

# Entries written before this guard existed that say "PR #NNN" for a number
# that is an issue (#290, #349) or a closed-unmerged PR (#378, superseded by
# #380). Left as-is because rewriting shipped release prose loses the audit
# trail. This set must never grow — a new number here means a new #400.
LEGACY_MISLABELS = frozenset({"243", "246", "290", "294", "323", "332", "348", "349", "378"})

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
    """PR numbers with a merge commit reachable from HEAD.

    Covers both merge styles the repo uses: GitHub squash-merge (number in a
    trailing `(#NNN)` suffix) and `--no-ff` merges of long-lived feature
    branches (number in a `Merge pull request #NNN` subject). Missing the
    second style is what forced #394 into `INFLIGHT_PRS` as a permanent
    unprunable entry.
    """
    out = _git("log", f"-{MERGE_SCAN_DEPTH}", "--format=%s")
    if not out:
        pytest.skip("no commits in this checkout (shallow clone?)")

    found = set()
    for subject in out.splitlines():
        m = MERGE_PR_SUFFIX.search(subject) or NO_FF_MERGE_SUBJECT.match(subject)
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


def test_no_entry_cites_its_own_issue_as_its_pr() -> None:
    """`Closes #N, PR #N` is always wrong — a PR cannot close itself.

    #400 and #422 are the same defect: the author wrote the CHANGELOG entry
    before GitHub assigned the PR number, guessed, and guessed a number that
    was already taken by the issue the entry closes. `INFLIGHT_PRS` then hides
    it from `test_unreleased_pr_citations_resolve`, because that allowlist is
    author-asserted and nothing proves an in-flight number is a PR at all.

    This check needs no network and no merge commit: the collision is visible
    in the prose itself. Every legitimate historical pair has distinct
    numbers (`Closes #395, PR #399`; `Closes #411, PR #414`).
    """
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()

    collisions = []
    for offset, line in enumerate(lines):
        for closed, cited in CLOSES_PR_PAIR.findall(line):
            if closed != cited:
                continue

            collisions.append(
                f"CHANGELOG.md:{offset + 1} says 'Closes #{closed}, PR #{cited}' — a PR cannot close itself"
            )

    assert not collisions, "self-closing PR citations in CHANGELOG.md:\n" + "\n".join(collisions)


def test_inflight_prs_are_not_closed_issues() -> None:
    """An in-flight number must exceed every merged PR number.

    PR and issue numbers share one counter. A number at or below the newest
    merged PR is already spent — it is an issue, or a PR that merged long ago
    and should have been pruned. Either way it is not a PR still in flight,
    so admitting it turns `INFLIGHT_PRS` into a permanent bypass (#422).
    """
    merged = _merged_pr_numbers()
    if not merged:
        pytest.skip("no squash-merge commits reachable from HEAD")

    newest_merged = max(int(pr) for pr in merged)

    spent = sorted((pr for pr in INFLIGHT_PRS if int(pr) < newest_merged), key=int)

    assert not spent, (
        f"these INFLIGHT_PRS numbers are below the newest merged PR (#{newest_merged}), "
        f"so they cannot be in flight — they are issues or stale entries: "
        f"{', '.join('#' + pr for pr in spent)}"
    )


def test_no_changelog_commit_self_asserts_inflight() -> None:
    """No CHANGELOG-touching merge in the scan window may keep itself in INFLIGHT_PRS.

    The recursion that #433 / #438 / #447 / #449 / #453 each had to ship a
    one-line workaround for: an author adds their own PR number to
    `INFLIGHT_PRS` to make the CHANGELOG entry resolve on branch tip, then
    forgets to drop the entry on squash-merge. The drift-window guard from
    PR #451 covers the symmetric half (latest CHANGELOG commit's PR exempt
    from the window), but INFLIGHT_PRS pruning has no equivalent — every
    author-asserted self-insert has to be hand-pruned by the next person who
    notices main CI red.

    This test scans every CHANGELOG-touching commit in the scan window, not
    just the latest. A single-commit squash-merge is the common case (the
    latest-only variant handled this), but a multi-commit CHANGELOG fix that
    self-inserts via an earlier commit and then has a follow-up CHANGELOG
    edit land on main afterwards would escape a latest-only scan: the
    follow-up becomes the latest CHANGELOG commit and lacks the merge
    suffix, so the test would skip; meanwhile the earlier self-insert still
    asserts in INFLIGHT_PRS. Issue #462.

    Branch-tip CI is still safe: branch commits lack the `(#NNN)` suffix
    (GitHub appends it on squash-merge), so they match nothing and the test
    passes vacuously until the squash lands.
    """
    history = _git(
        "log",
        f"-{MERGE_SCAN_DEPTH}",
        "--format=%H %s",
        "--",
        "CHANGELOG.md",
    )
    if not history:
        pytest.skip("no commit touches CHANGELOG.md in the scan window")

    offenders: list[str] = []
    for line in history.splitlines():
        sha, _, subject = line.partition(" ")
        m = MERGE_PR_SUFFIX.search(subject) or NO_FF_MERGE_SUBJECT.match(subject)
        if not m:
            continue
        pr = m.group(1)
        if pr in INFLIGHT_PRS:
            offenders.append(f"{sha[:7]} (PR #{pr}, subject: {subject[:80]!r})")

    assert not offenders, (
        "these CHANGELOG-touching merges still assert their own PR in INFLIGHT_PRS — "
        f"prune them in {Path(__file__).name}:64 (issues #433, #447, #449, #453, #462):\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "history_lines, inflight, expected_offender_substrings",
    [
        # Multi-commit gap scenario from issue #462: an earlier commit's
        # `(#NNN)` self-insert survives while the latest CHANGELOG commit
        # is a follow-up with no suffix. The latest-only check would skip;
        # the full-window check must fire on the earlier commit.
        (
            [
                "aaa1111 docs: typo fix in CHANGELOG",
                "bbb2222 docs(changelog): backfill stuff (Closes #500) (#500)",
            ],
            frozenset({"500"}),
            ["bbb2222 (PR #500"],
        ),
        # Latest-only case: the squash-merge is the latest and self-asserts.
        # Must still be caught by the full-window scan.
        (
            ["ccc3333 fix(changelog): backfill cycle200 (#501)"],
            frozenset({"501"}),
            ["ccc3333 (PR #501"],
        ),
        # Both a backfill and a follow-up squash-merge self-assert. The
        # follow-up's PR is the latest (exempted in the old code) but the
        # backfill's PR must also be caught.
        (
            [
                "ddd4444 docs(changelog): backfill #496 #497 (#500)",
                "eee5555 fix(other): also touches CHANGELOG (#501)",
            ],
            frozenset({"500", "501"}),
            ["ddd4444 (PR #500", "eee5555 (PR #501"],
        ),
        # No offenders: neither commit's PR is in INFLIGHT_PRS. Passes.
        (
            [
                "fff6666 docs: typo fix in CHANGELOG",
                "ggg7777 docs(changelog): backfill cycle200 (#600)",
            ],
            frozenset({"601"}),  # unrelated, not in window
            [],
        ),
        # --no-ff merge subject must also be caught by the full scan.
        (
            ["hhh8888 Merge pull request #700 from feature/x"],
            frozenset({"700"}),
            ["hhh8888 (PR #700"],
        ),
    ],
)
def test_no_changelog_commit_self_asserts_inflight_full_window(
    monkeypatch: pytest.MonkeyPatch,
    history_lines: list[str],
    inflight: frozenset[str],
    expected_offender_substrings: list[str],
) -> None:
    """Regression test for issue #462 — covers the multi-commit gap.

    Each parametrized case mocks the `_git` helper to return a controlled
    CHANGELOG commit history. The cases enumerate:

    * Earlier-commit self-insert (multi-commit gap from #462's proof).
    * Latest-commit self-insert (single-commit squash, the case the old
      test handled).
    * Two self-inserts across a backfill and a follow-up.
    * Negative case: no self-inserts present.
    * `--no-ff` merge subject (the `_drift_window_prs` guard skips this
      style, but the INFLIGHT_PRS recursion guard catches it).

    Asserts the new full-window scan fires on every offender in
    `expected_offender_substrings` and reports none when the list is empty.
    """
    fake_history = "\n".join(history_lines)
    monkeypatch.setattr(
        "tests.test_changelog_pr_refs_resolve._git",
        lambda *args, **_kwargs: fake_history,
    )
    monkeypatch.setattr(
        "tests.test_changelog_pr_refs_resolve.INFLIGHT_PRS",
        inflight,
    )

    import tests.test_changelog_pr_refs_resolve as _module

    try:
        _module.test_no_changelog_commit_self_asserts_inflight()
    except AssertionError as exc:
        message = str(exc)
        for needle in expected_offender_substrings:
            assert needle in message, f"expected offender substring {needle!r} in failure message:\n{message}"
        if not expected_offender_substrings:
            raise AssertionError(f"expected test to PASS with no offenders, got:\n{message}") from exc
    else:
        assert not expected_offender_substrings, (
            "expected test to FAIL with offenders " f"{expected_offender_substrings!r}, but it passed"
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
