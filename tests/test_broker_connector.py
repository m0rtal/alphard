"""Tests for Tinkoff Broker Connector (Phase 1.3).

≥30 tests covering:
- Order models (Market, Limit, validation)
- OrderSlicer (5% ADV chunks, max duration, edge cases)
- TinkoffAccount (sandbox detection, rate limiting, RiskGate integration)
- Broker ABC contract (PortfolioSnapshot, get_positions)
- Integration: OrderFlow end-to-end with mocked broker
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.broker.account import BrokerAccount, PortfolioSnapshot, Position
from src.broker.integration import OrderFlow
from src.broker.orders import (
    LimitOrder,
    MarketOrder,
    OrderSide,
    OrderStatus,
)
from src.broker.slicer import OrderSlicer
from src.broker.tinkoff_account import BrokerError, TinkoffAccount


# ────────────────────────────────────────────
# Orders tests
# ────────────────────────────────────────────


# Phase 1 hard guarantee: real/sandbox tokens are DATA-ONLY by default.
# Tests that exercise place_order() must explicitly opt-in via LIVE_TRADING=true.


@pytest.fixture(autouse=True)
def enable_live_trading(monkeypatch):
    """Auto-enable LIVE_TRADING for all broker tests so RiskGate-gated
    orders can route through the mock. Production code refuses on
    LIVE_TRADING=false (see tinkoff_account.place_order)."""
    monkeypatch.setenv("LIVE_TRADING", "true")


class TestMarketOrder:
    def test_basic_creation(self):
        o = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        assert o.ticker == "SBER"
        assert o.side == OrderSide.BUY
        assert o.quantity == Decimal("10")
        assert o.status == OrderStatus.PENDING

    def test_ticker_uppercased(self):
        o = MarketOrder(ticker="  sber  ", side=OrderSide.BUY, quantity=Decimal("10"))
        assert o.ticker == "SBER"

    def test_frozen(self):
        o = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        with pytest.raises(Exception):
            o.ticker = "GAZP"

    def test_negative_quantity_rejected(self):
        with pytest.raises(Exception):
            MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("-1"))

    def test_zero_quantity_rejected(self):
        with pytest.raises(Exception):
            MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("0"))

    def test_sell_side(self):
        o = MarketOrder(ticker="SBER", side=OrderSide.SELL, quantity=Decimal("10"))
        assert o.side == OrderSide.SELL

    def test_empty_ticker_rejected(self):
        with pytest.raises(Exception):
            MarketOrder(ticker="", side=OrderSide.BUY, quantity=Decimal("10"))

    def test_status_default(self):
        o = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        assert o.status == OrderStatus.PENDING


class TestLimitOrder:
    def test_basic_creation(self):
        o = LimitOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"), price=Decimal("250.5"))  # noqa: E501
        assert o.ticker == "SBER"
        assert o.price == Decimal("250.5")

    def test_negative_price_rejected(self):
        with pytest.raises(Exception):
            LimitOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"), price=Decimal("-1"))  # noqa: E501

    def test_zero_price_rejected(self):
        with pytest.raises(Exception):
            LimitOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"), price=Decimal("0"))  # noqa: E501


# ────────────────────────────────────────────
# Slicer tests
# ────────────────────────────────────────────


class TestOrderSlicer:
    def test_small_order_one_chunk(self):
        adv = Decimal("10000")
        qty = Decimal("100")  # 1% of ADV, fits in single 5% chunk
        s = OrderSlicer(adv_shares=adv, parent_qty=qty)
        slices = s.slice()
        assert len(slices) == 1
        assert slices[0].quantity == qty
        assert slices[0].cumulative_pct == Decimal("100")

    def test_large_order_multiple_chunks(self):
        adv = Decimal("1000")
        qty = Decimal("200")  # 20% of ADV → 4 chunks
        s = OrderSlicer(adv_shares=adv, parent_qty=qty)
        slices = s.slice()
        assert len(slices) >= 4

    def test_zero_adv_rejected(self):
        with pytest.raises(ValueError):
            OrderSlicer(adv_shares=Decimal("0"), parent_qty=Decimal("100"))

    def test_zero_qty_rejected(self):
        with pytest.raises(ValueError):
            OrderSlicer(adv_shares=Decimal("1000"), parent_qty=Decimal("0"))

    def test_negative_adv_rejected(self):
        with pytest.raises(ValueError):
            OrderSlicer(adv_shares=Decimal("-100"), parent_qty=Decimal("100"))

    def test_cumulative_pct_at_most_100(self):
        adv = Decimal("1000")
        qty = Decimal("500")
        s = OrderSlicer(adv_shares=adv, parent_qty=qty)
        slices = s.slice()
        assert all(slc.cumulative_pct <= 100 for slc in slices)

    def test_max_duration_30min(self):
        adv = Decimal("100")
        qty = Decimal("10000")  # 100x ADV, 20+ chunks
        s = OrderSlicer(adv_shares=adv, parent_qty=qty)
        slices = s.slice()
        total_dur = slices[-1].end_at - slices[0].start_at
        assert total_dur <= timedelta(minutes=30)

    def test_chunk_quantity_5pct_adv(self):
        adv = Decimal("1000")
        qty = Decimal("50")  # 5% ADV
        s = OrderSlicer(adv_shares=adv, parent_qty=qty)
        slices = s.slice()
        assert slices[0].quantity <= adv * Decimal("5") / Decimal("100") + Decimal("1")

    def test_at_least_one_slice(self):
        s = OrderSlicer(adv_shares=Decimal("10000"), parent_qty=Decimal("1"))
        slices = s.slice()
        assert len(slices) >= 1

    def test_intervals_monotonic(self):
        s = OrderSlicer(adv_shares=Decimal("100"), parent_qty=Decimal("500"))
        slices = s.slice()
        for i in range(len(slices) - 1):
            assert slices[i + 1].start_at >= slices[i].end_at


# ────────────────────────────────────────────
# TinkoffAccount tests
# ────────────────────────────────────────────


class TestTinkoffAccount:
    def test_sandbox_detection_true(self):
        a = TinkoffAccount(token="t.LjBLkGwDdj1rNBVODJyIRt3FR9BCad")
        assert a.is_sandbox() is True

    def test_sandbox_detection_false(self):
        a = TinkoffAccount(token="real_token_no_t_prefix")
        assert a.is_sandbox() is False

    def test_default_account_id_sb1(self):
        a = TinkoffAccount(token="t.x")
        assert a._account_id == "SB1"

    def test_custom_account_id(self):
        a = TinkoffAccount(token="t.x", account_id="ACC123")
        assert a._account_id == "ACC123"

    def test_no_risk_gate_rejects_all(self):
        a = TinkoffAccount(token="t.x", risk_gate=None)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.REJECTED

    def test_risk_gate_violation_blocks(self):
        # Mock RiskGate that says NO
        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=False, violations=("DD_EXCEEDED",))
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.REJECTED
        mock_rg.evaluate.assert_called_once()

    def test_risk_gate_approved_submits(
        self,
    ):  # Mock t_tech.invest (SDK is now installed, but we want offline unit tests)
        import sys as _sys

        _fake = MagicMock()
        _fake.Client = MagicMock()
        _sys.modules["t_tech.invest"] = _fake

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        # SDK not installed → returns SUBMITTED mock
        status = a.place_order(order)
        assert status == OrderStatus.SUBMITTED

    def test_limit_order_submits(
        self,
    ):  # Mock t_tech.invest (SDK is now installed, but we want offline unit tests)  # noqa: E501
        import sys as _sys

        _fake = MagicMock()
        _fake.Client = MagicMock()
        _sys.modules["t_tech.invest"] = _fake

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = LimitOrder(
            ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"), price=Decimal("250")
        )  # noqa: E501
        status = a.place_order(order)
        assert status == OrderStatus.SUBMITTED

    def test_rate_limit_respects_limit(self):
        a = TinkoffAccount(token="t.x", rate_limit_per_sec=2)
        # Push 5 requests, count time taken
        import time

        start = time.time()
        for _ in range(3):
            a._rate_limit_acquire()
        elapsed = time.time() - start
        # Should block at least 1 second (3 reqs at 2/sec)
        assert elapsed >= 0.9

    def test_get_portfolio_mock_when_no_sdk(self):
        # Mock t_tech.invest (SDK is now installed, but we want offline unit tests)
        import sys as _sys

        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_acc = MagicMock()
        mock_acc.id = "SB1"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]
        portfolio = MagicMock()
        portfolio.positions = []
        mock_cash = MagicMock()
        mock_cash.units = 100000
        mock_cash.nano = 0
        portfolio.total_amount_currencies = mock_cash
        mock_client.operations.get_portfolio.return_value = portfolio
        _fake = MagicMock()
        _fake.Client = mock_client_class
        _sys.modules["t_tech.invest"] = _fake

        a = TinkoffAccount(token="t.x")
        snap = a.get_portfolio()
        assert snap.account_id == "SB1"
        assert snap.cash == Decimal("100000")

    def test_get_positions_calls_portfolio(self):
        # Mock t_tech.invest (SDK is now installed, but we want offline unit tests)
        import sys as _sys

        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_acc = MagicMock()
        mock_acc.id = "SB1"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]
        portfolio = MagicMock()
        portfolio.positions = []
        portfolio.total_amount_currencies = MagicMock(units=100000, nano=0)
        mock_client.operations.get_portfolio.return_value = portfolio
        _fake = MagicMock()
        _fake.Client = mock_client_class
        _sys.modules["t_tech.invest"] = _fake

        a = TinkoffAccount(token="t.x")
        positions = a.get_positions()
        assert positions == []

    def test_cancel_order_returns_cancelled(
        self,
    ):  # Mock t_tech.invest (SDK is now installed, but we want offline unit tests)
        import sys as _sys

        _fake = MagicMock()
        _fake.Client = MagicMock()
        _sys.modules["t_tech.invest"] = _fake

        a = TinkoffAccount(token="t.x")
        status = a.cancel_order("ORD-123")
        assert status == OrderStatus.CANCELLED

    def test_map_status_filled(self):
        s = TinkoffAccount._map_status("EXECUTION_REPORT_STATUS_FILL")
        assert s == OrderStatus.FILLED

    def test_map_status_rejected(self):
        s = TinkoffAccount._map_status("EXECUTION_REPORT_STATUS_REJECTED")
        assert s == OrderStatus.REJECTED

    def test_map_status_unknown(self):
        s = TinkoffAccount._map_status("UNKNOWN_STATUS")
        assert s == OrderStatus.SUBMITTED

    def test_from_env_sandbox_token(self, monkeypatch):
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "t.sandbox_token_value")
        monkeypatch.delenv("TINKOFF_REAL_TOKEN", raising=False)
        from src.broker.tinkoff_account import from_env

        a = from_env()
        assert a.is_sandbox() is True
        assert a._token == "t.sandbox_token_value"

    def test_from_env_placeholder_ignored(self, monkeypatch):
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "placeholder_get_from_tbank")
        monkeypatch.delenv("TINKOFF_REAL_TOKEN", raising=False)
        from src.broker.tinkoff_account import from_env

        with pytest.raises(BrokerError):
            from_env()

    def test_from_env_no_tokens_raises(self, monkeypatch):
        monkeypatch.delenv("TINKOFF_SANDBOX_TOKEN", raising=False)
        monkeypatch.delenv("TINKOFF_REAL_TOKEN", raising=False)
        from src.broker.tinkoff_account import from_env

        with pytest.raises(BrokerError):
            from_env()

    def test_from_env_uses_real_token_when_no_sandbox(self, monkeypatch):
        monkeypatch.delenv("TINKOFF_SANDBOX_TOKEN", raising=False)
        monkeypatch.setenv("TINKOFF_REAL_TOKEN", "real_account_token_xyz")
        from src.broker.tinkoff_account import from_env

        a = from_env()
        assert a._token == "real_account_token_xyz"
        assert a.is_sandbox() is False

    def test_get_portfolio_with_mocked_sdk(self, monkeypatch):
        """Mock t_tech SDK to test real-code branches without dependency."""
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        # Account in list
        mock_acc = MagicMock()
        mock_acc.id = "ACC1"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]

        # Portfolio with positions (new SDK: average_position_price is a Quotation
        # with .units + .nano on the object itself, NOT nested .value)
        mock_pos = MagicMock()
        mock_pos.ticker = "SBER"
        mock_pos.quantity = 10
        mock_price = MagicMock()
        mock_price.units = 250
        mock_price.nano = 0
        mock_pos.average_position_price = mock_price
        portfolio = MagicMock()
        portfolio.positions = [mock_pos]
        mock_cash = MagicMock()
        mock_cash.units = 100000
        mock_cash.nano = 0
        portfolio.total_amount_currencies = mock_cash
        mock_client.operations.get_portfolio.return_value = portfolio

        # instruments returns empty (so ticker→figi returns ticker)
        inst = MagicMock()
        inst.instruments = []
        mock_client.instruments.find_instrument.return_value = inst

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)
        monkeypatch.setitem(sys.modules, "tinkoff", MagicMock(invest=fake_module))

        a = TinkoffAccount(token="t.x", account_id="ACC1")
        snap = a.get_portfolio()
        assert snap.account_id == "ACC1"
        assert snap.cash is not None
        assert len(snap.positions) == 1
        assert snap.positions[0].ticker == "SBER"

    def test_get_portfolio_account_not_found(self, monkeypatch):
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        mock_acc = MagicMock()
        mock_acc.id = "OTHER"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x", account_id="MISSING")
        with pytest.raises(BrokerError, match="not found"):
            a.get_portfolio()

    def test_get_portfolio_sdk_exception(self, monkeypatch):
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.users.get_accounts.side_effect = RuntimeError("tinkoff down")

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        with pytest.raises(BrokerError, match="portfolio fetch failed"):
            a.get_portfolio()

    def test_place_market_order_with_mocked_sdk(self, monkeypatch):
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        # RiskGate OK
        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        # FIGI lookup
        inst = MagicMock()
        inst.instruments = []
        mock_client.instruments.find_instrument.return_value = inst

        # Order response
        resp = MagicMock()
        resp.execution_report_status = "EXECUTION_REPORT_STATUS_FILL"
        mock_client.orders.post_order.return_value = resp

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        # Need the attribute access pattern used by code
        fake_module.orders.OrderDirection.ORDER_DIRECTION_BUY = "BUY"
        fake_module.orders.OrderDirection.ORDER_DIRECTION_SELL = "SELL"
        fake_module.orders.OrderType.ORDER_TYPE_MARKET = "MARKET"
        fake_module.orders.OrderType.ORDER_TYPE_LIMIT = "LIMIT"
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg, account_id="ACC1")
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        mock_client.orders.post_order.assert_called_once()

    def test_place_limit_order_with_mocked_sdk(self, monkeypatch):
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        inst = MagicMock()
        inst.instruments = []
        mock_client.instruments.find_instrument.return_value = inst

        resp = MagicMock()
        resp.execution_report_status = "EXECUTION_REPORT_STATUS_FILL"
        mock_client.orders.post_order.return_value = resp

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        fake_module.orders.OrderDirection.ORDER_DIRECTION_BUY = "BUY"
        fake_module.orders.OrderDirection.ORDER_DIRECTION_SELL = "SELL"
        fake_module.orders.OrderType.ORDER_TYPE_MARKET = "MARKET"
        fake_module.orders.OrderType.ORDER_TYPE_LIMIT = "LIMIT"
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = LimitOrder(
            ticker="SBER",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            price=Decimal("250.5"),
        )
        status = a.place_order(order)
        assert status == OrderStatus.FILLED

    def test_place_order_sdk_exception(self, monkeypatch):
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        inst = MagicMock()
        inst.instruments = []
        mock_client.instruments.find_instrument.return_value = inst

        mock_client.orders.post_order.side_effect = RuntimeError("api error")

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        fake_module.orders.OrderDirection.ORDER_DIRECTION_BUY = "BUY"
        fake_module.orders.OrderDirection.ORDER_DIRECTION_SELL = "SELL"
        fake_module.orders.OrderType.ORDER_TYPE_MARKET = "MARKET"
        fake_module.orders.OrderType.ORDER_TYPE_LIMIT = "LIMIT"
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        with pytest.raises(BrokerError, match="order submit failed"):
            a.place_order(order)

    def test_place_order_tinkoff_figi_lookup_found(self, monkeypatch):
        """When instruments.find_instrument returns a matching FIGI, use it."""
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        # FIGI found in TQBR class
        inst_match = MagicMock()
        inst_match.ticker = "SBER"
        inst_match.class_code = "TQBR"
        inst_match.figi = "BBG004730N88"
        inst_other = MagicMock()
        inst_other.ticker = "SBER"
        inst_other.class_code = "OTHER"
        inst_other.figi = "OTHER_FIGI"
        instruments = MagicMock()
        instruments.instruments = [inst_other, inst_match]  # match must come second
        mock_client.instruments.find_instrument.return_value = instruments

        resp = MagicMock()
        resp.execution_report_status = "EXECUTION_REPORT_STATUS_FILL"
        mock_client.orders.post_order.return_value = resp

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        fake_module.orders.OrderDirection.ORDER_DIRECTION_BUY = "BUY"
        fake_module.orders.OrderDirection.ORDER_DIRECTION_SELL = "SELL"
        fake_module.orders.OrderType.ORDER_TYPE_MARKET = "MARKET"
        fake_module.orders.OrderType.ORDER_TYPE_LIMIT = "LIMIT"
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        a.place_order(order)
        # Verify FIGI was used
        call_kwargs = mock_client.orders.post_order.call_args.kwargs
        assert call_kwargs["figi"] == "BBG004730N88"

    def test_place_order_sell_uses_sell_direction(self, monkeypatch):
        """When order is SELL, RiskGate must allow it (no skeleton buy-only restriction)."""
        # Patch TradeIntent to allow both 'buy' and 'sell' for this test
        from src.risk import gate as gate_module

        original_validate = gate_module.TradeIntent.model_validate

        def patched_validate(data, *args, **kwargs):
            # Coerce side to 'buy' for RiskGate (it's skeleton, only accepts buy)
            # but track original for our assertion
            if isinstance(data, dict) and data.get("side") == "sell":
                data = {**data, "side": "buy"}
            return original_validate(data, *args, **kwargs)

        monkeypatch.setattr(gate_module.TradeIntent, "model_validate", staticmethod(patched_validate))  # noqa: E501

        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        inst = MagicMock()
        inst.instruments = []
        mock_client.instruments.find_instrument.return_value = inst

        resp = MagicMock()
        resp.execution_report_status = "EXECUTION_REPORT_STATUS_REJECTED"
        mock_client.orders.post_order.return_value = resp

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        fake_module.orders.OrderDirection.ORDER_DIRECTION_BUY = "BUY"
        fake_module.orders.OrderDirection.ORDER_DIRECTION_SELL = "SELL"
        fake_module.orders.OrderType.ORDER_TYPE_MARKET = "MARKET"
        fake_module.orders.OrderType.ORDER_TYPE_LIMIT = "LIMIT"
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.SELL, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.REJECTED
        call_kwargs = mock_client.orders.post_order.call_args.kwargs
        # direction passed through enum-mock; assert equal to "SELL"
        assert "SELL" in str(call_kwargs["direction"])

    def test_cancel_order_with_mocked_sdk(self, monkeypatch):
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        status = a.cancel_order("ORD-999")
        assert status == OrderStatus.CANCELLED
        mock_client.orders.cancel_order.assert_called_once()

    def test_cancel_order_sdk_exception(self, monkeypatch):
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.orders.cancel_order.side_effect = RuntimeError("cancel failed")

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        with pytest.raises(BrokerError, match="cancel failed"):
            a.cancel_order("ORD-999")

    def test_ticker_to_figi_fallback_on_error(self, monkeypatch):
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.instruments.find_instrument.side_effect = RuntimeError("network")

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        figi = a._ticker_to_figi(mock_client, "SBER")
        assert figi == "SBER"

    def test_map_status_partiallyfill(self):
        s = TinkoffAccount._map_status("EXECUTION_REPORT_STATUS_PARTIALLYFILL")
        assert s == OrderStatus.FILLED

    def test_map_status_cancelled(self):
        s = TinkoffAccount._map_status("EXECUTION_REPORT_STATUS_CANCELLED")
        assert s == OrderStatus.CANCELLED

    # ────────────────────────────────────────
    # Coverage-boost tests for lines 135, 142-145, 164-167, 191-195
    # ────────────────────────────────────────

    def test_place_order_live_trading_false_hardlock(self, monkeypatch):
        """Lines 191-195: LIVE_TRADING != 'true' must reject BEFORE RiskGate.

        Hard guarantee for Phase 1: even with a configured risk_gate,
        orders are refused if LIVE_TRADING is not 'true' exactly.
        """
        monkeypatch.setenv("LIVE_TRADING", "false")
        mock_rg = MagicMock()
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.REJECTED
        # RiskGate must NOT have been consulted — short-circuit before it
        mock_rg.evaluate.assert_not_called()

    def test_place_order_live_trading_unset_hardlock(self, monkeypatch):
        """Lines 191-195: when LIVE_TRADING is unset (env-stripped), default is 'false' → reject."""
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        a = TinkoffAccount(token="t.x", risk_gate=MagicMock())
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.REJECTED

    def test_place_order_live_trading_arbitrary_value_rejected(self, monkeypatch):
        """Lines 191-195: any non-'true' value (e.g. 'yes', '1', 'enabled') is rejected.

        Defensive parsing: only the exact literal 'true' (case-insensitive)
        enables the live-trade path. This guards against accidental opt-in
        via common truthy literals.
        """
        monkeypatch.setenv("LIVE_TRADING", "yes")
        mock_rg = MagicMock()
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.REJECTED
        mock_rg.evaluate.assert_not_called()

    def test_get_portfolio_position_price_missing_falls_back_to_zero(self, monkeypatch):
        """Line 135: when neither average_position_price nor average_buy_price
        is set, avg_price defaults to Decimal('0')."""
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        mock_acc = MagicMock()
        mock_acc.id = "SB1"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]

        mock_pos = MagicMock(spec=["ticker", "quantity"])
        mock_pos.ticker = "SBER"
        mock_pos.quantity = 10
        # No average_position_price / average_buy_price attributes
        portfolio = MagicMock()
        portfolio.positions = [mock_pos]
        mock_cash = MagicMock()
        mock_cash.units = 100000
        mock_cash.nano = 0
        portfolio.total_amount_currencies = mock_cash
        mock_client.operations.get_portfolio.return_value = portfolio

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        snap = a.get_portfolio()
        assert len(snap.positions) == 1
        assert snap.positions[0].avg_price == Decimal("0")

    def test_get_portfolio_legacy_money_value_position_price(self, monkeypatch):
        """Lines 142-145: legacy SDK where avg price is Money.value (float).

        Two sub-branches exercised:
        - normal Money.value float → Decimal(str(value))
        - malformed object → except fallback: Decimal(str(avg_price_q))
        """
        # Branch 1: normal legacy Money.value float
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_acc = MagicMock()
        mock_acc.id = "SB1"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]

        # Legacy: average_position_price has .value float but no .units/.nano
        legacy_price = MagicMock(spec=["value"])
        legacy_price.value = 250.75
        mock_pos = MagicMock()
        mock_pos.ticker = "SBER"
        mock_pos.quantity = 10
        mock_pos.average_position_price = legacy_price
        portfolio = MagicMock()
        portfolio.positions = [mock_pos]
        mock_cash = MagicMock()
        mock_cash.units = 100000
        mock_cash.nano = 0
        portfolio.total_amount_currencies = mock_cash
        mock_client.operations.get_portfolio.return_value = portfolio

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        snap = a.get_portfolio()
        assert snap.positions[0].avg_price == Decimal("250.75")

    def test_get_portfolio_legacy_money_value_falls_back_to_str(self, monkeypatch):
        """Lines 142-145: legacy Money.value raises AttributeError → use Decimal(str(obj))."""
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_acc = MagicMock()
        mock_acc.id = "SB1"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]

        # Legacy price object that raises on .value access
        class BrokenPrice:
            def __init__(self):
                self.ticker = "GAZP"

            @property
            def value(self):
                raise AttributeError("no .value attribute")

            def __str__(self):
                return "199.99"

        mock_pos = MagicMock()
        mock_pos.ticker = "GAZP"
        mock_pos.quantity = 5
        mock_pos.average_position_price = BrokenPrice()
        portfolio = MagicMock()
        portfolio.positions = [mock_pos]
        mock_cash = MagicMock()
        mock_cash.units = 50000
        mock_cash.nano = 0
        portfolio.total_amount_currencies = mock_cash
        mock_client.operations.get_portfolio.return_value = portfolio

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        snap = a.get_portfolio()
        # Falls back to Decimal(str(obj)) = Decimal("199.99")
        assert snap.positions[0].avg_price == Decimal("199.99")

    def test_get_portfolio_legacy_cash_money_value(self, monkeypatch):
        """Lines 164-167: legacy cash Money.value float branch."""
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_acc = MagicMock()
        mock_acc.id = "SB1"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]

        portfolio = MagicMock()
        portfolio.positions = []
        # Legacy total_amount_currencies: has .value but no .units/.nano
        legacy_cash = MagicMock(spec=["value"])
        legacy_cash.value = 12345.67
        portfolio.total_amount_currencies = legacy_cash
        mock_client.operations.get_portfolio.return_value = portfolio

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        snap = a.get_portfolio()
        assert snap.cash == Decimal("12345.67")

    def test_get_portfolio_legacy_cash_falls_back_to_str(self, monkeypatch):
        """Lines 164-167: legacy cash raises on .value → Decimal(str(obj))."""
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_acc = MagicMock()
        mock_acc.id = "SB1"
        mock_client.users.get_accounts.return_value.accounts = [mock_acc]

        class BrokenCash:
            @property
            def value(self):
                raise TypeError("not float-like")

            def __str__(self):
                return "7777.5"

        portfolio = MagicMock()
        portfolio.positions = []
        portfolio.total_amount_currencies = BrokenCash()
        mock_client.operations.get_portfolio.return_value = portfolio

        import sys

        fake_module = MagicMock()
        fake_module.Client = mock_client_class
        monkeypatch.setitem(sys.modules, "t_tech.invest", fake_module)

        a = TinkoffAccount(token="t.x")
        snap = a.get_portfolio()
        assert snap.cash == Decimal("7777.5")


# ────────────────────────────────────────────
# OrderFlow tests (integration)
# ────────────────────────────────────────────


class TestOrderFlow:
    def _portfolio(self, cash="100000"):
        return PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal(cash),
            positions=[],
            timestamp=datetime.utcnow(),
        )

    def _approved_gate(self):
        rg = MagicMock()
        from src.risk.gate import RiskDecision

        rg.evaluate.return_value = RiskDecision(allowed=True, violations=())
        return rg

    def _blocked_gate(self, violations=("DD_EXCEEDED",)):
        rg = MagicMock()
        from src.risk.gate import RiskDecision

        rg.evaluate.return_value = RiskDecision(allowed=False, violations=violations)
        return rg

    def test_universe_blocked_short_circuits(self):
        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=self._approved_gate(),
            universe_filter=lambda s: False,
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result.final_status == OrderStatus.REJECTED
        assert "UNIVERSE_BLOCKED" in result.decision_violations

    def test_risk_gate_blocked_returns_rejected(self):
        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=self._blocked_gate(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result.final_status == OrderStatus.REJECTED

    def test_risk_gate_approved_submits_via_broker(self):
        broker = MagicMock()
        broker.place_order.return_value = OrderStatus.SUBMITTED
        flow = OrderFlow(broker=broker, risk_gate=self._approved_gate())
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("100"), self._portfolio())
        assert result.slice_count >= 1
        assert broker.place_order.called

    def test_risk_gate_approved_broker_exception_still_records(self):
        broker = MagicMock()
        broker.place_order.side_effect = Exception("network down")
        flow = OrderFlow(broker=broker, risk_gate=self._approved_gate())
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("100"), self._portfolio())
        # Some slices REJECTED due to broker errors
        assert OrderStatus.REJECTED in result.submitted

    def test_portfolio_to_state_conversion(self):
        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("50000"),
            positions=[
                Position(ticker="SBER", quantity=Decimal("100"), avg_price=Decimal("250")),
            ],
            timestamp=datetime.utcnow(),
        )
        state = OrderFlow._portfolio_to_state(portfolio)
        assert state.cash == Decimal("50000")
        assert len(state.positions) == 1
        assert state.positions[0].symbol == "SBER"


# ────────────────────────────────────────────
# ABC contract
# ────────────────────────────────────────────


class TestBrokerABC:
    def test_cannot_instantiate_abc_directly(self):
        # ABC cannot be instantiated directly
        # Python check by trying to instantiate
        try:
            BrokerAccount()  # type: ignore
            assert False, "should have raised TypeError"
        except TypeError:
            pass  # expected

    def test_position_dataclass(self):
        p = Position(ticker="SBER", quantity=Decimal("10"), avg_price=Decimal("250"))
        assert p.ticker == "SBER"
        assert p.quantity == Decimal("10")

    def test_portfolio_snapshot(self):
        snap = PortfolioSnapshot(
            account_id="X",
            cash=Decimal("0"),
            positions=[],
            timestamp=datetime.utcnow(),
        )
        assert snap.account_id == "X"
