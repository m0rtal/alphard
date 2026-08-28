"""CI gate for relative markdown links (issue #320, refined by #324).

Background
----------
PR #307 (closed) and #320 (open) document the same defect class: PRs
that cross-link markdown files supplied only by sibling PRs. If the
siblings land out of order, ``main`` briefly carries broken links.
Issue #307's specific instance was fixed by PR #306; #320 generalizes
it and points at #301, #302, #305, all of which have since landed.

This test prevents recurrence: every tracked ``.md`` file is scanned
for ``[text](target.md)`` and ``[text](relative/path.md)`` patterns.
Any target that doesn't resolve on disk fails the test.

This complements ``test_check_changelog_paths.py`` (issue #310) which
checks only backtick-quoted paths inside ``CHANGELOG.md``. That guard
catches file-existence claims in narrative prose; THIS guard catches
hyperlink existence across all tracked docs.

Issue #324 refinement
---------------------
The per-file walk strips inline code spans and fenced code blocks
*before* applying ``LINK_RE``. Per the CommonMark spec, content inside
backticks (`` `[text](path.md)` ``) and fenced blocks is literal and
must not be parsed as a markdown link — the original regex did not
honor that and produced false positives when docs documented link
syntax. The helper ``_strip_code_spans`` implements this and is
pinned by five dedicated unit tests at the bottom of this file.

What we deliberately do NOT check
---------------------------------
- Absolute URLs (``http://...``, ``https://...``) — network calls in CI.
- In-page anchors (``[text](#section)``) — content drift only.
- Image references (``![alt](image.png)``) — binary asset guard out of
  scope (separate concern, different test if needed later).
"""

from __future__ import annotations

