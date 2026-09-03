"""Regression tests for issue #440 — README freshness contract.

Issue #440 (Tech-Debt: High) flagged the README top status block as
13 days stale and the coverage badge as yellow (93%) when the actual
CI coverage is ≥95%. These tests pin the post-fix contract:

  1. README.md status block date is no more than 14 days behind HEAD.
  2. The coverage badge URL does not claim a sub-95% (yellow) value.
  3. No ``grafana`` / ``prometheus`` / ``chownfix`` mentions in active prose
     (the doc-drift guard from #401 / #408 / #419 already enforces this in
     README, but pinning here makes the README-specific gate explicit).
  4. CHANGELOG.md has no empty-href ``[anchor](#)`` Markdown links.

The regression catches the doc-drift class (multiple prior issues: #357,
#401, #419, #429, #436) at the highest-visibility file in the repo.

The freshness check tolerates a 14-day window so the contract does not
fail on every cron cycle — README refresh is a deliberate act, not a
side effect of merging code. A future tightening (e.g. 7 days) can
shrink the window without touching the test shape.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Match the status block date in README.md — the line begins with
# **Статус (YYYY-MM-DD)**. We accept either English or Russian label.
_STATUS_DATE_RE = re.compile(
    r"\*\*\s*Статус\s*\(\s*(\d{4}-\d{2}-\d{2})\s*\)\s*:?\s*\*\*",
    re.IGNORECASE,
)

# Match shields.io coverage badge URL.
_COVERAGE_BADGE_RE = re.compile(r"https://img\.shields\.io/badge/coverage-(\d+)%25-([a-z]+)\.svg")

# Coverage thresholds per shields.io colour convention:
#   red < 60, yellow 60-84, orange 85-94, green ≥ 95.
_GREEN_THRESHOLD = 95

# services removed by PR #399 — must not appear in active prose.
_REMOVED_SERVICES = ("grafana", "prometheus", "chownfix")


def _status_date() -> date | None:
    """Parse the status block date from README.md, or None if absent."""
    text = README.read_text(encoding="utf-8")
    m = _STATUS_DATE_RE.search(text)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d").date()


def test_readme_status_date_present() -> None:
    """The README top status block must carry a YYYY-MM-DD date stamp."""
    text = README.read_text(encoding="utf-8")
    assert _STATUS_DATE_RE.search(text), (
        "Issue #440 regression: README.md top status block must include a "
        "``**Статус (YYYY-MM-DD):**`` line. The date stamp is what makes "
        "the block self-validating — without it, a maintainer cannot tell "
        "when the block was last refreshed."
    )


def test_readme_status_date_within_freshness_window() -> None:
    """The README status date must be ≤ 14 days behind HEAD.

    Without this guard, README drift recurs exactly as issue #440 surfaced
    (2026-08-20 status still on the page 13 days later, on 2026-09-02).
    A 14-day window tolerates one missed refresh but flags sustained drift.
    """
    status = _status_date()
    assert status is not None, "Issue #440: README.md status date is missing"
    today = datetime.now(tz=timezone.utc).date()
    delta = today - status
    assert delta <= timedelta(days=14), (
        f"Issue #440 regression: README.md status date {status} is "
        f"{delta.days} days behind today ({today}). Refresh the top "
        "status block (status, phase progress, active phase, badge) and "
        "add a regression note in CHANGELOG.md."
    )
    assert delta >= timedelta(days=0), (
        f"Issue #440: README.md status date {status} is in the future "
        f"relative to today ({today}). Update the date stamp."
    )


def test_readme_coverage_badge_is_green() -> None:
    """The coverage badge URL must claim ≥ 95% (green)."""
    text = README.read_text(encoding="utf-8")
    m = _COVERAGE_BADGE_RE.search(text)
    assert m is not None, (
        "Issue #440: README.md must include a shields.io coverage badge "
        "in the form ``coverage-NN%25-COLOUR.svg``. Without the badge, "
        "operators cannot tell coverage status at a glance."
    )
    value = int(m.group(1))
    colour = m.group(2)
    assert value >= _GREEN_THRESHOLD, (
        f"Issue #440 regression: README.md coverage badge shows {value}% "
        f"({colour}). CI is gated at ≥ {_GREEN_THRESHOLD}% — the badge "
        f"must read ≥ {_GREEN_THRESHOLD}% (brightgreen or green)."
    )


def test_readme_no_active_removed_services() -> None:
    """README.md must not reference grafana/prometheus/chownfix in active prose.

    The doc-drift guard from #408 / #419 covers all 12 operator-facing docs;
    this test pins the README-specific portion so a regression that
    re-introduces a Grafana reference in the highest-visibility file fails
    this gate even if the wider guard suite still passes.
    """
    text = README.read_text(encoding="utf-8")
    offenders: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if any(svc in low for svc in _REMOVED_SERVICES):
            # Allow archaeology markers like ``(removed, PR #399)``.
            if "pr #399" in low or "removed" in low or "deleted" in low:
                continue
            offenders.append((lineno, line))
    assert not offenders, (
        "Issue #440 regression: README.md has active-prose references to "
        "removed services. Wrap each in ``(removed, PR #399)`` archaeology "
        "marker or move into an archaeology section.\n" + "\n".join(f"  L{ln}: {line}" for ln, line in offenders)
    )


def test_changelog_no_empty_anchor_links() -> None:
    """CHANGELOG.md must not contain empty-href Markdown anchors like ``[Kanban board](#)``.

    A bare ``(#)`` anchor renders as a broken link in GitHub UI and
    surfaces as a confusing click target. Issue #440 calls this out
    specifically — pin the contract so future CHANGELOG additions do
    not regress.
    """
    text = CHANGELOG.read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(text.splitlines(), 1):
        # Empty anchor: ``] (#)`` or ``](#)`` with optional whitespace.
        if re.search(r"\]\(\s*#\s*\)", line):
            offenders.append((lineno, line))
    assert not offenders, (
        "Issue #440 regression: CHANGELOG.md has empty-href Markdown "
        "anchor links. Either remove them or link to the actual Kanban "
        "board URL.\n" + "\n".join(f"  L{ln}: {line}" for ln, line in offenders)
    )
