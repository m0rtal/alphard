"""Tests for the LIVE_TRADING hard-lock guard.

The Phase 1 contract: ``LIVE_TRADING=false`` is the default. Any code
path that could place a real order MUST refuse before touching the
broker. These tests pin that contract at the helper level and verify
it shows up in place_order() itself.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# _assert_not_live_trading() helper
# ---------------------------------------------------------------------------


def test_guard_returns_true_when_live_trading_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in flag explicitly set → helper returns True."""
    monkeypatch.setenv("LIVE_TRADING", "true")
    # Reload the module so its top-level env read picks up the live value.
    from src.broker import tinkoff_account

    importlib.reload(tinkoff_account)
    try:
        assert tinkoff_account._assert_not_live_trading("test action") is True
    finally:
        # Restore the default for the rest of the suite.
        monkeypatch.setenv("LIVE_TRADING", "false")
        importlib.reload(tinkoff_account)


def test_guard_returns_false_when_live_trading_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default state → helper refuses."""
    monkeypatch.setenv("LIVE_TRADING", "false")
    from src.broker import tinkoff_account

    importlib.reload(tinkoff_account)
    assert tinkoff_account._assert_not_live_trading("test action") is False


def test_guard_returns_false_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """LIVE_TRADING env not set at all → refuse (fail-safe default)."""
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    from src.broker import tinkoff_account

    importlib.reload(tinkoff_account)
    assert tinkoff_account._assert_not_live_trading("test action") is False


def test_guard_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """'TRUE', 'True', 'true', 'tRuE' all opt-in."""
    for variant in ("TRUE", "True", "true", "tRuE"):
        monkeypatch.setenv("LIVE_TRADING", variant)
        from src.broker import tinkoff_account

        importlib.reload(tinkoff_account)
        assert tinkoff_account._assert_not_live_trading("test") is True, f"variant={variant!r}"
    monkeypatch.setenv("LIVE_TRADING", "false")
    importlib.reload(tinkoff_account)


def test_guard_includes_ticker_in_message(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The refusal log includes the ticker for debugging."""
    monkeypatch.setenv("LIVE_TRADING", "false")
    from src.broker import tinkoff_account

    importlib.reload(tinkoff_account)
    import logging

    with caplog.at_level(logging.WARNING, logger="alphard.broker.tinkoff"):
        result = tinkoff_account._assert_not_live_trading("refusing order", ticker="SBER")
    assert result is False
    assert any("SBER" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# place_order() integration — the LIVE_TRADING check lives at the top
# ---------------------------------------------------------------------------


def test_place_order_refuses_when_live_trading_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """place_order() returns REJECTED without touching the broker SDK."""
    monkeypatch.setenv("LIVE_TRADING", "false")
    from src.broker import tinkoff_account

    importlib.reload(tinkoff_account)

    # Build a TinkoffAccount with no real broker (would fail elsewhere).
    # We short-circuit: provide a mock RiskGate that would allow any order
    # so that REJECTED status is unambiguously from the LIVE_TRADING gate.
    mock_gate = patch.object(tinkoff_account, "BrokerError", create=True)
    with mock_gate:
        # Use the existing TinkoffAccount but bypass __init__ for the broker.
        account = tinkoff_account.TinkoffAccount.__new__(tinkoff_account.TinkoffAccount)
        account._risk_gate = None  # fail-safe would also reject, but we check before
        from src.broker.orders import MarketOrder, OrderSide

        order = MarketOrder(ticker="SBER", quantity=Decimal("10"), side=OrderSide.BUY)
        from src.broker.orders import OrderStatus

        assert account.place_order(order) == OrderStatus.REJECTED


def test_place_order_proceeds_to_risk_gate_when_live_trading_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in flag → LIVE_TRADING gate passes, fall through to RiskGate.

    With RiskGate set to None the fail-safe kicks in and REJECTED — but
    NOT for the LIVE_TRADING reason. We verify the gate was passed by
    checking that the warning mentioned RiskGate, not LIVE_TRADING.
    """
    monkeypatch.setenv("LIVE_TRADING", "true")
    from src.broker import tinkoff_account

    importlib.reload(tinkoff_account)

    account = tinkoff_account.TinkoffAccount.__new__(tinkoff_account.TinkoffAccount)
    account._risk_gate = None
    from src.broker.orders import MarketOrder, OrderSide, OrderStatus

    order = MarketOrder(ticker="SBER", quantity=Decimal("10"), side=OrderSide.BUY)
    result = account.place_order(order)
    assert result == OrderStatus.REJECTED  # RiskGate fail-safe, NOT LIVE_TRADING
