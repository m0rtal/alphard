"""Shared pytest fixtures and path setup for the alphard test suite."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

# Project root so ``import scripts.backfill_history_md`` etc work.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


@pytest.fixture(autouse=True)
def _isolate_alphard_peak_store_dir(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Issue #220: redirect ``ALPHARD_PEAK_STORE_DIR`` to a per-session
    tmpdir for every test in the suite.

    ``src.broker.tinkoff_account.TinkoffAccount.__init__`` reads
    ``$ALPHARD_PEAK_STORE_DIR`` (defaulting to ``/var/lib/alphard``)
    to decide where to persist ``peak_equity_<acc>.json`` and
    ``daily_pnl_basis_<acc>.json``. Without isolation, any test that
    instantiates ``TinkoffAccount`` writes its basis into the real
    production directory; on the next test run (any calendar day
    after the persisted one) the issue #207 fail-closed gate refuses
    to silently disarm ``RISK_DAILY_LOSS`` and ``place_order`` raises
    ``BrokerError("Untrusted daily-P&L basis ... calendar mismatch")``
    *before* the broker SDK is consulted — leaving 11 tests in
    ``tests/test_broker_connector.py`` in ERROR with
    ``AttributeError: 'NoneType' object has no attribute 'kwargs'``
    because ``client.orders.post_order.call_args`` is ``None``.

    The isolation pattern (``patch.dict(os.environ, {"ALPHARD_PEAK_STORE_DIR":
    tmpdir})``) is already established in ``test_peak_equity_tracker.py``
    and ``test_daily_pnl_tracker.py``. Lifting it into a session-scoped
    autouse fixture in ``conftest.py`` guarantees every future test that
    touches ``TinkoffAccount`` is automatically covered.

    Scope: ``function`` (default) so each test gets a fresh tmpdir —
    no cross-test pollution even if a test writes corrupt JSON on
    purpose. We use ``tmp_path_factory`` instead of ``tmp_path`` to
    avoid the per-test teardown delete racing the ``monkeypatch``
    teardown that restores the original env.
    """
    peak_dir = tmp_path_factory.mktemp("alphard_peak_store")
    monkeypatch.setenv("ALPHARD_PEAK_STORE_DIR", str(peak_dir))
    yield
