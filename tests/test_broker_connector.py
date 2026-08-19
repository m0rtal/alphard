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
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.broker.account import BrokerAccount, PortfolioSnapshot, Position
from src.broker.integration import OrderFlow


def _make_mock_tinkoff_client(
    cash: Decimal = Decimal("1000000"),
    positions: list | None = None,
    last_prices: dict[str, Any] | None = None,
    account_id: str = "SB1",
) -> MagicMock:
    """Build a MagicMock that imitates the t_tech.invest.Client surface.

    Issue #11: place_order now uses the same client for both the
    live-quote fetch and the post_order call, so we mock both paths
    with realistic return values. ``last_prices`` maps ticker → Decimal
    price. If a ticker is missing from the map, the quote fetch
    raises BrokerError("no quote") — that's the intended fail-safe
    behaviour (test must provide a price for every MarketOrder ticker).
    """
    positions = positions or []
    if last_prices is None:
        last_prices = {"SBER": Decimal("300"), "GAZP": Decimal("150")}

    pos_mocks = []
    for p in positions:
        avg_price = p.get("avg_price", Decimal("0"))
        if isinstance(avg_price, Decimal):
            avg_price_q = MagicMock()
            avg_price_q.units = int(avg_price)
            avg_price_q.nano = int((avg_price - int(avg_price)) * Decimal("1000000000"))
        else:
            avg_price_q = avg_price
        pos_mock = MagicMock()
        pos_mock.ticker = p["ticker"]
        pos_mock.quantity = p.get("quantity", Decimal("1"))
        pos_mock.average_position_price = avg_price_q
        pos_mock.average_buy_price = avg_price_q
        pos_mocks.append(pos_mock)

    portfolio_mock = MagicMock()
    portfolio_mock.positions = pos_mocks
    total_q = MagicMock()
    total_q.units = int(cash)
    total_q.nano = int((cash - int(cash)) * Decimal("1000000000"))
    portfolio_mock.total_amount_currencies = total_q

    def _find_instrument(query: str):
        resp = MagicMock()
        sber_inst = MagicMock()
        sber_inst.ticker = query.upper()
        sber_inst.class_code = "TQBR"
        sber_inst.figi = f"FIGI_{query.upper()}"
        resp.instruments = [sber_inst]
        return resp

    def _get_last_prices(figi: list[str]):
        resp = MagicMock()
        resp.last_prices = []
        for f in figi:
            ticker = f.replace("FIGI_", "")
            if ticker not in last_prices:
                # Real fail-safe: raise to simulate "no quote" so the
                # test sees the rejection. Callers must populate the map.
                raise BrokerError(f"no quote for {ticker} (figi={f})")
            price = last_prices[ticker]
            lp = MagicMock()
            lp.figi = f
            q_price = MagicMock()
            q_price.units = int(price)
            q_price.nano = int((price - int(price)) * Decimal("1000000000"))
            lp.price = q_price
            resp.last_prices.append(lp)
        return resp

    client = MagicMock()
    client.users.get_accounts.return_value.accounts = [
        MagicMock(id=account_id),
    ]
    client.operations.get_portfolio.return_value = portfolio_mock
    client.instruments.find_instrument.side_effect = _find_instrument
    client.market_data.get_last_prices.side_effect = _get_last_prices

    post_order_response = MagicMock()
    post_order_response.execution_report_status = MagicMock()
    post_order_response.execution_report_status.name = "EXECUTION_REPORT_STATUS_FILL"
    client.orders.post_order.return_value = post_order_response

    return client


