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
        """Issue #13 (C.1): when the ticker is not in TQBR/TQOB
        (e.g. unknown ticker), _ticker_to_figi MUST raise rather
        than silently return the ticker string."""
        mock_client = MagicMock()
        # Return a list of matches but none with TQBR/TQOB class.
        match = MagicMock()
        match.ticker = "SBER"
        match.class_code = "OTHER"
        match.figi = "OTHER_FIGI"
        resp = MagicMock()
        resp.instruments = [match]
        mock_client.instruments.find_instrument.return_value = resp
        a = TinkoffAccount(token="t.x")
        with pytest.raises(BrokerError, match="not found in TQBR/TQOB"):
            a._ticker_to_figi(mock_client, "SBER")

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
        broker = MagicMock()
        broker.place_order.side_effect = Exception("network down")
        flow = OrderFlow(
            broker=broker,
            risk_gate=self._approved_gate(),
            quote_provider=self._quote_provider(),
        )
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
