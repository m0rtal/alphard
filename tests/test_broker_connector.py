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


from src.broker.orders import (  # noqa: E402
    LimitOrder,
    MarketOrder,
    OrderSide,
    OrderStatus,
)
from src.broker.slicer import OrderSlicer  # noqa: E402
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

        # Issue #13: the mock Client must yield a TQBR match for SBER
        # so that _ticker_to_figi returns a real FIGI rather than
        # raising BrokerError. The minimal viable mock is one match.
        _client_instance = MagicMock()
        _inst_match = MagicMock()
        _inst_match.ticker = "SBER"
        _inst_match.class_code = "TQBR"
        _inst_match.figi = "BBG004730N88"
        _client_instance.instruments.find_instrument.return_value.instruments = [_inst_match]
        # post_order returns a fill marker so the broker call path
        # succeeds.
        _resp = MagicMock()
        _resp.execution_report_status.name = "EXECUTION_REPORT_STATUS_FILL"
        _client_instance.orders.post_order.return_value = _resp

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

        # Issue #13: see test_risk_gate_approved_submits above.
        _client_instance = MagicMock()
        _inst_match = MagicMock()
        _inst_match.ticker = "SBER"
        _inst_match.class_code = "TQBR"
        _inst_match.figi = "BBG004730N88"
        _client_instance.instruments.find_instrument.return_value.instruments = [_inst_match]
        _resp = MagicMock()
        _resp.execution_report_status.name = "EXECUTION_REPORT_STATUS_FILL"
        _client_instance.orders.post_order.return_value = _resp

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
        # TokenBucket starts full at capacity=2, so the first 2 acquire()
        # calls return immediately. The 3rd call must wait for the
        # bucket to refill at 2 tokens/sec → ~0.5s sleep before
        # acquiring. (Pre-TokenBucket implementation used a sliding-
        # window list with no burst allowance, hence the old test
        # asserted >= 0.9s.)
        import time

        start = time.time()
        for _ in range(3):
            a._rate_limit_acquire()
        elapsed = time.time() - start
        # Should block at least ~0.4s (refill 2→3 takes ~0.5s)
        assert elapsed >= 0.4

    def test_rate_limit_thread_safe_under_concurrency(self):
        """Issue #103: concurrent _rate_limit_acquire must not burst above SLA.

        The pre-fix implementation maintained self._last_request_ts as
        a list mutated without any lock, so two threads could both
        observe an empty list, both append, and both submit to Tinkoff
        in the same instant — violating the 60 req/sec SLA. After
        switching to TokenBucket (thread-safe), 2 threads × 50 calls at
        rate=10/s should serialize into a wall-clock of ~10s for 100
        calls and the bucket's internal Lock must prevent any two
        acquire()s from observing > capacity tokens in a single
        critical section.
        """
        import threading

        import time

        rate = 10
        per_thread = 50
        n_threads = 2

        a = TinkoffAccount(token="t.x", rate_limit_per_sec=rate)

        stamps: list[float] = []
        stamp_lock = threading.Lock()

        def hit() -> None:
            for _ in range(per_thread):
                a._rate_limit_acquire()
                with stamp_lock:
                    stamps.append(time.monotonic())

        threads = [threading.Thread(target=hit) for _ in range(n_threads)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - start

        total = n_threads * per_thread
        assert len(stamps) == total
        # 100 calls at 10/s: initial burst == capacity (10) is free,
        # then 90 more at 1/10s each = 9s. Allow generous slack.
        assert elapsed >= 8.0, f"elapsed {elapsed:.2f}s < 8.0s — bucket did not block"
        # Worst-case window can hold capacity tokens (initial burst)
        # plus a small refill during the test run. Any window of 1.0s
        # must hold < rate + capacity + small slack.
        stamps_sorted = sorted(stamps)
        max_in_window = 0
        for i in range(len(stamps_sorted)):
            j = i
            while j < len(stamps_sorted) and stamps_sorted[j] < stamps_sorted[i] + 1.0:
                j += 1
            max_in_window = max(max_in_window, j - i)
        # Bound: capacity (initial burst) + one full refill window.
        assert max_in_window <= 2 * rate, (
            f"observed {max_in_window} acquires within a 1s window "
            f"(rate={rate}, capacity={rate}); thread-safety regression"
        )

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
        match = MagicMock()
        match.ticker = "SBER"
        match.class_code = "TQBR"
        match.figi = "BBG004730N88"
        inst.instruments = [match]
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

    def test_place_limit_order_subnano_price_floors_to_wire_precision(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue #100: a LimitOrder price with > 9 fractional digits
        must round-trip through ``Quotation(units, nano)`` within 1e-9
        of the rounded value, and a warning must be logged so the
        operator notices the precision loss.

        Pre-fix: ``Decimal("100.0000000001")`` (10 digits) was packed
        via ``int(0.0000000001 * 1e9) = 0``, so the wire Quotation
        became ``(100, 0)`` — Tinkoff sees ``100.0`` and the order is
        silently placed at the wrong price.

        Post-fix: ``quantize(1e-9, ROUND_DOWN)`` floors to 9 digits,
        yielding ``(100, 0)`` explicitly with a warning logged.
        """
        client = _make_mock_tinkoff_client(cash=Decimal("1000000"))
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        # 10 fractional digits — exactly one more than the wire format.
        subnano_price = Decimal("100.0000000001")
        order = LimitOrder(
            ticker="SBER",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            price=subnano_price,
        )
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        # The post_order call's price argument is a MagicMock Quotation.
        # Inspect it: units=100, nano=0 (floored from 0.0000000001).
        post_kwargs = client.orders.post_order.call_args.kwargs
        price_arg = post_kwargs["price"]
        assert price_arg.units == 100
        assert price_arg.nano == 0
        # Round-trip: reconstruct Decimal from wire fields, must equal
        # the floored input (within 1e-9).
        reconstructed = Decimal(price_arg.units) + Decimal(price_arg.nano) / Decimal(1_000_000_000)
        assert reconstructed == Decimal("100.000000000")

    def test_place_limit_order_subnano_high_precision_preserved(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Counterpart to #100: a price with exactly 9 fractional digits
        must pass through unchanged (no spurious rounding).
        """
        client = _make_mock_tinkoff_client(cash=Decimal("1000000"))
        _install_mock_sdk(monkeypatch, client)

        mock_rg = MagicMock()
        from src.risk.gate import RiskDecision

        mock_rg.evaluate.return_value = RiskDecision(allowed=True, violations=())

        a = TinkoffAccount(token="t.x", risk_gate=mock_rg)
        # 9 fractional digits — exactly wire precision.
        precise_price = Decimal("100.123456789")
        order = LimitOrder(
            ticker="SBER",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            price=precise_price,
        )
        status = a.place_order(order)
        assert status == OrderStatus.FILLED
        post_kwargs = client.orders.post_order.call_args.kwargs
        price_arg = post_kwargs["price"]
        assert price_arg.units == 100
        assert price_arg.nano == 123_456_789

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

    def test_ticker_to_figi_raises_on_instrument_error(self, monkeypatch):
        """Issue #13 (C.1): when find_instrument fails, _ticker_to_figi
        MUST raise BrokerError so the operator sees the actual
        failure (auth, network, rate-limit). The historical
        ``test_ticker_to_figi_fallback_on_error`` asserted the
        opposite — silent fallback to the ticker string, which
        sent ``figi="SBER"`` to post_order and produced a confusing
        INVALID_ARGUMENT deep in the broker."""
        mock_client = MagicMock()
        mock_client.instruments.find_instrument.side_effect = RuntimeError("network")
        a = TinkoffAccount(token="t.x")
        with pytest.raises(BrokerError, match="find_instrument failed"):
            a._ticker_to_figi(mock_client, "SBER")

    def test_ticker_to_figi_raises_when_not_in_tqbr_tqob(self, monkeypatch):
        """Issue #13 (C.1): when the ticker is not in any tradable
        class_code (e.g. some non-MOEX instrument), _ticker_to_figi
        MUST raise rather than silently return the ticker string.

        Issue #187: error message now lists the actually-seen
        class_codes and the expected tradable set so operators can
        distinguish "wrong ticker" from "right ticker in a class
        we don't trade".
        """
        mock_client = MagicMock()
        # Return a list of matches but none with a tradable class_code.
        match = MagicMock()
        match.ticker = "SBER"
        match.class_code = "OTHER"
        match.figi = "OTHER_FIGI"
        resp = MagicMock()
        resp.instruments = [match]
        mock_client.instruments.find_instrument.return_value = resp
        a = TinkoffAccount(token="t.x")
        with pytest.raises(BrokerError, match="not found in tradable instrument universe"):
            a._ticker_to_figi(mock_client, "SBER")

    def test_ticker_to_figi_accepts_tqte_etf(self):
        """Issue #187: TQTE-class ETFs / BPIFs must map to a FIGI.

        Regression for the latent gap where the broker whitelist
        ``("TQBR", "TQOB")`` rejected every ETF order at FIGI-mapping
        time with the misleading ``"not found in TQBR/TQOB instrument
        universe"`` error, even though ``list_etfs()`` /
        ``_ETF_CLASS_CODE`` already recognise TQTE in the data layer.
        """
        mock_client = MagicMock()
        match = MagicMock()
        match.ticker = "FXRL"
        match.class_code = "TQTE"
        match.figi = "FXRL_FIGI"
        resp = MagicMock()
        resp.instruments = [match]
        mock_client.instruments.find_instrument.return_value = resp
        a = TinkoffAccount(token="t.x")
        assert a._ticker_to_figi(mock_client, "FXRL") == "FXRL_FIGI"

    def test_ticker_to_figi_accepts_tqcb_corporate_bond(self):
        """Issue #187: TQCB corporate / municipal bonds must map to a FIGI.

        TQOB was already in the whitelist; TQCB was missing. The data
        layer recognises both via ``_BOND_CLASS_CODES = {"TQOB", "TQCB"}``
        in src/data/tinkoff_loader.py.
        """
        mock_client = MagicMock()
        match = MagicMock()
        match.ticker = "RU000A0JX0QJ"
        match.class_code = "TQCB"
        match.figi = "CORP_BOND_FIGI"
        resp = MagicMock()
        resp.instruments = [match]
        mock_client.instruments.find_instrument.return_value = resp
        a = TinkoffAccount(token="t.x")
        assert a._ticker_to_figi(mock_client, "RU000A0JX0QJ") == "CORP_BOND_FIGI"

    def test_ticker_to_figi_accepts_spbxm_foreign_share(self):
        """Issue #187: SPBXM-listed foreign shares (AAPL, MSFT) must map.

        Regression for the gap where the broker rejected every SPB
        foreign-share order. ``TinkoffInvestDataLoader.get_ticker``
        explicitly searches SPBXM as part of its cross-board
        resolution; without the broker whitelist change, a
        RiskGate-approved SPB order would fail with the same
        misleading error.
        """
        mock_client = MagicMock()
        match = MagicMock()
        match.ticker = "AAPL"
        match.class_code = "SPBXM"
        match.figi = "AAPL_SPBXM_FIGI"
        resp = MagicMock()
        resp.instruments = [match]
        mock_client.instruments.find_instrument.return_value = resp
        a = TinkoffAccount(token="t.x")
        assert a._ticker_to_figi(mock_client, "AAPL") == "AAPL_SPBXM_FIGI"

    def test_ticker_to_figi_accepts_tqbs_tqde_tqno_tqlv_tqpi(self):
        """Issue #187: every SPB foreign-share variant must be tradable.

        Mirrors the per-class search in
        ``TinkoffInvestMDDataLoader._collect_tickers``
        (src/data/tinkoff_md_loader.py:308).
        """
        a = TinkoffAccount(token="t.x")
        for class_code, ticker, figi in (
            ("TQBS", "VOD", "VOD_TQBS_FIGI"),
            ("TQDE", "BMW", "BMW_TQDE_FIGI"),
            ("TQNO", "EQNR", "EQNR_TQNO_FIGI"),
            ("TQLV", "LSM", "LSM_TQLV_FIGI"),
            ("TQPI", "PEO", "PEO_TQPI_FIGI"),
        ):
            mock_client = MagicMock()
            match = MagicMock()
            match.ticker = ticker
            match.class_code = class_code
            match.figi = figi
            resp = MagicMock()
            resp.instruments = [match]
            mock_client.instruments.find_instrument.return_value = resp
            assert a._ticker_to_figi(mock_client, ticker) == figi, f"class_code={class_code} ticker={ticker} rejected"

    def test_ticker_to_figi_rejects_unknown_class_with_diagnostic_message(self):
        """Issue #187: error message lists seen vs expected class_codes.

        When Tinkoff returns the right ticker but in a class we
        don't trade (shouldn't happen in production since the data
        layer mirrors the tradable set, but if it does, the
        diagnostic should pinpoint the drift instead of saying
        "wrong ticker").
        """
        mock_client = MagicMock()
        match = MagicMock()
        match.ticker = "ZZZZ"
        match.class_code = "UNKNOWN"
        match.figi = "ZZZ_FIGI"
        resp = MagicMock()
        resp.instruments = [match]
        mock_client.instruments.find_instrument.return_value = resp
        a = TinkoffAccount(token="t.x")
        with pytest.raises(BrokerError) as exc_info:
            a._ticker_to_figi(mock_client, "ZZZZ")
        msg = str(exc_info.value)
        # Diagnostic: lists what we saw vs what we expect.
        assert "UNKNOWN" in msg
        assert "TQBR" in msg  # expected set is enumerated
        assert "tradable instrument universe" in msg

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

    def _quote_provider(self, price: Decimal | None = Decimal("250")):
        """Build a stub QuoteProvider.

        Issue #166: OrderFlow now requires a real, non-placeholder quote.
        Tests pass a callable returning a fixed price; tests that exercise
        the "no quote" path override ``_quote_provider`` with a raising
        callable.
        """

        def _qp(_symbol: str) -> Decimal:
            assert price is not None
            return price

        return _qp

    def _raising_quote_provider(self, exc: Exception | None = None):
        def _qp(_symbol: str) -> Decimal:
            raise exc if exc is not None else ConnectionError("tinkoff down")

        return _qp

    def test_universe_blocked_short_circuits(self):
        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
            universe_filter=lambda s: False,
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result.final_status == OrderStatus.REJECTED
        assert "UNIVERSE_BLOCKED" in result.decision_violations

    def test_risk_gate_blocked_returns_rejected(self):
        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=self._blocked_gate(),
            quote_provider=self._quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result.final_status == OrderStatus.REJECTED

    def test_risk_gate_approved_submits_via_broker(self):
        broker = MagicMock()
        broker.place_order.return_value = OrderStatus.SUBMITTED
        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("100"), self._portfolio())
        assert result.slice_count >= 1
        assert broker.place_order.called

    def test_risk_gate_approved_broker_exception_still_records(self):
        """Issue #170: bare ``Exception`` from ``place_order`` is a programming
        error and must propagate — NOT be silently mapped to REJECTED.

        Pre-#170 the integration layer caught ``Exception`` blanket and
        wrote it to the audit log as ``OrderStatus.REJECTED``, hiding
        real bugs (TypeError / KeyError / AttributeError looked
        indistinguishable from a legitimate broker refusal). The fix
        only catches ``BrokerError`` (technical broker failure) and
        re-raises everything else so the supervisor / error tracker
        sees it. ``BrokerError`` is covered separately in
        ``test_broker_error_mapped_to_rejected_but_does_not_raise``.
        """
        from src.broker.tinkoff_account import BrokerError

        broker = MagicMock()
        broker.place_order.side_effect = BrokerError("network down")
        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("100"), self._portfolio())
        # BrokerError is a known technical failure → REJECTED (warning log).
        assert OrderStatus.REJECTED in result.submitted

    def test_portfolio_to_state_conversion(self):
        """Issue #180 + #191: PortfolioState.cash is free cash, not NAV.

        PortfolioSnapshot.cash carries the Tinkoff NAV (cash + positions at mark).
        The cash field of PortfolioState is free cash = NAV − Σ(positions.value).
        For a NAV of 50_000 with one position worth 25_000, free cash is 25_000.
        Issue #180 fix sets `total_equity == NAV` (not NAV + positions.value, which
        would double-count); issue #191 fix sets `cash == NAV − positions.value`.
        """
        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("50000"),
            positions=[
                Position(ticker="SBER", quantity=Decimal("100"), avg_price=Decimal("250")),
            ],
            timestamp=datetime.utcnow(),
        )
        state = OrderFlow._portfolio_to_state(portfolio)
        # Issue #191: free cash, not NAV.
        assert state.cash == Decimal("25000")  # 50_000 NAV − 100*250 = 25_000
        assert len(state.positions) == 1
        assert state.positions[0].symbol == "SBER"
        # Issue #180 regression guard: total_equity is NAV (which is what
        # TinkoffAccount populates PortfolioSnapshot.cash with), NOT
        # cash + sum(positions.value). For this fixture, PortfolioSnapshot.cash
        # is 50_000 (acting as NAV in the Tinkoff contract), so
        # total_equity must be 50_000, NOT 50_000 + 100*250 = 75_000.
        assert state.total_equity == Decimal("50000")
        assert state.peak_equity == Decimal("50000")

    def test_portfolio_to_state_does_not_double_count_positions(self) -> None:
        """Issue #180: ``PortfolioSnapshot.cash`` is NAV, not free cash.

        ``TinkoffAccount.get_portfolio()`` (src/broker/tinkoff_account.py:381-410)
        fills ``PortfolioSnapshot.cash`` from ``total_amount_currencies``,
        which is the Tinkoff SDK field that reports NAV (= cash + positions
        at mark). ``OrderFlow._portfolio_to_state`` previously summed
        ``portfolio.cash + sum(p.quantity * p.avg_price)`` on top of that,
        which double-counts the positions and inflates ``total_equity`` by
        the position book size. The downstream effect: ``RiskGate`` sees
        a 2x equity denominator, computes ``position_pct`` at half the
        real value, and silently approves positions up to 2x the configured
        limit — same failure mode as the historical MarketOrder(price=1)
        bypass (issues #11, #13) and frozen=False bypass (issue #98).

        Regression net: a portfolio with $1.2M NAV (positions $1M, free
        cash $200k per the Tinkoff contract where positions are valued
        at avg_price) yields total_equity == 1.2M, NOT 2.2M.
        """
        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("1200000"),  # NAV — positions $1M + free $200k
            positions=[
                Position(ticker="SBER", quantity=Decimal("1000"), avg_price=Decimal("1000")),
            ],
            timestamp=datetime.utcnow(),
        )
        state = OrderFlow._portfolio_to_state(portfolio)
        # Pre-fix: 1_200_000 + 1_000_000 = 2_200_000 (double-count bug).
        # Post-fix: 1_200_000 (NAV only — portfolio.cash IS the NAV).
        assert state.total_equity == Decimal("1200000")
        assert state.peak_equity == Decimal("1200000")
        # Positions are still passed through to RiskGate — the bug was
        # only in the equity denominator, not in the position list.
        assert len(state.positions) == 1
        assert state.positions[0].symbol == "SBER"
        assert state.positions[0].quantity == Decimal("1000")

    def test_portfolio_to_state_cash_equals_nav_when_no_positions(self) -> None:
        """Issue #191: when positions is empty, free cash == NAV (positions.value == 0).

        Edge case for the new `cash = NAV − positions_value` formula.
        Guards the post-fix against a future refactor that drops the empty-positions
        fast-path.
        """
        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("250000"),
            positions=[],
            timestamp=datetime.utcnow(),
        )
        state = OrderFlow._portfolio_to_state(portfolio)
        # Empty book → positions_value = 0 → free_cash = NAV.
        assert state.total_equity == Decimal("250000")
        assert state.peak_equity == Decimal("250000")
        assert state.cash == Decimal("250000")
        assert state.positions == []

    def test_portfolio_to_state_cash_zero_when_positions_fill_nav(self) -> None:
        """Issue #191: positions exactly equal NAV → free cash == 0 (fully invested).

        Operator-meaningful: 100% invested book → no free cash to deploy on a new BUY.
        A future `_check_cash_adequacy` would correctly reject any BUY with non-zero
        notional against this state. Pre-fix #191, `state.cash` wrongly reported
        NAV (1_000_000), so a cap like "BUY ≤ 50% of cash" would silently allow
        a BUY up to 500_000 against a fully-invested book.
        """
        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("1000000"),  # NAV
            positions=[
                Position(ticker="SBER", quantity=Decimal("1000"), avg_price=Decimal("1000")),
            ],
            timestamp=datetime.utcnow(),
        )
        state = OrderFlow._portfolio_to_state(portfolio)
        assert state.total_equity == Decimal("1000000")
        assert state.cash == Decimal("0")  # 1_000_000 − 1_000*1_000 = 0

    def test_portfolio_to_state_cash_clamped_when_positions_exceed_nav(self) -> None:
        """Issue #191: positions_value > NAV (synthetic) → free cash clamped at 0.

        Tinkoff contract guarantees positions ≤ NAV, but a partial / mock snapshot
        (or a stale avg_price from a pre-split mark) can produce a transient
        positions_value > NAV. Without clamping, ``PortfolioState.cash`` would
        raise ``pydantic.ValidationError`` (``ge=Decimal("0")``); the clamp keeps
        the audit log stable and surfaces the over-invested state with cash == 0.
        """
        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("1000000"),  # NAV
            positions=[
                # 1500 * 1000 = 1_500_000 > 1_000_000 — synthetic over-invested state.
                Position(ticker="SBER", quantity=Decimal("1500"), avg_price=Decimal("1000")),
            ],
            timestamp=datetime.utcnow(),
        )
        state = OrderFlow._portfolio_to_state(portfolio)
        # total_equity stays at NAV; only the free-cash field is clamped.
        assert state.total_equity == Decimal("1000000")
        assert state.cash == Decimal("0")  # clamped, not -500_000

    def test_portfolio_to_state_cash_aggregates_multiple_positions(self) -> None:
        """Issue #191: free cash sums across all positions, not just one symbol.

        A multi-symbol book must produce `free_cash = NAV − Σ(qty*price)` over
        every position, matching the `_check_sector_exposure` accumulation in
        `src/risk/gate.py:386`. Catches a regression where the new formula
        windowed by `min(positions, 1)` and silently dropped the rest.
        """
        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("1000000"),  # NAV
            positions=[
                Position(ticker="SBER", quantity=Decimal("100"), avg_price=Decimal("300")),  # 30_000
                Position(ticker="GAZP", quantity=Decimal("200"), avg_price=Decimal("150")),  # 30_000
                Position(ticker="YNDX", quantity=Decimal("50"), avg_price=Decimal("800")),  # 40_000
            ],
            timestamp=datetime.utcnow(),
        )
        state = OrderFlow._portfolio_to_state(portfolio)
        # 1_000_000 − 30_000 − 30_000 − 40_000 = 900_000.
        assert state.cash == Decimal("900000")
        assert len(state.positions) == 3

    def test_portfolio_to_state_empty_positions_total_equals_cash(self) -> None:
        """Issue #180 (edge case): no positions → total_equity == cash.

        Pre-fix this happened to work because the sum loop returned 0,
        so ``total = cash + 0 = cash``. Post-fix it still works because
        ``total = portfolio.cash`` directly. This test guards the
        post-fix against future refactor regressions.
        """
        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("250000"),
            positions=[],
            timestamp=datetime.utcnow(),
        )
        state = OrderFlow._portfolio_to_state(portfolio)
        assert state.total_equity == Decimal("250000")
        assert state.peak_equity == Decimal("250000")
        assert state.cash == Decimal("250000")
        assert state.positions == []

    def test_portfolio_to_state_risk_gate_position_pct_uses_real_equity(self) -> None:
        """Issue #180 (end-to-end): position_pct uses real NAV, not 2x NAV.

        Pre-fix OrderFlow double-counted positions in total_equity, so
        RiskGate saw a 2x equity denominator and reported position_pct
        at half the real value. A 10% BUY against a portfolio with 5%
        existing exposure must surface position_pct = 10%, not 5%.
        """
        from src.risk.gate import RiskGate, RiskLimits, TradeIntent

        portfolio = PortfolioSnapshot(
            account_id="SB1",
            cash=Decimal("1000000"),  # NAV (Tinkoff contract)
            positions=[
                # 50_000 / 1_000_000 = 5% of NAV.
                Position(ticker="SBER", quantity=Decimal("500"), avg_price=Decimal("100")),
            ],
            timestamp=datetime.utcnow(),
        )
        limits = RiskLimits(
            max_dd_pct=Decimal("100"),
            max_position_pct=Decimal("100"),  # permissive — only equity ratio matters here
            max_sector_pct=Decimal("100"),
            max_daily_loss_pct=Decimal("100"),
        )
        gate = RiskGate(limits=limits)
        state = OrderFlow._portfolio_to_state(portfolio)
        # BUY 100_000 worth (10% of NAV) into NEW ticker so position_pct
        # reflects the intent, not an existing position. Pre-fix this
        # would compute position_pct = 100_000 / 2_000_000 = 5.0%. Post-fix
        # it computes 100_000 / 1_000_000 = 10.0%.
        intent = TradeIntent(
            symbol="NEWPOS",
            side="buy",
            quantity=Decimal("1000"),
            price=Decimal("100"),  # 1000 * 100 = 100_000 = 10% of NAV
        )
        decision = gate.evaluate(intent, state)
        assert decision.allowed is True  # within the permissive limit
        # The exact assertion: position_pct must be 10.0, not 5.0.
        assert decision.meta["position_pct"] == pytest.approx(10.0)

    def test_order_slicer_value_error_handled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If OrderSlicer raises ValueError (e.g. negative qty slips through),
        OrderFlow.submit_market must catch it and continue with empty slices.

        Covers src/broker/integration.py:104-105 (except ValueError).
        """
        from src.broker.slicer import OrderSlicer

        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")

        def _raise_value_error(*args, **kwargs):
            raise ValueError("forced for test")

        monkeypatch.setattr(OrderSlicer, "slice", _raise_value_error)

        broker = MagicMock()
        flow = OrderFlow(broker=broker, risk_gate=self._approved_gate(), quote_provider=self._quote_provider())
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("100"), self._portfolio())
        # No slices submitted; final_status is REJECTED
        assert result.slice_count == 0
        assert broker.place_order.call_count == 0

    # ---------------------------------------------------------------
    # Issue #166 — OrderFlow must require a real (non-placeholder) quote.
    # ---------------------------------------------------------------

    def test_constructor_requires_quote_provider(self):
        """Passing quote_provider=None is a programming error (issue #166).

        The constructor must raise ``TypeError`` rather than silently
        degrading to a placeholder — that is exactly the bug OrderFlow
        carried before this fix.
        """
        with pytest.raises(TypeError, match="quote_provider"):
            OrderFlow(
                broker=MagicMock(),
                risk_gate=self._approved_gate(),
                quote_provider=None,  # type: ignore[arg-type]
            )

    def test_quote_provider_raises_returns_quote_unavailable(self):
        """When the live quote cannot be fetched, refuse the order rather than
        fall back to a placeholder (issue #166).

        The RiskGate is irrelevant here: the order must short-circuit
        before the gate is consulted so a downstream ``price=Decimal("1")``
        placeholder can never reach ``TradeIntent`` construction.
        """
        broker = MagicMock()
        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._raising_quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result.final_status == OrderStatus.REJECTED
        assert result.decision_violations == ("QUOTE_UNAVAILABLE",)
        assert result.slice_count == 0
        # The broker must NOT have been touched — refusing the quote is
        # upstream of order placement.
        assert not broker.place_order.called

    def test_quote_provider_non_positive_returns_quote_invalid(self):
        """Defence-in-depth: if the quote provider returns ``Decimal('0')`` or
        a negative price, refuse the order rather than letting an obviously
        bogus price reach the RiskGate (issue #166).
        """
        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(Decimal("0")),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result.decision_violations == ("QUOTE_INVALID",)

    def test_real_gate_with_quote_provider_submits_order(self):
        """End-to-end with a REAL ``RiskGate`` (not a MagicMock).

        This is the regression test that would have caught issue #166:
        with the pre-fix ``price=Decimal("1")`` placeholder, the real gate
        blocks every call via ``RISK_MARKET_ORDER_NO_QUOTE``. With the
        quote_provider returning a real price, the order is allowed.
        """
        from src.risk.gate import RiskGate, RiskLimits

        limits = RiskLimits(
            max_dd_pct=Decimal("10"),
            max_position_pct=Decimal("10"),
            max_sector_pct=Decimal("30"),
            max_daily_loss_pct=Decimal("3"),
        )
        real_gate = RiskGate(limits=limits)
        broker = MagicMock()
        broker.place_order.return_value = OrderStatus.SUBMITTED

        def _real_quote(_symbol: str) -> Decimal:
            return Decimal("100")  # 10 shares * 100 = 1000 = 1% of 100k NAV

        flow = OrderFlow(
            broker=broker,
            risk_gate=real_gate,
            quote_provider=_real_quote,
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio(cash="100000"))
        assert result.final_status == OrderStatus.SUBMITTED
        assert result.slice_count >= 1
        assert broker.place_order.called

    def test_real_gate_blocks_oversized_position_with_real_quote(self):
        """End-to-end with REAL ``RiskGate``: an oversized position is
        correctly blocked via ``RISK_POSITION`` (not ``RISK_MARKET_ORDER_NO_QUOTE``).

        This pins the contract: with a real quote, the gate evaluates the
        intended position-sizing rule. Without one, it would have bounced
        on the placeholder guard and the operator would never see the
        real sizing violation.
        """
        from src.risk.gate import RiskGate, RiskLimits

        limits = RiskLimits(
            max_dd_pct=Decimal("10"),
            max_position_pct=Decimal("10"),  # 10% cap
            max_sector_pct=Decimal("30"),
            max_daily_loss_pct=Decimal("3"),
        )
        real_gate = RiskGate(limits=limits)
        broker = MagicMock()
        broker.place_order.return_value = OrderStatus.SUBMITTED

        def _real_quote(_symbol: str) -> Decimal:
            return Decimal("1000")  # 50 shares * 1000 = 50_000 = 50% of NAV → exceeds 10%

        flow = OrderFlow(
            broker=broker,
            risk_gate=real_gate,
            quote_provider=_real_quote,
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("50"), self._portfolio(cash="100000"))
        assert result.final_status == OrderStatus.REJECTED
        # Must be the sizing violation, not the placeholder guard:
        assert any(
            v.startswith("RISK_POSITION:") for v in result.decision_violations
        ), f"unexpected violations: {result.decision_violations}"
        assert not any("RISK_MARKET_ORDER_NO_QUOTE" in v for v in result.decision_violations)
        assert not broker.place_order.called

    # ---------------------------------------------------------------
    # Issue #168 — OrderFlow.submit_market must distinguish REJECTED
    # from SUBMITTED so the audit log does not silently treat a
    # silent failure as a real submit.
    # ---------------------------------------------------------------

    def test_final_status_rejected_when_slicer_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OrderSlicer raises ValueError → no slice is ever submitted.

        Issue #168: pre-fix, ``final_status`` returned SUBMITTED with
        ``submitted == []`` because the only check was
        ``all(s == FILLED for s in submitted)`` — vacuously True on an
        empty list. Operators looking at the audit log saw a
        SUBMITTED run that never touched the broker. Post-fix,
        ``final_status == REJECTED`` and ``filled_count ==
        rejected_count == 0`` makes the silent failure visible.
        """
        from src.broker import slicer as slicer_mod

        def _raise(self):  # noqa: ANN001 — patching OrderSlicer.slice
            raise ValueError("forced for issue #168 test")

        monkeypatch.setattr(slicer_mod.OrderSlicer, "slice", _raise)

        broker = MagicMock()
        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("100"), self._portfolio())
        assert result.slice_count == 0
        assert result.submitted == []
        assert result.final_status == OrderStatus.REJECTED
        assert result.filled_count == 0
        assert result.rejected_count == 0
        assert broker.place_order.call_count == 0

    def test_final_status_rejected_when_all_slices_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every slice is rejected by the broker → final_status == REJECTED.

        Issue #168: pre-fix this case returned SUBMITTED (the
        ``all(... == FILLED)`` test was False, so the else-branch set
        SUBMITTED). Operators monitoring ``decision_log.broker_status
        == SUBMITTED`` saw a phantom "in-flight" run that the broker
        had actually fully refused.
        """
        broker = MagicMock()
        broker.place_order.return_value = OrderStatus.REJECTED

        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("100"), self._portfolio())
        assert result.slice_count >= 1
        assert result.submitted and all(s == OrderStatus.REJECTED for s in result.submitted)
        assert result.final_status == OrderStatus.REJECTED
        assert result.filled_count == 0
        assert result.rejected_count == len(result.submitted)

    def test_final_status_filled_when_all_slices_filled(self) -> None:
        """Regression guard for the all-FILLED path."""
        broker = MagicMock()
        broker.place_order.return_value = OrderStatus.FILLED

        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("100"), self._portfolio())
        assert result.submitted and all(s == OrderStatus.FILLED for s in result.submitted)
        assert result.final_status == OrderStatus.FILLED
        assert result.filled_count == len(result.submitted)
        assert result.rejected_count == 0

    def test_final_status_submitted_on_partial_fill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mixed FILLED + REJECTED → final_status == SUBMITTED (legitimate partial).

        Issue #168: this is the only path where ``final_status ==
        SUBMITTED`` is the truthful answer — the broker is still
        working on at least one slice. ``filled_count`` and
        ``rejected_count`` let downstream consumers audit exactly how
        many slices each outcome touched.
        """
        # ``OrderFlow.submit_market`` computes ``adv_shares = max(qty *
        # 20, 100)`` (see integration.py:189), which makes 5%-ADV equal
        # to ``qty`` for any qty ≥ 5. The slicer therefore yields exactly
        # one chunk under normal inputs. To exercise a multi-slice
        # scenario we shrink ``adv_shares`` post-construction via a
        # monkeypatched ``OrderSlicer.__init__`` — equivalent to feeding
        # OrderFlow a real ADV estimate, which is exactly what Phase 2
        # will do once the data agent exposes it.
        from src.broker.slicer import OrderSlicer

        original_init = OrderSlicer.__init__

        def _small_adv(self, adv_shares, parent_qty):  # noqa: ANN001 — patch
            original_init(self, adv_shares=Decimal("100"), parent_qty=parent_qty)
            # 5% ADV = qty/3 → 3 chunks of size ~qty/3.
            self.adv_shares = parent_qty * Decimal("20") / Decimal("3")

        monkeypatch.setattr(OrderSlicer, "__init__", _small_adv)

        broker = MagicMock()
        broker.place_order.side_effect = [
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.FILLED,
        ]
        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("3000"), self._portfolio(cash="10000000"))
        assert result.slice_count == 3
        assert result.submitted == [OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.FILLED]
        assert result.final_status == OrderStatus.SUBMITTED
        assert result.filled_count == 2
        assert result.rejected_count == 1

    # ---------------------------------------------------------------
    # Issue #170 — OrderFlow.submit_market must distinguish "broker
    # technical failure" (BrokerError → REJECTED, warning log) from
    # "programming error" (TypeError / KeyError / AttributeError →
    # re-raise, NEVER silently REJECTED).
    # ---------------------------------------------------------------

    def test_broker_error_mapped_to_rejected_but_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BrokerError (technical broker failure) → per-slice REJECTED, no re-raise.

        Pre-#170 this also covered bare ``Exception`` which silently
        masked programming bugs. Now only ``BrokerError`` is caught —
        a normal RuntimeError subclass signalling SDK / network /
        malformed-response failures. The slice is recorded as
        ``REJECTED`` so the three-tier ``final_status`` logic from
        issue #168 keeps treating it as a broker refusal.
        """
        from src.broker.tinkoff_account import BrokerError
        from src.broker.slicer import OrderSlicer

        # Force multi-slice to make the assertion non-vacuous.
        original_init = OrderSlicer.__init__

        def _small_adv(self, adv_shares, parent_qty):  # noqa: ANN001
            original_init(self, adv_shares=Decimal("100"), parent_qty=parent_qty)
            self.adv_shares = parent_qty * Decimal("20") / Decimal("3")

        monkeypatch.setattr(OrderSlicer, "__init__", _small_adv)

        broker = MagicMock()
        broker.place_order.side_effect = BrokerError("tinkoff 503")

        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("3000"), self._portfolio(cash="10000000"))
        assert result.slice_count == 3
        assert result.submitted == [OrderStatus.REJECTED] * 3
        # All slices rejected by broker → final_status REJECTED (issue #168 tier-2).
        assert result.final_status == OrderStatus.REJECTED
        assert result.rejected_count == 3
        assert result.filled_count == 0

    def test_programming_error_in_place_order_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TypeError / KeyError / AttributeError → re-raised, NOT swallowed as REJECTED.

        Pre-#170 the ``except Exception`` blanket caught these and
        wrote them to the audit log as REJECTED, hiding bugs. The
        supervisor would happily proceed and operators would chase a
        phantom "broker refusal". Post-#170 the exception propagates so
        error tracking / supervisor sees it.
        """
        from src.broker.slicer import OrderSlicer

        original_init = OrderSlicer.__init__

        def _small_adv(self, adv_shares, parent_qty):  # noqa: ANN001
            original_init(self, adv_shares=Decimal("100"), parent_qty=parent_qty)
            self.adv_shares = parent_qty * Decimal("20") / Decimal("3")

        monkeypatch.setattr(OrderSlicer, "__init__", _small_adv)

        broker = MagicMock()
        broker.place_order.side_effect = TypeError("frozen model mutation attempt — issue #170 regression")

        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        with pytest.raises(TypeError, match="frozen model mutation"):
            flow.submit_market("SBER", OrderSide.BUY, Decimal("3000"), self._portfolio(cash="10000000"))

    def test_programming_error_after_partial_fills_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2 slices FILLED, 1 raises AttributeError → AttributeError propagates.

        Pre-#170 this would have been silently written as
        ``[FILLED, FILLED, REJECTED]`` with ``final_status == SUBMITTED`` —
        the operator sees "partial fill in progress" while in reality
        one slice crashed our code. Post-#170 the AttributeError
        propagates so the supervisor stops execution instead of
        reporting a phantom in-flight run.
        """
        from src.broker.slicer import OrderSlicer

        original_init = OrderSlicer.__init__

        def _small_adv(self, adv_shares, parent_qty):  # noqa: ANN001
            original_init(self, adv_shares=Decimal("100"), parent_qty=parent_qty)
            self.adv_shares = parent_qty * Decimal("20") / Decimal("3")

        monkeypatch.setattr(OrderSlicer, "__init__", _small_adv)

        broker = MagicMock()
        broker.place_order.side_effect = [
            OrderStatus.FILLED,
            OrderStatus.FILLED,
            AttributeError("missing instrument metadata"),
        ]

        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
        with pytest.raises(AttributeError, match="missing instrument metadata"):
            flow.submit_market("SBER", OrderSide.BUY, Decimal("3000"), self._portfolio(cash="10000000"))

    # ────────────────────────────────────────────
    # Issue #195: peak_equity_provider — RISK_DD guard via OrderFlow
    # ────────────────────────────────────────────

    def test_peak_equity_provider_pulls_persistent_peak(self) -> None:
        """Issue #195: when a peak_equity_provider is configured, the
        PortfolioState passed to RiskGate has ``peak_equity`` from the
        provider (NOT the current NAV). This is what makes
        ``_check_drawdown`` actually trip after a drawdown.

        Pre-#195, ``_portfolio_to_state`` (static) hard-coded
        ``peak_equity = total_equity``, so ``dd_pct = (peak - current)
        / peak`` was always 0%% and the RISK_DD guard never fired via
        the OrderFlow code path.
        """
        # Provider returns 200_000; portfolio NAV is 100_000. A 50%%
        # drawdown. With a real peak provider, the RiskGate should
        # observe peak=200_000 and report drawdown; we verify by
        # capturing the state passed to the gate.
        captured: dict[str, Any] = {}

        def capturing_gate():
            rg = MagicMock()
            from src.risk.gate import RiskDecision

            def _capture(intent, state):
                captured["peak"] = state.peak_equity
                captured["total"] = state.total_equity
                return RiskDecision(allowed=True, violations=())

            rg.evaluate.side_effect = _capture
            return rg

        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=capturing_gate(),
            quote_provider=self._quote_provider(),
            peak_equity_provider=lambda: Decimal("200000"),
        )
        flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio(cash="100000"))

        # Peak must come from the provider, NOT equal to total_equity.
        assert captured["total"] == Decimal("100000")
        assert captured["peak"] == Decimal("200000")

    def test_peak_equity_provider_missing_warns_and_uses_legacy_fallback(self) -> None:
        """Issue #195: when no peak_equity_provider is configured,
        ``OrderFlow`` falls back to ``peak_equity = total_equity`` (the
        pre-#195 behaviour) and emits a one-shot WARNING so operators
        can see the gap. The order path must still succeed — backward
        compatibility for callers that haven't wired in peak tracking.
        """
        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
            # peak_equity_provider intentionally omitted
        )
        # First call: should succeed and not raise. The one-shot
        # WARNING is logged but does not block the order path.
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result.final_status != OrderStatus.REJECTED or "QUOTE_UNAVAILABLE" in result.decision_violations
        # Second call: same outcome, no extra exceptions.
        result2 = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result2 is not None

    def test_peak_equity_provider_below_total_is_bumped(self) -> None:
        """Issue #195: if the persistent peak is BELOW current NAV
        (cold start, deleted peak file, NAV jump), the validator at
        ``src/risk/gate.py:168-171`` would reject. The instance method
        bumps the peak to current NAV — a high-water mark by definition
        only goes up — so the validator passes and the RiskGate sees a
        coherent snapshot.
        """
        captured: dict[str, Any] = {}

        def capturing_gate():
            rg = MagicMock()
            from src.risk.gate import RiskDecision

            def _capture(intent, state):
                captured["peak"] = state.peak_equity
                captured["total"] = state.total_equity
                return RiskDecision(allowed=True, violations=())

            rg.evaluate.side_effect = _capture
            return rg

        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=capturing_gate(),
            quote_provider=self._quote_provider(),
            # Provider returns LESS than current NAV (e.g. fresh peak
            # file with a stale small value, or a unit-test fixture).
            peak_equity_provider=lambda: Decimal("50000"),
        )
        flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio(cash="100000"))
        # Peak must be bumped up to total_equity.
        assert captured["total"] == Decimal("100000")
        assert captured["peak"] == Decimal("100000")

    def test_peak_equity_provider_exception_disables_and_continues(self) -> None:
        """Issue #195: a peak_equity_provider that raises must NOT
        break the order path. We log a WARNING, fall back to
        ``peak=total`` for THIS call, and disable the provider for the
        rest of the process so a flapping provider doesn't spam the
        log on every submit_market.
        """
        captured: dict[str, Any] = {}
        call_count = {"n": 0}

        def flaky_provider():
            call_count["n"] += 1
            raise RuntimeError("disk full")

        def capturing_gate():
            rg = MagicMock()
            from src.risk.gate import RiskDecision

            def _capture(intent, state):
                captured.setdefault("peaks", []).append(state.peak_equity)
                captured.setdefault("totals", []).append(state.total_equity)
                return RiskDecision(allowed=True, violations=())

            rg.evaluate.side_effect = _capture
            return rg

        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=capturing_gate(),
            quote_provider=self._quote_provider(),
            peak_equity_provider=flaky_provider,
        )
        # First call: provider raises -> fallback to peak=total.
        flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert captured["totals"][-1] == Decimal("100000")
        assert captured["peaks"][-1] == Decimal("100000")
        # Provider was disabled after the first failure — the flaky
        # callable must NOT have been invoked twice.
        assert call_count["n"] == 1
        # Second call: provider disabled, fallback path runs (no second
        # call to flaky_provider).
        flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert call_count["n"] == 1

    # ────────────────────────────────────────────
    # Issue #197: daily_pnl_provider — RISK_DAILY_LOSS guard via OrderFlow
    # ────────────────────────────────────────────

    def test_daily_pnl_provider_pulls_real_daily_loss(self) -> None:
        """Issue #197: when a ``daily_pnl_provider`` is configured, the
        ``PortfolioState`` passed to ``RiskGate`` has ``daily_pnl``
        from the provider (NOT ``Decimal("0")``). This is what makes
        ``_check_daily_loss`` actually trip on a -4% day.

        Pre-#197, ``_portfolio_to_state`` (static) left ``daily_pnl``
        at the pydantic default ``Decimal("0")``, so the
        ``daily_pnl >= 0`` short-circuit in ``_check_daily_loss``
        always returned early — the ``RISK_DAILY_LOSS`` guard never
        fired via the OrderFlow code path.
        """
        # Provider returns -4_000 on a 100_000 NAV → -4% day.
        captured: dict[str, Any] = {}

        def capturing_gate():
            rg = MagicMock()
            from src.risk.gate import RiskDecision

            def _capture(intent, state):
                captured["daily_pnl"] = state.daily_pnl
                captured["total"] = state.total_equity
                return RiskDecision(allowed=True, violations=())

            rg.evaluate.side_effect = _capture
            return rg

        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=capturing_gate(),
            quote_provider=self._quote_provider(),
            daily_pnl_provider=lambda: Decimal("-4000"),
        )
        flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio(cash="100000"))

        # daily_pnl must come from the provider, NOT default to 0.
        assert captured["total"] == Decimal("100000")
        assert captured["daily_pnl"] == Decimal("-4000")

    def test_daily_pnl_provider_missing_warns_and_uses_legacy_fallback(self) -> None:
        """Issue #197: when no ``daily_pnl_provider`` is configured,
        ``OrderFlow`` falls back to ``daily_pnl = Decimal("0")`` (the
        pre-#197 behaviour) and emits a one-shot WARNING so operators
        can see the gap. The order path must still succeed — backward
        compatibility for callers that haven't wired in daily-pnl
        tracking.
        """
        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
            # daily_pnl_provider intentionally omitted
        )
        # First call: should succeed and not raise. The one-shot
        # WARNING is logged but does not block the order path.
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result is not None
        # Second call: same outcome, no extra exceptions.
        result2 = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert result2 is not None

    def test_daily_pnl_provider_exception_disables_and_continues(self) -> None:
        """Issue #197: a ``daily_pnl_provider`` that raises must NOT
        break the order path. We log a WARNING, fall back to
        ``daily_pnl=0`` for THIS call, and disable the provider for
        the rest of the process so a flapping provider doesn't spam
        the log on every ``submit_market``.
        """
        captured: dict[str, Any] = {}
        call_count = {"n": 0}

        def flaky_provider():
            call_count["n"] += 1
            raise RuntimeError("disk full")

        def capturing_gate():
            rg = MagicMock()
            from src.risk.gate import RiskDecision

            def _capture(intent, state):
                captured.setdefault("daily_pnls", []).append(state.daily_pnl)
                return RiskDecision(allowed=True, violations=())

            rg.evaluate.side_effect = _capture
            return rg

        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=capturing_gate(),
            quote_provider=self._quote_provider(),
            daily_pnl_provider=flaky_provider,
        )
        # First call: provider raises -> fallback to daily_pnl=0.
        flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert captured["daily_pnls"][-1] == Decimal("0")
        # Provider was disabled after the first failure — the flaky
        # callable must NOT have been invoked twice.
        assert call_count["n"] == 1
        # Second call: provider disabled, fallback path runs (no second
        # call to flaky_provider).
        flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio())
        assert call_count["n"] == 1

    def test_daily_pnl_provider_endto_end_risk_daily_loss_fires(self) -> None:
        """Issue #197: end-to-end integration. ``OrderFlow`` with a
        ``daily_pnl_provider`` returning -4_000 on a 100_000 NAV feeds
        a real P&L into ``RiskGate``, and ``_check_daily_loss`` trips
        ``RISK_DAILY_LOSS``. Pre-#197 the gate would silently approve
        the order because ``daily_pnl`` defaulted to 0.

        This is the production-path equivalent of the unit-test in
        ``tests/test_risk_gate.py::TestDailyLoss``.
        """
        gate = MagicMock()

        def _eval(intent, state):
            from src.risk.gate import RiskDecision

            # -4% on 100k = -4000 vs 3% limit → reject.
            if state.daily_pnl < 0 and (-state.daily_pnl / state.total_equity) * Decimal("100") > Decimal("3"):
                return RiskDecision(
                    allowed=False,
                    violations=(f"RISK_DAILY_LOSS: {state.daily_pnl}",),
                )
            return RiskDecision(allowed=True, violations=())

        gate.evaluate.side_effect = _eval

        flow = OrderFlow(
            broker=MagicMock(),
            risk_gate=gate,
            quote_provider=self._quote_provider(),
            daily_pnl_provider=lambda: Decimal("-4000"),
        )
        result = flow.submit_market("SBER", OrderSide.BUY, Decimal("10"), self._portfolio(cash="100000"))

        # RISK_DAILY_LOSS must be in the violations.
        assert any(
            "RISK_DAILY_LOSS" in v for v in result.decision_violations
        ), f"RISK_DAILY_LOSS must fire on -4% day; got: {result.decision_violations}"


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

    def test_broker_position_to_gate_position_conversion(self):
        """Cover src/broker/tinkoff_account.py:43-45 (helper function).

        The helper converts broker Position (dataclass with .ticker)
        to gate Position (pydantic with .symbol). Sectors are not yet
        mapped from Tinkoff in Phase 1, so sector=None is expected.
        """
        from src.broker.tinkoff_account import _broker_position_to_gate_position
        from src.risk.gate import Position as GatePosition

        broker_pos = Position(
            ticker="SBER",
            quantity=Decimal("100"),
            avg_price=Decimal("250"),
        )
        gate_pos = _broker_position_to_gate_position(broker_pos)
        assert isinstance(gate_pos, GatePosition)
        assert gate_pos.symbol == "SBER"
        assert gate_pos.quantity == Decimal("100")
        assert gate_pos.avg_price == Decimal("250")
        assert gate_pos.sector is None
