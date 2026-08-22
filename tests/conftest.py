"""Shared pytest fixtures and path setup for the alphard test suite."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

# Project root so ``import scripts.backfill_history_md`` etc work.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


import pytest


@pytest.fixture(autouse=True)
def _reset_moex_cache_per_test() -> Iterator[None]:
    """Issue #140: clear ``scripts.apply_corporate_actions._MOEX_CACHE``
    before every test in the suite.

    The cache is module-level state. Without this fixture, a test that
    hits ``_fetch_splits_for_ticker`` / ``_fetch_dividends_for_ticker``
    directly (bypassing ``main()``) would inherit whatever payload the
    previous test cached, and its ``fetch_splits.call_count``
    assertion would be silently wrong. Production code is unaffected:
    ``main()`` resets the cache at entry on every invocation.
    """
    import importlib

    aca = importlib.import_module("apply_corporate_actions")
    aca._reset_moex_cache_for_tests()
    yield
    aca._reset_moex_cache_for_tests()
