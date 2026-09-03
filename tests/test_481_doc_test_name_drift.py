"""Issue #481 regression guard.

The CHANGELOG.md / docker-compose.yaml / scripts/ docs cite specific
`test_*` names to anchor behavioural claims. If a future refactor renames
a test without updating the prose, the cited name becomes a phantom
reference and a maintainer following the breadcrumb finds nothing.

This test parses every `def test_*` definition in `tests/` and asserts
that every test-name citation in the four documented artifact dirs
resolves to a real definition. Failure mode: any cited name that does
not match a current `def test_` exits 1 with a list of phantom names.

Scope: test names cited as ``tests/test_*.py::test_*`` and the loose
``test_*`` form preceded by ``test_`` in narrative prose. We deliberately
scan only the four artifact dirs (CHANGELOG.md, docker-compose.yaml,
scripts/, docs/) — code under `src/` is allowed to use test names as
identifiers without anchoring them.

Refs: #481. Closes the recurring drift class that #433 / #438 / #447 /
#449 / #453 / #459 / #462 / #473 each shipped a one-line workaround for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

TESTS_DIR = REPO_ROOT / "tests"

ARTIFACT_FILES: tuple[Path, ...] = (
    REPO_ROOT / "CHANGELOG.md",
    REPO_ROOT / "docker-compose.yaml",
    REPO_ROOT / "scripts",
    REPO_ROOT / "docs",
)

# ``tests/test_foo.py::test_bar`` (the canonical form) and the bare
# ``test_bar`` form. The bare form is restricted to follow ``test_`` with
# an underscore or alphanumeric continuation so we don't match unrelated
# prose like "in the test_phase" or "test_run_id".
QUALIFIED_TEST_REF = re.compile(r"tests/test_[\w/]+\.py::(test_\w+)")
BARE_TEST_REF = re.compile(r"\b(test_[a-z_]\w*)\b")


def _collect_real_test_names() -> set[str]:
    """Every ``test_*`` identifier the test suite exposes.

    Three flavours are collected:
    1. Top-level functions (``def test_foo``) and methods on classes
       prefixed with ``Test`` (``class TestBar: def test_foo``). Both
       are stored as bare names so qualified citations match.
    2. Test file basenames (``test_foo.py`` → ``test_foo``) so a doc
       citation like ``test_init_postgres_sh`` — referring to the whole
       file — resolves.
    3. Class-method forms are stored as ``ClassName.test_foo`` for
       future qualified support.
    """
    out: set[str] = set()
    for path in TESTS_DIR.glob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        out.add(path.stem)  # "test_init_postgres_sh" from test_init_postgres_sh.py
        for match in re.finditer(r"^(\s*)(?:async\s+)?def\s+(test_\w+)\s*\(", text, re.MULTILINE):
            indent, name = match.group(1), match.group(2)
            out.add(name)
            if indent:
                cls_match = re.search(r"^class\s+(\w+)\s*[:(]", text[: match.start()], re.MULTILINE)
                if cls_match:
                    out.add(f"{cls_match.group(1)}.{name}")
    return out


def _iter_artifact_files() -> list[Path]:
    files: list[Path] = []
    for entry in ARTIFACT_FILES:
        if entry.is_file():
            files.append(entry)
            continue
        # Directory: recurse, but skip compiled bytecode and __pycache__.
        for path in entry.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            if "__pycache__" in path.parts:
                continue
            if path.suffix in {".md", ".rst", ".txt"}:
                files.append(path)
                continue
            if path.suffix in {".yaml", ".yml", ".sh", ".py"}:
                files.append(path)
                continue
    return files


def _scan_artifact_for_phantom_names(artifact: Path, real_names: set[str]) -> list[str]:
    text = artifact.read_text(encoding="utf-8")
    phantoms: list[str] = []
    seen: set[str] = set()

    for match in QUALIFIED_TEST_REF.finditer(text):
        name = match.group(1)
        if name in real_names or name in seen:
            continue
        seen.add(name)
        phantoms.append(f"{artifact.relative_to(REPO_ROOT)}: cites `{name}` (qualified)")

    # Bare form is ambiguous: lots of prose contains ``test_`` substrings
    # that are not test references. We only flag the bare form when the
    # token is preceded by backtick OR the word "test" / "Test" in a
    # citation context. The conservative form below catches:
    #   `` `test_foo` ``
    #   "...verified by the new `test_foo` test"
    # but not "pytest test_changelog_drift_window" (no backticks).
    bare_in_backticks = re.compile(r"`(test_[a-z_]\w*)`")
    for match in bare_in_backticks.finditer(text):
        name = match.group(1)
        if name in real_names or name in seen:
            continue
        # Suppress historical mentions: when a backticked name sits inside
        # a parenthetical that also names the current test, this is a
        # "renamed from X" breadcrumb, not a stale forward citation.
        # Look for "(originally named `X`", "(renamed from `X`", or
        # "`X` (renamed to ...)" within ±200 chars.
        start = max(0, match.start() - 200)
        end = min(len(text), match.end() + 200)
        context = text[start:end]
        if re.search(
            r"\(\s*(?:originally\s+named|renamed\s+from|formerly|was\s+named)\s+`" + re.escape(name) + r"`",
            context,
        ):
            continue
        if re.search(
            r"`" + re.escape(name) + r"`\s*\(\s*renamed\s+to\s+`[^`]+`",
            context,
        ):
            continue
        seen.add(name)
        phantoms.append(f"{artifact.relative_to(REPO_ROOT)}: cites `{name}` (backticked)")

    return phantoms


def test_no_doc_cites_phantom_test_name() -> None:
    """Every test_* reference in narrative artifacts resolves to a real def."""
    real_names = _collect_real_test_names()
    assert real_names, "no test_* definitions found — has the test layout changed?"

    phantoms: list[str] = []
    for artifact in _iter_artifact_files():
        phantoms.extend(_scan_artifact_for_phantom_names(artifact, real_names))

    assert not phantoms, (
        "phantom test-name citations in narrative artifacts — a refactor "
        "renamed these tests but the docs still point at the old name. "
        "Either restore the test under the cited name, or update the "
        "citation. Offending references:\n  " + "\n  ".join(phantoms)
    )


@pytest.mark.parametrize(
    "artifact_path",
    [str(p.relative_to(REPO_ROOT)) for p in ARTIFACT_FILES],
)
def test_artifact_paths_resolve(artifact_path: str) -> None:
    """The four rooted artifact paths all exist (sanity)."""
    assert (REPO_ROOT / artifact_path).exists(), (
        f"ARTIFACT_FILES entry {artifact_path} does not exist — "
        "update tests/test_481_doc_test_name_drift.py:ARTIFACT_FILES"
    )
