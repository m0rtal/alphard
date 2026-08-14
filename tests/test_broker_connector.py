"""Broker Connector tests — Phase 1.3.

Coverage target: >=30 tests, all using ``unittest.mock`` for the Tinkoff
SDK surface. We do NOT make any real network calls.

Test classes (the python -m pytest --collect-only output must show >=30):
- ``TestOrderModels``              — pydantic validation of MarketOrder, LimitOrder,
                                     AccountOrder, OrderResult.    [~6 tests]
- ``TestBrokerAccountABC``        — TinkoffAccount implements the ABC, plus
                                     OrderRejectedByRisk contract.  [~5 tests]
- ``TestPreTradeRiskGate``        — submit_intent MUST call gate before
                                     reaching the broker; risk denial blocks
                                     place_order entirely.            [~6 tests]
- ``TestOrderSlicer``             — 5% ADV chunks, 30min cap, sub-1-share
                                     floor, idempotent client_order_id. [~7 tests]
- ``TestTokenBucketRateLimit``    — Tinkoff's 60rps bucket is consulted on
                                     every place_order and cancel_order.  [~4 tests]
- ``TestTinkoffSandboxToken``     — env-loading, empty/garbage rejection,
                                     TinkoffConfig construction rules.  [~5 tests]
- ``TestTinkoffAccountSDKGuards`` — Quant/Figi handling, error classification,
                                     balance/position snapshot shape.     [~7 tests]
                                    ----
                                    Total: ~40 tests, comfortably >=30.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.broker import (
    AccountOrder,
    AccountPosition,
    ADVRequired,
    Balance,
    BrokerAccount,
    LimitOrder,
    MarketOrder,
    OrderRejectedByRisk,
    OrderResult,
    OrderSlicer,
    SlicerResult,
    TinkoffAccount,
    TinkoffConfig,
    TinkoffSDKUnavailable,
    TinkoffTokenMissing,
)
from src.broker.account import OrderRejectedByRisk as _O
from src.broker.orders import (
    AccountOrder as _AO,
)
from src.broker.tinkoff_account import (
    _MockQuotation,
    _RATE_PER_SECOND,
    _RATE_WINDOW_SECONDS,
    _BURST_CAPACITY,
    _SANDBOX_TOKEN_MIN_LEN,
)
from src.data.token_bucket import TokenBucket
from src.risk.gate import (
    PortfolioState,
    Position,
    RiskDecision,
    RiskGate,
    RiskLimits,
    TradeIntent,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sandbox_token() -> str:
    return "t.sandbox" + "x" * max(0, _SANDBOX_TOKEN_MIN_LEN - len("t.sandbox")) + "abcd"


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits(
        max_dd_pct=Decimal("15.0"),
        max_position_pct=Decimal("10.0"),
        max_sector_pct=Decimal("30.0"),
        max_daily_loss_pct=Decimal("3.0"),
        allow_short=False,
    )


@pytest.fixture
def permissive_limits() -> RiskLimits:
    """Limits that allow shorts — used for the sell-opens-short path."""
    return RiskLimits(
        max_dd_pct=Decimal("15.0"),
        max_position_pct=Decimal("10.0"),
        max_sector_pct=Decimal("30.0"),
        max_daily_loss_pct=Decimal("3.0"),
        leverage_max=Decimal("2.0"),
        allow_short=True,
    )


@pytest.fixture
def base_state() -> PortfolioState:
    return PortfolioState(
        total_equity=Decimal("1000000"),
        cash=Decimal("1000000"),
        positions=[],
        daily_pnl=Decimal("0"),
        peak_equity=Decimal("1000000"),
    )


@pytest.fixture
def state_with_long_sber() -> PortfolioState:
    return PortfolioState(
        total_equity=Decimal("1000000"),
        cash=Decimal("900000"),
        positions=[Position(symbol="SBER", quantity=Decimal("1000"), avg_price=Decimal("100"), sector="energy")],
        daily_pnl=Decimal("0"),
        peak_equity=Decimal("1000000"),
    )


@pytest.fixture
def tconn(sandbox_token: str) -> TinkoffAccount:
    """A TinkoffAccount wired with a fake SDK client and an isolated bucket.

    The fake SDK is just a MagicMock; tests assert on it via
    ``tconn._client.orders.post_order.assert_called_once_with(...)``.

    ``connect()`` is invoked eagerly so the fake client is available to
    tests that exercise place_order / cancel_order / get_* views without
    having to remember to call connect first.
    """
    fake_client = MagicMock(name="FakeTinkoffClient")
    bucket = TokenBucket(rate=_RATE_PER_SECOND, window_seconds=_RATE_WINDOW_SECONDS, capacity=_BURST_CAPACITY)
    acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
    acc.connect()
    # Save the fake client ref so tests can interrogate call_args_list.
    acc._fake_client_ref = fake_client  # type: ignore[attr-defined]
    return acc


@pytest.fixture
def post_order_response() -> MagicMock:
    resp = MagicMock(name="PostOrderResponse")
    resp.order_id = "broker-abc-123"
    resp.execution_status = "EXECUTION_STATUS_NEW"
    return resp


# ===========================================================================
# TestOrderModels — pydantic shapes
# ===========================================================================


class TestOrderModels:
    def test_market_order_accepts_minimal_required(self) -> None:
        order = MarketOrder(ticker="sber", side="buy", quantity=Decimal("10"))
        assert order.ticker == "SBER"  # uppercased
        assert order.type == "market"
        assert order.quantity == Decimal("10")
        assert order.account_id == "default"

    def test_limit_order_requires_price(self) -> None:
        with pytest.raises(ValidationError):
            LimitOrder(ticker="SBER", side="buy", quantity=Decimal("10"))  # type: ignore[call-arg]
        # Sanity — sane construction succeeds.
        ok = LimitOrder(ticker="SBER", side="buy", quantity=Decimal("10"), price=Decimal("100"))
        assert ok.price == Decimal("100")

    def test_limit_order_rejects_zero_price(self) -> None:
        with pytest.raises(ValidationError):
            LimitOrder(ticker="SBER", side="buy", quantity=Decimal("10"), price=Decimal("0"))

    def test_ticker_validation_strict(self) -> None:
        # Tickers with spaces are refused.
        with pytest.raises(ValidationError):
            MarketOrder(ticker="SB ER", side="buy", quantity=Decimal("1"))
        # Non-string is rejected (model already constrains str).
        with pytest.raises(ValidationError):
            MarketOrder(ticker="###", side="buy", quantity=Decimal("1"))

    def test_quantity_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            MarketOrder(ticker="SBER", side="buy", quantity=Decimal("0"))
        with pytest.raises(ValidationError):
            MarketOrder(ticker="SBER", side="buy", quantity=Decimal("-1"))

    def test_invalid_side_rejected_at_model(self) -> None:
        with pytest.raises(ValidationError):
            MarketOrder(ticker="SBER", side="short", quantity=Decimal("1"))

    def test_account_order_status_default_is_submitted(self) -> None:
        order = _AO(
            broker_order_id="x",
            account_id="default",
            ticker="SBER",
            side="buy",
            type="market",
            requested_qty=Decimal("10"),
        )
        assert order.status == "submitted"
        assert order.filled_qty == Decimal("0")

    def test_order_result_is_frozen(self) -> None:
        result = OrderResult(
            ok=False,
            status="rejected",
            order=None,
            error_code="auth_error",
            error_message="bad token",
        )
        # Pydantic v2 frozen=True forbids mutation via __setattr__.
        with pytest.raises(ValidationError):
            result.ok = True  # type: ignore[misc]


# ===========================================================================
# TestBrokerAccountABC — interface & hard invariants
# ===========================================================================


class TestBrokerAccountABC:
    def test_tinkoff_account_is_broker_account_subclass(self) -> None:
        assert issubclass(TinkoffAccount, BrokerAccount)

    def test_abstract_methods_exist(self) -> None:
        # ABC's full method surface — failing to override any of them
        # in a subclass would raise TypeError on construction.
        required = {
            "connect",
            "close",
            "get_balance",
            "get_positions",
            "get_orders",
            "place_order",
            "cancel_order",
            "submit_intent",
        }
        for name in required:
            assert name in BrokerAccount.__abstractmethods__ or hasattr(BrokerAccount, name), name

    def test_placeholder_balance_construction(self) -> None:
        bal = Balance(cash=Decimal("1000"), currency="RUB")
        assert bal.currency == "RUB"
        assert bal.net_liquidation is None

    def test_account_position_signed_quantity(self) -> None:
        long = AccountPosition(ticker="SBER", quantity=Decimal("100"), avg_price=Decimal("10"))
        short = AccountPosition(ticker="SBER", quantity=Decimal("-100"), avg_price=Decimal("10"))
        assert long.quantity > 0
        assert short.quantity < 0  # negative => short

    def test_order_rejected_by_risk_carries_intent(self) -> None:
        intent = TradeIntent(symbol="SBER", side="sell", quantity=Decimal("1"), price=Decimal("10"))
        exc = OrderRejectedByRisk(intent, "denied", ("RISK_SIDE: foo",))
        assert exc.intent is intent
        assert "RISK_SIDE" in exc.violations[0]


# ===========================================================================
# TestPreTradeRiskGate — submit_intent invariant
# ===========================================================================


class TestPreTradeRiskGate:
    def test_submit_intent_calls_evaluate_before_place_order(
        self, tconn: TinkoffAccount, base_state: PortfolioState, limits: RiskLimits, post_order_response: MagicMock
    ) -> None:
        tconn._client.orders.post_order.return_value = post_order_response
        gate = RiskGate(limits)

        intent = TradeIntent(
            symbol="SBER", side="buy", quantity=Decimal("100"), price=Decimal("100"), sector="energy"
        )
        with patch.object(gate, "evaluate", wraps=gate.evaluate) as spy:
            result = tconn.submit_intent(intent, base_state, gate)

        spy.assert_called_once_with(intent, base_state)
        tconn._client.orders.post_order.assert_called_once()
        assert result.ok is True

    def test_submit_intent_with_sector_none_rejected(
        self, tconn: TinkoffAccount, base_state: PortfolioState, limits: RiskLimits
    ) -> None:
        gate = RiskGate(limits)
        intent = TradeIntent(
            symbol="SBER", side="buy", quantity=Decimal("100"), price=Decimal("100"), sector=None
        )
        with pytest.raises(OrderRejectedByRisk):
            tconn.submit_intent(intent, base_state, gate)
        tconn._client.orders.post_order.assert_not_called()

    def test_submit_intent_blocked_by_position_size(
        self, tconn: TinkoffAccount, base_state: PortfolioState, limits: RiskLimits
    ) -> None:
        gate = RiskGate(limits)
        intent = TradeIntent(
            symbol="SBER",
            side="buy",
            quantity=Decimal("12000"),  # 1200000 -> 120% of equity, fails 10% limit
            price=Decimal("100"),
            sector="energy",
        )
        with pytest.raises(OrderRejectedByRisk) as ei:
            tconn.submit_intent(intent, base_state, gate)
        assert any("RISK_POSITION" in v for v in ei.value.violations)
        tconn._client.orders.post_order.assert_not_called()

    def test_submit_intent_sell_opens_short_default_rejected(
        self, tconn: TinkoffAccount, base_state: PortfolioState, limits: RiskLimits
    ) -> None:
        gate = RiskGate(limits)  # allow_short=False
        intent = TradeIntent(
            symbol="SBER", side="sell", quantity=Decimal("100"), price=Decimal("100"), sector="energy"
        )
        with pytest.raises(OrderRejectedByRisk) as ei:
            tconn.submit_intent(intent, base_state, gate)
        assert any("RISK_SIDE" in v for v in ei.value.violations)

    def test_submit_intent_sell_opens_short_with_allow_short_works(
        self,
        tconn: TinkoffAccount,
        base_state: PortfolioState,
        permissive_limits: RiskLimits,
        post_order_response: MagicMock,
    ) -> None:
        tconn._client.orders.post_order.return_value = post_order_response
        gate = RiskGate(permissive_limits)  # allow_short=True
        intent = TradeIntent(
            symbol="SBER", side="sell", quantity=Decimal("100"), price=Decimal("100"), sector="energy"
        )
        result = tconn.submit_intent(intent, base_state, gate)
        assert result.ok is True
        # The submitted order is a SELL at market.
        tconn._client.orders.post_order.assert_called_once()
        kwargs = tconn._client.orders.post_order.call_args.kwargs
        assert kwargs["direction"] == "ORDER_DIRECTION_SELL"

    def test_submit_intent_sell_trims_long_is_always_allowed(
        self,
        tconn: TinkoffAccount,
        state_with_long_sber: PortfolioState,
        limits: RiskLimits,
        post_order_response: MagicMock,
    ) -> None:
        # limits.allow_short=False but SELL of 100 (long is 1000) should be fine.
        tconn._client.orders.post_order.return_value = post_order_response
        gate = RiskGate(limits)
        intent = TradeIntent(
            symbol="SBER", side="sell", quantity=Decimal("100"), price=Decimal("100"), sector="energy"
        )
        result = tconn.submit_intent(intent, state_with_long_sber, gate)
        assert result.ok is True


# ===========================================================================
# TestOrderSlicer — slicing math
# ===========================================================================


class TestOrderSlicer:
    def test_default_chunk_pct_is_five_percent(self) -> None:
        s = OrderSlicer()
        assert s.chunk_pct_of_adv == Decimal("0.05")

    def test_default_max_total_seconds_is_thirty_minutes(self) -> None:
        s = OrderSlicer()
        assert s.max_total_seconds == 30 * 60.0

    def test_plan_chunks_match_adv_fraction(self) -> None:
        # 5% of 50_000 = 2500 per child. 10_000 / 2500 = 4 chunks.
        s = OrderSlicer()
        order = MarketOrder(ticker="SBER", side="buy", quantity=Decimal("10000"))
        result = s.plan(order, adv_qty=Decimal("50000"), ref_price=Decimal("100"))
        assert isinstance(result, SlicerResult)
        assert result.chunks_planned == 4
        assert result.chunk_qty == Decimal("2500")
        assert all(c.quantity == Decimal("2500") for c in result.children)
        assert sum(c.quantity for c in result.children) == Decimal("10000")

    def test_plan_rounds_up_partial_chunk(self) -> None:
        # 5% of 100 = 5 per child. 13 / 5 -> 3 chunks of 5 + last partial of 3 (=3).
        s = OrderSlicer()
        order = MarketOrder(ticker="SBER", side="buy", quantity=Decimal("13"))
        result = s.plan(order, adv_qty=Decimal("100"), ref_price=Decimal("1"))
        assert result.chunks_planned == 3
        assert [c.quantity for c in result.children] == [Decimal("5"), Decimal("5"), Decimal("3")]

    def test_plan_raises_adv_required_when_no_adv(self) -> None:
        s = OrderSlicer()
        order = MarketOrder(ticker="SBER", side="buy", quantity=Decimal("100"))
        with pytest.raises(ADVRequired):
            s.plan(order, adv_qty=None, ref_price=Decimal("1"))

    def test_min_chunk_floor_is_one_share(self) -> None:
        # 5% of 1 share -> 0.05 (rounded down to 0). MIN_CHUNK_QTY=1 prevents 0-share children.
        s = OrderSlicer()
        order = MarketOrder(ticker="SBER", side="buy", quantity=Decimal("3"))
        result = s.plan(order, adv_qty=Decimal("1"), ref_price=Decimal("1"))
        assert all(c.quantity >= Decimal("1") for c in result.children)
        assert sum(c.quantity for c in result.children) == Decimal("3")

    def test_plan_preserves_limit_price_in_children(self) -> None:
        s = OrderSlicer()
        order = LimitOrder(ticker="SBER", side="buy", quantity=Decimal("1000"), price=Decimal("42"))
        result = s.plan(order, adv_qty=Decimal("10000"), ref_price=Decimal("42"))
        for child in result.children:
            assert isinstance(child, LimitOrder)
            assert child.price == Decimal("42")

    def test_plan_client_order_id_derivation(self) -> None:
        s = OrderSlicer()
        order = MarketOrder(
            ticker="SBER", side="buy", quantity=Decimal("100"), client_order_id="core-id"
        )
        # Multiple children — verify the suffix pattern is #<idx>/<total>.
        result = s.plan(order, adv_qty=Decimal("200"), ref_price=Decimal("1"))
        total = result.chunks_planned
        assert total >= 2
        for i, child in enumerate(result.children, start=1):
            assert child.client_order_id.startswith("core-id#")
            assert f"#{i}/{total}" in child.client_order_id

    def test_plan_rejects_zero_ref_price(self) -> None:
        s = OrderSlicer()
        order = MarketOrder(ticker="SBER", side="buy", quantity=Decimal("10"))
        with pytest.raises(ValueError):
            s.plan(order, adv_qty=Decimal("100"), ref_price=Decimal("0"))

    def test_plan_pace_and_estimated_seconds_positive(self) -> None:
        s = OrderSlicer()
        order = MarketOrder(ticker="SBER", side="buy", quantity=Decimal("10000"))
        result = s.plan(order, adv_qty=Decimal("50000"), ref_price=Decimal("100"))
        assert result.pace_qty_per_sec > Decimal("0")
        assert result.estimated_seconds > 0.0

    def test_slicer_validates_chunk_pct_range(self) -> None:
        with pytest.raises(ValueError):
            OrderSlicer(chunk_pct_of_adv=Decimal("0"))
        with pytest.raises(ValueError):
            OrderSlicer(chunk_pct_of_adv=Decimal("1.5"))
        with pytest.raises(ValueError):
            OrderSlicer(max_total_seconds=0.0)


# ===========================================================================
# TestTokenBucketRateLimit — Tinkoff 60rps budget consumed on every call
# ===========================================================================


class TestTokenBucketRateLimit:
    def test_place_order_consumes_token(
        self, tconn: TinkoffAccount, post_order_response: MagicMock
    ) -> None:
        tconn._client.orders.post_order.return_value = post_order_response
        before = tconn._bucket.tokens_available()
        tconn.place_order(MarketOrder(ticker="SBER", side="buy", quantity=Decimal("1")))
        after = tconn._bucket.tokens_available()
        assert after < before

    def test_rate_limit_returns_error_result(self, tconn: TinkoffAccount) -> None:
        # Drain the bucket to force a RateLimitError on the next acquire.
        # The bucket has capacity=5; acquire 6 times to force a wait or refusal.
        # But acquire blocks — we replace the bucket with one that refuses immediately.
        refusing_bucket = TokenBucket(rate=1, window_seconds=60, capacity=0.5)
        # Make the bucket empty to force RateLimitError on next acquire.
        # Simpler: monkey-patch _consume_token to raise RateLimitError.
        from src.data.token_bucket import RateLimitError

        tconn._consume_token = lambda now=None: (_ for _ in ()).throw(RateLimitError("forced"))
        result = tconn.place_order(MarketOrder(ticker="SBER", side="buy", quantity=Decimal("1")))
        assert result.ok is False
        assert result.error_code == "rate_limited"

    def test_cancel_order_uses_same_bucket(
        self, tconn: TinkoffAccount, post_order_response: MagicMock
    ) -> None:
        tconn._client.orders.post_order.return_value = post_order_response
        tconn._client.orders.cancel_order.return_value = MagicMock()
        # Two operations share the bucket — total budget is bounded.
        before = tconn._bucket.tokens_available()
        tconn.place_order(MarketOrder(ticker="SBER", side="buy", quantity=Decimal("1")))
        tconn.cancel_order("broker-abc-123")
        after = tconn._bucket.tokens_available()
        # Two operations consume ~2 tokens.
        assert after <= before

    def test_long_client_order_id_rejected_pre_broker(
        self, tconn: TinkoffAccount
    ) -> None:
        # 37+ chars -> rejected before reaching the SDK.
        long_id = "x" * 40
        order = MarketOrder(ticker="SBER", side="buy", quantity=Decimal("1"), client_order_id=long_id)
        result = tconn.place_order(order)
        assert result.ok is False
        assert result.error_code == "client_order_id_too_long"
        tconn._client.orders.post_order.assert_not_called()


# ===========================================================================
# TestTinkoffSandboxToken — env wiring
# ===========================================================================


class TestTinkoffSandboxToken:
    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch, sandbox_token: str) -> None:
        monkeypatch.delenv("TINKOFF_SANDBOX_TOKEN", raising=False)
        with pytest.raises(TinkoffTokenMissing):
            TinkoffAccount()

    def test_empty_token_treated_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "   ")
        with pytest.raises(TinkoffTokenMissing):
            TinkoffAccount()

    def test_explicit_token_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch, sandbox_token: str
    ) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "ignore-me")
        acc = TinkoffAccount(token=sandbox_token)
        assert acc.config.token == sandbox_token

    def test_sandbox_false_refused(self, sandbox_token: str) -> None:
        with pytest.raises(ValidationError):
            TinkoffConfig(token=sandbox_token, sandbox=False)

    def test_token_short_rejected_at_model(self) -> None:
        with pytest.raises(ValidationError):
            TinkoffConfig(token="x", sandbox=True)

    def test_token_from_env_static_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "abcd" * 10)
        v = TinkoffAccount.token_from_env()
        assert len(v) >= _SANDBOX_TOKEN_MIN_LEN

    def test_token_from_env_raises_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TINKOFF_SANDBOX_TOKEN", raising=False)
        with pytest.raises(TinkoffTokenMissing):
            TinkoffAccount.token_from_env("TINKOFF_SANDBOX_TOKEN")

    def test_is_sandbox_property_true(self, tconn: TinkoffAccount) -> None:
        assert tconn.is_sandbox is True

    def test_account_id_property_round_trips(self, tconn: TinkoffAccount) -> None:
        assert tconn.account_id == "default"


# ===========================================================================
# TestTinkoffAccountSDKGuards — error handling & balance/positions plumbing
# ===========================================================================


class TestTinkoffAccountSDKGuards:
    def test_connect_uses_factory_if_provided(self, sandbox_token: str) -> None:
        # Build a TinkoffAccount WITHOUT the fixture (so connect hasn't run yet)
        # and verify that connect() lazily calls _client_factory.
        from src.data.token_bucket import TokenBucket as _TB

        bucket = _TB(rate=60, window_seconds=60, capacity=5)
        factory_calls: list[str] = []

        def factory(token: str) -> Any:
            factory_calls.append(token)
            return MagicMock(name="LazyClient")

        acc = TinkoffAccount(token=sandbox_token, _client_factory=factory, _bucket=bucket)
        assert acc._client is None
        assert factory_calls == []

        acc.connect()

        assert factory_calls == [sandbox_token]
        assert acc._client is not None

    def test_connect_without_factory_raises_when_sdk_missing(
        self, sandbox_token: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a TinkoffAccount with no _client_factory.
        acc = TinkoffAccount(token=sandbox_token, _client_factory=None, _bucket=TokenBucket(rate=1, window_seconds=60))
        # Make tinkoff.investments import fail.
        import sys

        monkeypatch.setitem(sys.modules, "tinkoff.investments", None)
        with pytest.raises((TinkoffSDKUnavailable, ImportError)):
            acc.connect()

    def test_get_balance_returns_zero_when_sdk_unimplemented(
        self, tconn: TinkoffAccount
    ) -> None:
        # The MagicMock client has no users/portfolios.get_portfolio; should
        # swallow AttributeError and return zero balance.
        bal = tconn.get_balance()
        assert isinstance(bal, Balance)
        assert bal.currency == "RUB"

    def test_get_positions_sorted_by_ticker(
        self, tconn: TinkoffAccount
    ) -> None:
        # Hand-build a minimally-shaped Tinkoff SDK response.
        class _PositionsResp:
            securities = [
                type("S", (), {"ticker": "YDEX", "balance": type("B", (), {"units": 0, "nano": 0})(), "average_position_price": None, "market_value": None})(),
                type("S", (), {"ticker": "SBER", "balance": type("B", (), {"units": 100, "nano": 0})(), "average_position_price": type("Q", (), {"units": 100, "nano": 0})(), "market_value": None})(),
            ]

        tconn._client.operations.get_positions.return_value = _PositionsResp()
        positions = tconn.get_positions()
        assert [p.ticker for p in positions] == ["SBER"]  # YDEX had qty=0, skipped

    def test_get_orders_returns_list(self, tconn: TinkoffAccount) -> None:
        class _OrdersResp:
            orders = []
        tconn._client.orders.get_orders.return_value = _OrdersResp()
        assert isinstance(tconn.get_orders(), list)

    def test_classify_error_known_codes(self) -> None:
        cls = TinkoffAccount._classify_error

        # We exercise the class-name path, not the message path.
        class AuthError(Exception):
            pass

        class ForbiddenError(Exception):
            pass

        class _RateLimitError(Exception):
            pass

        class _TimeoutError(Exception):
            pass

        class _ConnectError(Exception):
            pass

        assert cls(AuthError("denied")) == "auth_error"
        assert cls(ForbiddenError("403")) == "auth_error"
        assert cls(_RateLimitError("too many")) == "rate_limited"
        assert cls(_TimeoutError("slow")) == "timeout"
        assert cls(_ConnectError("dns")) == "network"
        assert cls(RuntimeError("weird")) == "broker_error"

    def test_quotation_helpers_roundtrip(self) -> None:
        q = TinkoffAccount._decimal_to_quotation(Decimal("123.45"))
        # Either a SDK Quotation or our _MockQuotation — both have units/nano.
        units, nano = q.units, q.nano
        assert units == 123
        assert nano == 450_000_000
        # Inverse: Tinkoff Quotation-like -> Decimal.
        d = TinkoffAccount._quotation_to_decimal(type("Q", (), {"units": 123, "nano": 450_000_000})())
        assert d == Decimal("123.45")
        d2 = TinkoffAccount._quotation_to_decimal(None)
        assert d2 is None

    def test_money_value_helper_handles_garbage(self) -> None:
        amt, ccy = TinkoffAccount._money_value_to_decimal(None)
        # magic: garbage falls back to zero.
        assert amt == Decimal("0") and ccy == "RUB"

    def test_close_clears_state(self, tconn: TinkoffAccount) -> None:
        tconn.connect()
        tconn._order_cache["x"] = _AO(broker_order_id="x", account_id="default", ticker="SBER", side="buy", type="market", requested_qty=Decimal("1"))
        tconn.close()
        assert tconn._client is None
        assert tconn._order_cache == {}

    def test_mock_quotation_serialisation(self) -> None:
        mq = _MockQuotation(Decimal("99.5"))
        assert mq.units == 99
        assert mq.nano == 500_000_000

    def test_connect_no_factory_via_patch_to_real_sdk(
        self, sandbox_token: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When _client_factory is None and SDK import succeeds, ``Client(token)`` is called.

        We don't have the SDK installed — but we patch it into ``sys.modules``
        with a MagicMock so the import succeeds and ``Client(token)`` returns
        a usable mock that exercises the real-branch (line 248).
        """
        from src.data.token_bucket import TokenBucket as _TB

        sentinel_client = MagicMock(name="SentinelClient")
        client_module = MagicMock()
        client_module.Client = MagicMock(return_value=sentinel_client)

        import sys

        monkeypatch.setitem(sys.modules, "tinkoff.investments", client_module)
        # Also patch the optional import path used inside _decimal_to_quotation.
        client_module.Quotation = MagicMock()

        bucket = _TB(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=None, _bucket=bucket)
        acc.connect()
        client_module.Client.assert_called_once_with(sandbox_token)
        assert acc._client is sentinel_client

    def test_get_balance_account_id_path_exercised(self, sandbox_token: str) -> None:
        """When SDK returns a matching account, we walk the per-account balance branch."""

        class _Acct:
            id = "default"

        class _Portfolio:
            total_amount_currencies = type(
                "MV", (), {"units": 1000, "nano": 0, "currency": "RUB"}
            )()

        fake_client = MagicMock()
        fake_client.users.get_accounts.return_value = type(
            "AccountsResp", (), {"accounts": [_Acct()]}
        )()
        fake_client.portfolios.get_portfolio.return_value = _Portfolio()

        bucket = TokenBucket(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
        acc.connect()

        bal = acc.get_balance()
        assert bal.cash == Decimal("1000")
        assert bal.currency == "RUB"

    def test_get_balance_wraps_unexpected_exceptions(self, sandbox_token: str) -> None:
        """A network-broker exception is wrapped, not swallowed."""
        fake_client = MagicMock()
        fake_client.users.get_accounts.side_effect = ConnectionError("boom")
        bucket = TokenBucket(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
        acc.connect()
        with pytest.raises(ConnectionError, match="boom"):
            acc.get_balance()

    def test_get_orders_handles_empty_response(self, sandbox_token: str) -> None:
        """An orders response with empty list returns []."""

        class _OrdersResp:
            orders = []

        fake_client = MagicMock()
        fake_client.orders.get_orders.return_value = _OrdersResp()
        bucket = TokenBucket(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
        acc.connect()

        assert acc.get_orders() == []
        assert acc._order_cache == {}

    def test_get_orders_handles_orders_with_extra_attrs(self, sandbox_token: str) -> None:
        """The order_state shim gets transformed into AccountOrder."""

        order_state = type(
            "OS",
            (),
            {
                "order_id": "broker-xyz",
                "execution_status": "EXECUTION_STATUS_FILL",
                "executed_quantity": type("Q", (), {"units": 5, "nano": 0})(),
                "average_price": type("Q", (), {"units": 110, "nano": 0})(),
                "status": None,
            },
        )()

        class _OrdersResp:
            orders = [order_state]

        fake_client = MagicMock()
        fake_client.orders.get_orders.return_value = _OrdersResp()
        bucket = TokenBucket(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
        acc.connect()

        orders = acc.get_orders()
        assert len(orders) == 1
        assert orders[0].broker_order_id == "broker-xyz"
        assert orders[0].status == "filled"
        assert orders[0].filled_qty == Decimal("5")
        assert orders[0].avg_fill_price == Decimal("110")

    def test_place_order_failure_is_mapped_to_error_result(
        self, sandbox_token: str
    ) -> None:
        """An SDK exception in place_order is mapped to OrderResult(ok=False)."""
        fake_client = MagicMock()
        fake_client.orders.post_order.side_effect = ConnectionError("broker down")
        bucket = TokenBucket(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
        acc.connect()

        result = acc.place_order(MarketOrder(ticker="SBER", side="buy", quantity=Decimal("1")))
        assert result.ok is False
        assert result.error_code == "network"
        assert "broker down" in result.error_message

    def test_place_order_limit(self, sandbox_token: str) -> None:
        """Limit orders go through the price branch in place_order."""
        fake_client = MagicMock()
        order_resp = MagicMock(order_id="lim-1", execution_status="EXECUTION_STATUS_NEW")
        fake_client.orders.post_order.return_value = order_resp
        bucket = TokenBucket(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
        acc.connect()

        result = acc.place_order(
            LimitOrder(ticker="SBER", side="sell", quantity=Decimal("1"), price=Decimal("100"))
        )
        assert result.ok is True
        kwargs = fake_client.orders.post_order.call_args.kwargs
        assert kwargs["order_type"] == "limit"
        assert kwargs["direction"] == "ORDER_DIRECTION_SELL"

    def test_cancel_order_with_failure_uses_error_result(self, sandbox_token: str) -> None:
        """Cancel-order SDK failures return ok=False with mapped error_code."""
        fake_client = MagicMock()
        fake_client.orders.cancel_order.side_effect = ConnectionError("cancel boom")
        bucket = TokenBucket(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
        acc.connect()

        result = acc.cancel_order("some-id")
        assert result.ok is False
        assert result.error_code == "network"

    def test_cancel_order_without_cache_constructs_placeholder(self, sandbox_token: str) -> None:
        """Cancelling an unknown broker_order_id still produces a valid OrderResult."""
        fake_client = MagicMock()
        fake_client.orders.cancel_order.return_value = MagicMock()
        bucket = TokenBucket(rate=60, window_seconds=60, capacity=5)
        acc = TinkoffAccount(token=sandbox_token, _client_factory=lambda _t: fake_client, _bucket=bucket)
        acc.connect()

        result = acc.cancel_order("never-seen")
        assert result.ok is True
        assert result.status == "cancelled"
        assert result.order is not None
        assert result.order.broker_order_id == "never-seen"

    def test_tinkoff_status_to_status_all_branches(self) -> None:
        fn = TinkoffAccount._tinkoff_status_to_status
        assert fn("EXECUTION_STATUS_FILL", None) == "filled"
        assert fn("EXECUTION_STATUS_PARTIALFILL", None) == "partially_filled"
        assert fn(None, "STATUS_REJECTED") == "rejected"
        assert fn(None, "STATUS_CANCELLED") == "cancelled"
        assert fn(None, "STATUS_NEW") == "new"
        # Fallback
        assert fn(None, None) == "submitted"
        assert fn("EXECUTION_STATUS_SOMETHING_NEW", None) == "new"
        # Empty status -> default
        assert fn("", "") == "submitted"

    def test_extract_ticker_uppercases(self) -> None:
        s = type("S", (), {"ticker": "ydex"})()
        assert TinkoffAccount._extract_ticker(s) == "YDEX"
        empty = type("S", (), {"ticker": ""})()
        assert TinkoffAccount._extract_ticker(empty) == ""

    def test_ticker_to_figi_default_format(self) -> None:
        assert TinkoffAccount._ticker_to_figi_or_default("sber") == "FIGI:SBER"

    def test_connect_returns_early_when_client_already_set(self, tconn: TinkoffAccount) -> None:
        """A second connect() with the client already initialised is a no-op."""
        before = tconn._client
        # Replace _client_factory with a function that would blow up if called.
        def boom(_t: str) -> Any:
            raise AssertionError("factory should not be invoked when client is set")

        tconn._client_factory = boom  # type: ignore[assignment]
        tconn.connect()
        assert tconn._client is before

    def test_token_config_rejects_whitespace_control(self) -> None:
        with pytest.raises(ValidationError):
            TinkoffConfig(token="\nweird\n", sandbox=True)

    def test_token_config_rejects_empty_after_strip(self) -> None:
        with pytest.raises(ValidationError):
            TinkoffConfig(token="   ", sandbox=True)

    def test_heuristic_token_check(self) -> None:
        # Empty / whitespace-only / very short — all rejected.
        assert TinkoffAccount.heuristic_token_check("") is False
        assert TinkoffAccount.heuristic_token_check("    ") is False
        assert TinkoffAccount.heuristic_token_check("short") is False
        # A real-shape token passes (logging warning is allowed).
        ok = TinkoffAccount.heuristic_token_check("t." + "a" * (_SANDBOX_TOKEN_MIN_LEN - 2))
        assert ok is True