def _install_mock_sdk(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    """Install ``t_tech.invest`` as a fake module so ``from t_tech.invest
    import Client`` resolves to a context manager returning ``client``.

    The fake module must:
      * Have a ``Client(token)`` callable that returns a context manager
        whose ``__enter__`` yields ``client``.
      * Have ``Quotation`` and ``OrderDirection`` / ``OrderType`` names
        so the imports in place_order resolve.
    """
    import sys
    from contextlib import contextmanager

    @contextmanager
    def _client_cm(token: str):
        yield client

    fake = MagicMock()
    fake.Client = MagicMock(side_effect=lambda token: _client_cm(token))
    fake.OrderDirection = MagicMock()
    fake.OrderDirection.ORDER_DIRECTION_BUY = "BUY"
    fake.OrderDirection.ORDER_DIRECTION_SELL = "SELL"
    fake.OrderType = MagicMock()
    fake.OrderType.ORDER_TYPE_MARKET = "MARKET"
    fake.OrderType.ORDER_TYPE_LIMIT = "LIMIT"
    fake.Quotation = MagicMock(side_effect=lambda units=0, nano=0: MagicMock(units=units, nano=nano))
    monkeypatch.setitem(sys.modules, "t_tech.invest", fake)


from src.broker.orders import (
    LimitOrder,
    MarketOrder,
    OrderSide,
    OrderStatus,
)
from src.broker.slicer import OrderSlicer
from src.broker.tinkoff_account import BrokerError, TinkoffAccount  # noqa: E402


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

    def test_risk_gate_violation_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Issue #11: RiskGate denial blocks the order. The order is
        REJECTED; the broker is NOT consulted (no post_order call)."""
        client = _make_mock_tinkoff_client(
            cash=Decimal("1000000"),
            last_prices={"SBER": Decimal("300")},
        )
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=False, violations=("DD_EXCEEDED",))
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.REJECTED
        mock_rg.evaluate.assert_called_once()
        client.orders.post_order.assert_not_called()
        mock_rg.evaluate.assert_called_once()

    def test_risk_gate_approved_submits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue #11: MarketOrder path fetches a live quote via the
        mocked SDK; the order then submits with the real price."""
        client = _make_mock_tinkoff_client(
            cash=Decimal("1000000"),
            last_prices={"SBER": Decimal("300")},
        )
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        # Live quote was used: the intent receives price=300, not 1.
        intent_seen = mock_rg.evaluate.call_args[0][0]
        assert intent_seen.price == Decimal("300")
        assert intent_seen.notional == Decimal("3000")  # 10 × 300

    def test_limit_order_submits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LimitOrder path uses order.price directly; no live quote
        fetch needed. Issue #11: this path was the only one that
        worked correctly before the fix."""
        client = _make_mock_tinkoff_client(cash=Decimal("1000000"))
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = LimitOrder(
            ticker="SBER",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            price=Decimal("250"),
        )
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        intent_seen = mock_rg.evaluate.call_args[0][0]
        assert intent_seen.price == Decimal("250")  # LimitOrder price, not qty=1

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

    def test_get_portfolio_mock_when_no_sdk(self, monkeypatch: pytest.MonkeyPatch):
        # Mock t_tech.invest (SDK is now installed, but we want offline unit tests)
        import sys

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
        # Use monkeypatch.setitem so the real module is restored after the test
        # and does not leak into later tests (e.g. test_tinkoff_grpc.py).
        monkeypatch.setitem(sys.modules, "t_tech.invest", _fake)

        a = TinkoffAccount(token="t.x")
        snap = a.get_portfolio()
        assert snap.account_id == "SB1"
        assert snap.cash == Decimal("100000")

    def test_get_positions_calls_portfolio(self, monkeypatch: pytest.MonkeyPatch):
        # Mock t_tech.invest (SDK is now installed, but we want offline unit tests)
        import sys

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
        # Use monkeypatch.setitem so the real module is restored after the test
        # and does not leak into later tests (e.g. test_tinkoff_grpc.py).
        monkeypatch.setitem(sys.modules, "t_tech.invest", _fake)

        a = TinkoffAccount(token="t.x")
        positions = a.get_positions()
        assert positions == []

    def test_cancel_order_returns_cancelled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):  # Mock t_tech.invest (SDK is now installed, but we want offline unit tests)
        import sys

        _fake = MagicMock()
        _fake.Client = MagicMock()
        # Use monkeypatch.setitem so the real module is restored after the test
        # and does not leak into later tests (e.g. test_tinkoff_grpc.py).
        monkeypatch.setitem(sys.modules, "t_tech.invest", _fake)

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

    def test_place_market_order_with_mocked_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Issue #11: MarketOrder path fetches a live quote via the
        SDK and passes the resulting price to RiskGate. The order then
        submits through the same client."""
        client = _make_mock_tinkoff_client(
            cash=Decimal("1000000"),
            last_prices={"SBER": Decimal("300")},
            account_id="ACC1",
        )
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg, account_id="ACC1")
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        client.orders.post_order.assert_called_once()
        # Live quote was used: price=300, not 1.
        intent_seen = mock_rg.evaluate.call_args[0][0]
        assert intent_seen.price == Decimal("300")

    def test_place_limit_order_with_mocked_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LimitOrder uses order.price directly. No live quote needed."""
        client = _make_mock_tinkoff_client(cash=Decimal("1000000"))
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = LimitOrder(
            ticker="SBER",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            price=Decimal("250.5"),
        )
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        intent_seen = mock_rg.evaluate.call_args[0][0]
        assert intent_seen.price == Decimal("250.5")

    def test_place_order_sdk_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If post_order raises after RiskGate approved, the broker
        error is wrapped in BrokerError and re-raised."""
        client = _make_mock_tinkoff_client(
            cash=Decimal("1000000"),
            last_prices={"SBER": Decimal("300")},
        )
        client.orders.post_order.side_effect = RuntimeError("api error")
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        with pytest.raises(BrokerError, match="order submit failed"):
            a.place_order(order)

    def test_place_order_tinkoff_figi_lookup_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When instruments.find_instrument returns a matching FIGI in
        TQBR / TQOB, that FIGI is used by both the live-quote fetch
        and the post_order call."""
        client = _make_mock_tinkoff_client(
            cash=Decimal("1000000"),
            last_prices={"SBER": Decimal("300")},
        )
        # Override the default find_instrument to return a TQBR match
        # with a specific FIGI.

        def _find_instrument(query: str):
            resp = MagicMock()
            inst_other = MagicMock()
            inst_other.ticker = query.upper()
            inst_other.class_code = "OTHER"
            inst_other.figi = "OTHER_FIGI"
            inst_match = MagicMock()
            inst_match.ticker = query.upper()
            inst_match.class_code = "TQBR"
            inst_match.figi = "BBG004730N88"
            resp.instruments = [inst_other, inst_match]
            return resp

        client.instruments.find_instrument.side_effect = _find_instrument

        # Override _get_last_prices to use the matched FIGI
        def _get_last_prices(figi: list[str]):
            resp = MagicMock()
            resp.last_prices = []
            for f in figi:
                lp = MagicMock()
                lp.figi = f
                q_price = MagicMock()
                q_price.units = 300
                q_price.nano = 0
                lp.price = q_price
                resp.last_prices.append(lp)
            return resp

        client.market_data.get_last_prices.side_effect = _get_last_prices

        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        a.place_order(order)
        call_kwargs = client.orders.post_order.call_args.kwargs
        assert call_kwargs["figi"] == "BBG004730N88"

    def test_place_order_sell_uses_sell_direction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SELL orders use ORDER_DIRECTION_SELL and pass through RiskGate."""
        client = _make_mock_tinkoff_client(
            cash=Decimal("1000000"),
            last_prices={"SBER": Decimal("300")},
        )
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.SELL, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        intent_seen = mock_rg.evaluate.call_args[0][0]
        assert intent_seen.side == "sell"
        call_kwargs = client.orders.post_order.call_args.kwargs
        assert "SELL" in str(call_kwargs["direction"])

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
# Issue #11: MarketOrder without live quote → REJECTED (fail-safe)
# ────────────────────────────────────────────