import pathlib
import re
from typing import Iterator

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Markdown link ``[text](target)`` where target ends in ``.md`` and is
# NOT a URL/anchor. Matches both ``[t](x.md)`` and ``[t](./x.md)`` /
# ``[t](../other/x.md)`` style.
LINK_RE = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+?\.md)(?:#[^)]*)?\)")

# Inline code spans — `` `[text](path.md)` `` is literal code per the
# CommonMark spec and must not be parsed as a markdown link. Issue #324.
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# Fenced code blocks — triple-backtick and triple-tilde fences. Their
# contents are also literal per the CommonMark spec, so we strip those
# before applying ``LINK_RE``. Issue #324 (extension; complements the
# inline fix).
_FENCED_CODE_RE = re.compile(r"(?ms)^[ \t]*```.*?^[ \t]*```[ \t]*$|^[ \t]*~~~.*?^[ \t]*~~~[ \t]*$")


def _strip_code_spans(text: str) -> str:
    """Strip fenced code blocks and inline code spans from ``text``.

    Per the CommonMark spec, content inside backtick-delimited spans and
    fenced code blocks is literal — markdown link syntax there is NOT a
    link and must not be reported as broken. See issue #324.

    Order matters: fences are stripped first because the fence delimiter
    itself looks like an inline-code opener; if we stripped inline
    first, a fence-only doc would lose its fences but otherwise behave
    correctly. Stripping fences first keeps the implementation
    predictable.
    """
    text = _FENCED_CODE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


def _tracked_md_files() -> Iterator[pathlib.Path]:
    """Yield every tracked-or-on-disk ``*.md`` under repo root, skipping
    junk directories.

    We use ``Path.rglob`` rather than ``git ls-files`` so this test
    passes even when run from a worktree that hasn't yet staged the
    new file — failing closed is what we want for a CI gate.
    """
    skip_parts = (".git", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache", ".hypothesis", "__pycache__")
    for p in REPO_ROOT.rglob("*.md"):
        if any(part in skip_parts for part in p.parts):
            continue
        yield p


@pytest.mark.parametrize("md_file", list(_tracked_md_files()), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_relative_markdown_links_resolve(md_file: pathlib.Path) -> None:
    """Every ``[text](relative/path.md)`` reference in a tracked doc
    must point at an existing file on disk.

    Issue #320: a relative markdown link to a file supplied only by a
    sibling PR is a CI-invisible defect: the link target is absent from
    ``main`` until the sibling merges, with no window where anyone
    notices. This test enforces that every link resolves, so any
    cross-cutting PR set must merge in dependency order (or land as a
    single squashed commit).
    """
    text = _strip_code_spans(md_file.read_text(encoding="utf-8"))
    rel_md = md_file.relative_to(REPO_ROOT)
    broken: list[str] = []
    for match in LINK_RE.finditer(text):
        target = match.group(1)
        # Strip any in-page anchor ``#section`` that may follow.
        # (The regex already strips ``#...``; this is defensive in case
        # the format changes.)
        target_path = target.split("#", 1)[0]
        # Resolve relative to the file containing the link.
        candidate = (md_file.parent / target_path).resolve()
        if not candidate.exists():
            broken.append(target)
    assert not broken, (
        f"{rel_md} has {len(broken)} broken relative .md link(s):\n  "
        + "\n  ".join(sorted(broken))
        + "\n\nEither (a) add the missing file, (b) fix the link target, "
        "or (c) merge the PR that supplies the target first. See issue #320."
    )


def test_no_md_links_point_at_known_dead_targets() -> None:
    """Sanity test: the link guard's own walk finds at least some
    ``.md`` files and reports zero broken links on a clean main.

    Catches the case where the parametrization accidentally yields zero
    files (e.g. ``rglob`` exclude too aggressive) and the per-file test
    silently passes vacuously.
    """
    found = list(_tracked_md_files())
    assert len(found) >= 5, (
        f"_tracked_md_files() yielded only {len(found)} files — "
        "rglob exclude too aggressive? Sanity-check the helper."
    )
    # And on a clean tree, no file should have any broken links.
    any_broken = False
    for md in found:
        text = _strip_code_spans(md.read_text(encoding="utf-8"))
        for m in LINK_RE.finditer(text):
            target = m.group(1).split("#", 1)[0]
            candidate = (md.parent / target).resolve()
            if not candidate.exists():
                any_broken = True
                break
        if any_broken:
            break
    assert not any_broken, (
        "test_relative_markdown_links_resolve should have caught this — "
        "sanity-check the LINK_RE pattern matches what the per-file test sees."
    )


# ---------------------------------------------------------------------------
# Issue #324: backtick-wrapped inline-code link syntax must not be flagged.
# Unit tests for the helper directly so the regex behaviour is pinned.
# ---------------------------------------------------------------------------


def test_strip_code_spans_strips_backtick_wrapped_link() -> None:
    """`[text](path.md)` inside backticks is literal code, not a link."""
    text = "Use this syntax: `[text](relative/path.md)` inline."
    stripped = _strip_code_spans(text)
    assert "relative/path.md" not in stripped, f"backtick-wrapped link should be stripped; got: {stripped!r}"
    assert LINK_RE.findall(stripped) == []


def test_strip_code_spans_preserves_real_link() -> None:
    """A genuine `[text](real.md)` outside backticks still matches."""
    text = "But real [link](docs/REAL.md) should still match."
    stripped = _strip_code_spans(text)
    assert LINK_RE.findall(stripped) == ["docs/REAL.md"]


def test_strip_code_spans_handles_mixed_inline_and_real_links() -> None:
    """Both styles in one line — only the real link survives."""
    text = "`code with [text](path.md)` then real [link](other.md) outside."
    stripped = _strip_code_spans(text)
    assert LINK_RE.findall(stripped) == ["other.md"]


def test_strip_code_spans_handles_fenced_code_block() -> None:
    """Triple-backtick and triple-tilde fences are stripped too."""
    fenced_bt = "```\n[text](fake.md)\n```\nReal: [link](real.md)."
    fenced_tl = "~~~markdown\n[text](fake.md)\n~~~\nReal: [link](real.md)."
    for text in (fenced_bt, fenced_tl):
        stripped = _strip_code_spans(text)
        assert LINK_RE.findall(stripped) == ["real.md"], f"fence should be stripped; got: {stripped!r}"


def test_strip_code_spans_no_op_on_plain_prose() -> None:
    """No code spans → text is unchanged."""
    text = "Just a paragraph with a [link](docs/X.md) and nothing else."
    assert _strip_code_spans(text) == text
    assert LINK_RE.findall(_strip_code_spans(text)) == ["docs/X.md"]
