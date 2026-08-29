"""Regression tests for the CI lint scope (issue #341).

Background
----------
``.github/workflows/ci.yml`` originally restricted both lint steps to
``src/`` and ``tests/``::

    black --check src/ tests/
    flake8 src/ tests/ --max-line-length=120 --extend-ignore=E203,W503

``scripts/`` was therefore never linted, even though the supervisor in
``src/main.py`` spawns six production entrypoints from that directory
(``backfill_history_md.py``, ``daily_sync.py``, ``daily_incremental.py``,
``backfill_delisted_via_tinkoff.py``, ``apply_corporate_actions.py``,
``run_macro_sync.py``).

The gap shipped real defects: ``scripts/apply_corporate_actions.py``
carried two dead imports (``typing.Any``, ``src.data.models.OHLCVRow``)
that a CI flake8 run over ``scripts/`` flags as F401, and issue #333
(``daily_incremental.py`` unformatted in PR #332 while all 7 checks were
green) had the same root cause — QA ran ``black`` over the whole tree,
CI did not. The contradiction was scope, not flake.

These tests pin two properties:

1. Both CI lint invocations name ``scripts/`` so the directory can never
   silently drop out of the checked set again.
2. ``scripts/`` is actually clean under the project's own flake8 config,
   so widening the CI scope does not immediately red the pipeline.

Strategy mirrors ``tests/test_pre_pr_smoke.py``: textual assertions on
the workflow body plus one real linter invocation, skipped when the
linter is unavailable, so the test never hard-depends on the tool it
guards.

``mypy --strict`` over ``scripts/`` is deliberately NOT pinned here.
``mypy scripts/`` currently aborts with "Source file found twice under
different module names" because ``pyproject.toml`` puts ``scripts`` on
``pythonpath``, so every module resolves under both ``foo`` and
``scripts.foo``. Resolving that needs ``--explicit-package-bases``
plumbing and belongs in its own issue.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Entrypoints the supervisor spawns as subprocesses; each one is
# production code and must be inside the linted set.
SUPERVISED_ENTRYPOINTS = (
    "backfill_history_md.py",
    "daily_sync.py",
    "daily_incremental.py",
    "backfill_delisted_via_tinkoff.py",
    "apply_corporate_actions.py",
    "run_macro_sync.py",
)


def _workflow_text() -> str:
    """Read the CI workflow via a ``__file__``-relative path so the test
    works on any checkout layout (CI runner, fresh clone, container)."""
    assert WORKFLOW_PATH.is_file(), f"missing workflow: {WORKFLOW_PATH}"

    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _lint_command_lines(tool: str) -> list[str]:
    """Return every workflow line that invokes ``tool`` as a linter.

    Matches the bare command name at a word boundary so ``pip install
    flake8 mypy`` (a dependency install, not a lint invocation) does not
    count.
    """
    pattern = re.compile(rf"(?<![\w-]){re.escape(tool)}\s+[^\n]*")
    lines = []

    for raw in _workflow_text().splitlines():
        stripped = raw.strip()

        if stripped.startswith("#"):
            continue

        if "pip install" in stripped:
            continue

        if pattern.search(stripped):
            lines.append(stripped)

    return lines


@pytest.mark.parametrize("tool", ["black", "flake8"])
def test_ci_lints_scripts_dir(tool: str) -> None:
    """Both CI lint steps must include ``scripts/`` in their path set."""
    invocations = _lint_command_lines(tool)

    assert invocations, f"no {tool} lint invocation found in {WORKFLOW_PATH.name}"

    unscoped = [line for line in invocations if "scripts/" not in line]

    assert not unscoped, f"{tool} omits scripts/, leaving entrypoints unchecked (issue #341): {unscoped}"


def test_supervised_entrypoints_exist() -> None:
    """Pin the entrypoint inventory the lint scope has to cover.

    If a supervised script is renamed or removed, this test fails and
    forces a conscious update rather than silently shrinking the set
    the scope argument rests on.
    """
    missing = [name for name in SUPERVISED_ENTRYPOINTS if not (SCRIPTS_DIR / name).is_file()]

    assert not missing, f"supervised entrypoints missing from scripts/: {missing}"


def test_scripts_dir_is_flake8_clean() -> None:
    """``scripts/`` must pass flake8 under the project config.

    Guards the dead imports removed for issue #341 from creeping back,
    and keeps the widened CI scope green.
    """
    flake8 = shutil.which("flake8")

    if flake8 is None:
        pytest.skip("flake8 not installed")

    result = subprocess.run(
        [flake8, "scripts/", "--max-line-length=120", "--extend-ignore=E203,W503"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"flake8 findings in scripts/:\n{result.stdout}{result.stderr}"