class TestIssue11MarketOrderNoQuote:
    def test_market_order_refused_when_quote_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Issue #11: if the SDK returns no quote for the ticker (or
        raises), the order MUST be REJECTED, not silently priced at
        Decimal('1'). This is the fail-safe behaviour that closes the
        historical bypass."""
        # Empty last_prices map → no quote for any ticker.
        client = _make_mock_tinkoff_client(
            cash=Decimal("1000000"),
            last_prices={},
        )
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.REJECTED
        # Broker must NOT have been called.
        client.orders.post_order.assert_not_called()
        # And RiskGate must NOT even have been consulted — we fail-fast
        # on the broker-side precondition.
        mock_rg.evaluate.assert_not_called()

    def test_market_order_with_real_quote_submits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Counter-test: when a real quote is available, the order
        proceeds and the price seen by RiskGate is the live quote, not
        the historical placeholder."""
        client = _make_mock_tinkoff_client(
            cash=Decimal("1000000"),
            last_prices={"SBER": Decimal("300.5")},
        )
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())
        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        order = MarketOrder(ticker="SBER", side=OrderSide.BUY, quantity=Decimal("10"))
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        # The intent that RiskGate evaluated had price=300.5, not 1.
        intent_seen = mock_rg.evaluate.call_args[0][0]
        assert intent_seen.price == Decimal("300.5")
        # And the PortfolioState used cash from the live snapshot, not
        # the historical 100 000₽ placeholder.
        state_seen = mock_rg.evaluate.call_args[0][1]
        assert state_seen.total_equity == Decimal("1000000")


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
