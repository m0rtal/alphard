"""Integration: RiskGate + Broker + Data Agent.

OrderFlow is the canonical entry point for placing an order:
1. Universe filter (Phase 2)
2. Live quote fetch (issue #166 — never substitute a placeholder price)
3. RiskGate.evaluate() — only allowed=True proceeds
4. OrderSlicer.slice() — split into 5% ADV chunks
5. TinkoffAccount.place_order() — submit each slice
6. Audit log to Postgres (Phase 3.1)

Issue #166: the previous implementation constructed the RiskGate
``TradeIntent`` with ``price=Decimal("1")`` as a "proxy; real fetch from
market data". The real ``RiskGate.evaluate`` (src/risk/gate.py:274) adds
a guard that refuses any intent with ``price == Decimal("1")`` AND
``quantity > Decimal("1")`` — a defence-in-depth against the historical
issue #11 placeholder exploit. As a result the previous ``OrderFlow``
silently rejected 100% of real-market orders. The fix is structural:
``OrderFlow`` now requires a ``quote_provider`` (callable taking the
ticker and returning the live price as ``Decimal``) and refuses the order
with a clear ``QUOTE_UNAVAILABLE`` violation if no real price is
available. The broker's existing ``_fetch_live_quote_price`` method is
the canonical production quote source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from src.broker.account import BrokerAccount, PortfolioSnapshot
from src.broker.orders import (
    MarketOrder,
    OrderSide,
    OrderStatus,
)
from src.broker.slicer import OrderSlicer
from src.broker.tinkoff_account import BrokerError

logger = logging.getLogger("alphard.broker.flow")

# Type alias for the live-quote provider. Signature:
#   quote_provider(symbol: str) -> Decimal  (price > 0)
# Raise any exception to signal "quote unavailable". Callers (e.g.
# TinkoffAccount) wrap Tinkoff market_data.get_last_prices here.
QuoteProvider = Callable[[str], Decimal]


@dataclass
class OrderFlowResult:
    intent_symbol: str
    side: str
    quantity: Decimal
    decision_violations: tuple[str, ...]
    slice_count: int
    submitted: list[OrderStatus]
    final_status: OrderStatus
    # Issue #168: per-outcome counts so callers can disambiguate
    # ``final_status == SUBMITTED`` (legitimate partial fill) from
    # ``final_status == SUBMITTED`` masquerading as a silent failure
    # (slicer raised → submitted=[], or all slices rejected by broker).
    # Without these, the audit log conflates three distinct outcomes.
    filled_count: int = 0
    rejected_count: int = 0


class OrderFlow:
    """End-to-end order submission with full safety guarantees."""

    def __init__(
        self,
        broker: BrokerAccount,
        risk_gate: Any,  # src.risk.gate.RiskGate (typed Any to satisfy --strict)
        quote_provider: QuoteProvider,
        universe_filter: Callable[[str], bool] | None = None,
        peak_equity_provider: Callable[[], Decimal] | None = None,
        daily_pnl_provider: Callable[[], Decimal] | None = None,
    ):
        """
        Args:
            broker: Concrete broker (TinkoffAccount, future BCSAccount, etc.).
            risk_gate: RiskGate instance. Cannot be None — fail-safe contract.
            quote_provider: Callable returning the live ``Decimal`` price for
                a ticker. MUST raise on failure — ``OrderFlow`` will refuse
                the order with a ``QUOTE_UNAVAILABLE`` violation rather than
                substitute a placeholder (issue #166).
            universe_filter: Optional allow-list. Symbols for which the
                filter returns False are short-circuited with
                ``UNIVERSE_BLOCKED``.
            peak_equity_provider: Optional callable returning the persistent
                peak-equity high-water mark (issue #195). Without a
                persistent peak, ``PortfolioState.peak_equity`` defaults
                to the current NAV on every call, which makes
                ``_check_drawdown`` in ``src/risk/gate.py`` always report
                0%% drawdown — the RISK_DD guard never trips via the
                OrderFlow path. Pass a callable (e.g.
                ``lambda: tinker._peak_equity`` or a dedicated disk-backed
                tracker) to wire in real peak tracking. ``None`` keeps the
                legacy behaviour and logs a one-shot WARNING so the gap
                is visible to operators.
            daily_pnl_provider: Optional callable returning the current
                realised + unrealised daily P&L as ``Decimal`` (issue
                #197). Without a provider, ``PortfolioState.daily_pnl``
                defaults to ``Decimal("0")`` on every call, which makes
                ``_check_daily_loss`` short-circuit (``daily_pnl >= 0``
                early-return) — the daily-loss kill-switch is silently a
                no-op via the OrderFlow path. Pass a callable (e.g.
                ``lambda: tinker._fetch_daily_pnl(nav)`` or a dedicated
                disk-backed tracker) to wire in real daily-pnl
                tracking. ``None`` keeps the legacy behaviour and logs a
                one-shot WARNING so the gap is visible to operators.

        Raises:
            TypeError: if ``quote_provider`` is None. We require an explicit
                quote source rather than accepting a default that could
                silently degrade to a placeholder.
        """
        if quote_provider is None:
            raise TypeError(
                "OrderFlow requires a quote_provider (issue #166). "
                "Pass a callable (e.g. TinkoffAccount._fetch_live_quote_price) "
                "that returns a real Decimal price for the ticker; "
                "raising on failure."
            )
        self._broker = broker
        self._risk_gate = risk_gate
        self._quote_provider = quote_provider
        self._universe_filter = universe_filter
        self._peak_equity_provider = peak_equity_provider
        # Issue #197: same defensive pattern as peak_equity_provider
        # above. The provider returns ``Decimal("0")`` if no real daily
        # P&L is wired up, which makes _check_daily_loss short-circuit
        # rather than trip on stale data.
        self._daily_pnl_provider = daily_pnl_provider
        self._warned_missing_peak: bool = False
        # Issue #197: one-shot warning flag for missing daily_pnl
        # provider. Logged on the first submit_market call so a busy
        # session doesn't flood the log.
        self._warned_missing_daily_pnl: bool = False

    def submit_market(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        portfolio: PortfolioSnapshot,
    ) -> OrderFlowResult:
        # 1. Universe filter
        if self._universe_filter and not self._universe_filter(symbol):
            logger.warning("Symbol %s blocked by universe filter", symbol)
            return OrderFlowResult(
                intent_symbol=symbol,
                side=side.value,
                quantity=quantity,
                decision_violations=("UNIVERSE_BLOCKED",),
                slice_count=0,
                submitted=[],
                final_status=OrderStatus.REJECTED,
            )

        # 2. Live quote (issue #166). Refuse the order if the quote cannot
        # be fetched. We do NOT fall back to a placeholder — that is
        # exactly the bug we fixed at the broker layer (issue #11) and
        # the bug that broke this integration before issue #166.
        try:
            price = self._quote_provider(symbol)
        except Exception as exc:
            logger.error(
                "QUOTE_UNAVAILABLE for %s: %s — refusing order (issue #166, "
                "fail-safe: never substitute a placeholder price)",
                symbol,
                exc,
            )
            return OrderFlowResult(
                intent_symbol=symbol,
                side=side.value,
                quantity=quantity,
                decision_violations=("QUOTE_UNAVAILABLE",),
                slice_count=0,
                submitted=[],
                final_status=OrderStatus.REJECTED,
            )
        if not isinstance(price, Decimal) or price <= Decimal("0"):
            logger.error(
                "QUOTE_INVALID for %s: quote_provider returned %r — refusing "
                "order (issue #166, fail-safe: never substitute a placeholder)",
                symbol,
                price,
            )
            return OrderFlowResult(
                intent_symbol=symbol,
                side=side.value,
                quantity=quantity,
                decision_violations=("QUOTE_INVALID",),
                slice_count=0,
                submitted=[],
                final_status=OrderStatus.REJECTED,
            )

        # 3. RiskGate
        from src.risk.gate import RiskDecision, TradeIntent

        # Issue #195: use the INSTANCE method so peak_equity is pulled
        # from self._peak_equity_provider (when configured) rather than
        # always equal to current NAV. The static _portfolio_to_state is
        # kept as a back-compat alias for tests that call it directly.
        state = self._portfolio_to_state_impl(portfolio)
        intent = TradeIntent(
            symbol=symbol.upper(),
            # BUGFIX (C-4): pass side through unchanged. The previous expression
            # silently inverted SELL → BUY.
            side=side.value.lower(),
            quantity=quantity,
            price=price,
        )
        decision: RiskDecision = self._risk_gate.evaluate(intent, state)

        if not decision.allowed:
            logger.info("RiskGate blocked %s: %s", symbol, decision.violations)
            return OrderFlowResult(
                intent_symbol=symbol,
                side=side.value,
                quantity=quantity,
                decision_violations=decision.violations,
                slice_count=0,
                submitted=[],
                final_status=OrderStatus.REJECTED,
            )

        # 4. Slice
        adv_shares = max(quantity * Decimal("20"), Decimal("100"))
        try:
            slicer = OrderSlicer(adv_shares=adv_shares, parent_qty=quantity)
            slices = slicer.slice()
        except ValueError:
            slices = []

        # 5. Submit
        # Issue #170: the previous ``except Exception`` blanket caught
        # programming errors (TypeError / KeyError / AttributeError) and
        # wrote them to the audit log as ``OrderStatus.REJECTED`` — the
        # same code used for a legitimate broker rejection. Operators
        # could not tell apart "broker refused" (business) from "our code
        # blew up" (system). The catch now distinguishes:
        #   * BrokerError → technical broker failure. Logged at WARNING
        #     (not ERROR — would flood alerts during a Tinkoff outage).
        #     Mapped to REJECTED so the per-slice aggregate stays
        #     consistent with case 1.
        #   * Exception → programming error. Re-raised so it hits the
        #     supervisor / error tracker. NEVER written to the audit
        #     log as REJECTED.
        submitted: list[OrderStatus] = []
        for i, slc in enumerate(slices):
            order = MarketOrder(ticker=symbol, side=side, quantity=slc.quantity)
            try:
                status = self._broker.place_order(order)
            except BrokerError as e:
                logger.warning("Slice %d broker failure (mapped to REJECTED): %s", i, e)
                status = OrderStatus.REJECTED
            submitted.append(status)

        # Issue #168: three-tier final_status. The pre-fix logic lumped
        # every non-fully-FILLED outcome into SUBMITTED, conflating
        # "partial fill" with "no slice submitted at all" (slicer
        # raised → submitted == []) and "broker rejected every slice"
        # (submitted == [REJECTED, ...]). The audit log treated both as
        # a real SUBMITTED run, misleading operators and flooding
        # monitoring alerts. New contract:
        #   * submitted == []             → REJECTED (internal failure;
        #                                    nothing reached the broker)
        #   * all submitted == REJECTED   → REJECTED (broker refused all)
        #   * all submitted == FILLED     → FILLED   (clean execution)
        #   * mixed FILLED + non-FILLED   → SUBMITTED (legitimate partial
        #                                    fill, broker is still working
        #                                    on the order)
        filled_count = sum(1 for s in submitted if s == OrderStatus.FILLED)
        rejected_count = sum(1 for s in submitted if s == OrderStatus.REJECTED)
        if not submitted:
            final = OrderStatus.REJECTED
        elif rejected_count == len(submitted):
            final = OrderStatus.REJECTED
        elif filled_count == len(submitted):
            final = OrderStatus.FILLED
        else:
            final = OrderStatus.SUBMITTED

        return OrderFlowResult(
            intent_symbol=symbol,
            side=side.value,
            quantity=quantity,
            decision_violations=decision.violations,
            slice_count=len(slices),
            submitted=submitted,
            final_status=final,
            filled_count=filled_count,
            rejected_count=rejected_count,
        )

    @staticmethod
    def _portfolio_to_state(portfolio: PortfolioSnapshot) -> Any:
        from src.risk.gate import PortfolioState, Position as RiskPosition

        positions = [
            RiskPosition(
                symbol=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
            )
            for p in portfolio.positions
        ]
        # Issue #180 + #191: `portfolio.cash` is the full NAV, not free cash — see
        # `src/broker/tinkoff_account.py:381-410`, where TinkoffAccount
        # fills `PortfolioSnapshot.cash = total_amount_currencies` (the
        # Tinkoff SDK field that reports NAV = cash + positions at mark).
        #
        # Issue #180 fix: use `portfolio.cash` as `total_equity` directly.
        # Do not add `sum(p.quantity * p.avg_price)` on top — that would
        # double-count positions and inflate equity by the position book
        # size, silently approving positions up to 2x the configured
        # position limit (issue #11 class).
        #
        # Issue #191 fix: derive `cash` as **free cash** (NAV minus the
        # value of open positions at avg_price). The previous code passed
        # NAV straight through into `PortfolioState.cash`, which conflates
        # NAV with free cash. The bug is latent today (no `_check_*` in
        # `src/risk/gate.py` reads `state.cash`), but any future check that
        # treats `state.cash` as tradeable cash (cash-adequacy gate,
        # buy-in-cash cap, audit log) would silently over-approve by
        # treating NAV as the actually-tradeable amount. Use the same
        # `quantity * avg_price` formula already used by
        # `Position.market_value` at `src/risk/gate.py:135-137` so the
        # math stays consistent with `_check_sector_exposure`.
        total = portfolio.cash
        positions_value = sum(
            (p.quantity * p.avg_price for p in portfolio.positions),
            Decimal("0"),
        )
        # Free cash cannot be negative by the Tinkoff contract (positions
        # are always ≤ NAV), but a synthetic snapshot with positions
        # exceeding NAV would produce a negative value. PortfolioState.cash
        # is `Field(..., ge=Decimal("0"))`, so clamp to zero rather than
        # letting pydantic raise ValidationError — the clamp also makes
        # the audit log stable.
        free_cash = portfolio.cash - positions_value
        if free_cash < Decimal("0"):
            free_cash = Decimal("0")
        return PortfolioState(
            total_equity=total,
            cash=free_cash,
            positions=positions,
            peak_equity=total,
        )

    # ------------------------------------------------------------------
    # Issue #195: peak_equity = current NAV on every call makes
    # _check_drawdown always report 0%. Instance method below reads from
    # self._peak_equity_provider when configured so the persistent peak
    # semantics from TinkoffAccount._peak_equity carry over to the
    # OrderFlow code path.
    # ------------------------------------------------------------------

    def _portfolio_to_state_impl(self, portfolio: PortfolioSnapshot) -> Any:
        """Build ``PortfolioState`` with peak_equity + daily_pnl from providers.

        Issue #195: ``_portfolio_to_state`` (static) hard-codes
        ``peak_equity=total_equity`` so the ``RISK_DD`` guard in
        ``src/risk/gate.py:452-474`` reports 0% drawdown forever. This
        instance method pulls the high-water mark from
        ``self._peak_equity_provider`` so real drawdown tracking works
        through ``OrderFlow.submit_market``.

        Issue #197: same fix for ``daily_pnl``. The static helper
        leaves it at the pydantic default (``Decimal("0")``) so the
        ``_check_daily_loss`` short-circuit (``daily_pnl >= 0``) trips
        on every call — the daily-loss kill-switch is silently a no-op
        via OrderFlow. Pull from ``self._daily_pnl_provider`` when
        configured; otherwise fall back to 0 with a one-shot WARNING.

        Backwards-compat: when ``self._peak_equity_provider is None``
        (legacy call sites), we fall back to ``peak_equity=total_equity``
        and emit a one-shot WARNING — the same behaviour as the static
        method, but visible in logs.
        """
        from src.risk.gate import PortfolioState, Position as RiskPosition

        positions = [
            RiskPosition(
                symbol=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
            )
            for p in portfolio.positions
        ]
        total = portfolio.cash
        positions_value = sum(
            (p.quantity * p.avg_price for p in portfolio.positions),
            Decimal("0"),
        )
        free_cash = portfolio.cash - positions_value
        if free_cash < Decimal("0"):
            free_cash = Decimal("0")

        # Resolve peak: provider if configured, else legacy fallback.
        if self._peak_equity_provider is not None:
            try:
                peak = self._peak_equity_provider()
            except Exception as exc:  # noqa: BLE001 — provider errors must never break the order path
                logger.warning(
                    "OrderFlow._portfolio_to_state_impl: peak_equity_provider "
                    "raised %s: %s — falling back to peak=total (issue #195)",
                    type(exc).__name__,
                    exc,
                )
                # Disable for the rest of the process — a flapping
                # provider would otherwise spam the log every submit_market.
                self._peak_equity_provider = None
                peak = total
        else:
            peak = total
            if not self._warned_missing_peak:
                logger.warning(
                    "OrderFlow._portfolio_to_state_impl: peak_equity_provider "
                    "not configured — RISK_DD guard will report 0%% drawdown "
                    "(issue #195). Pass a persistent peak tracker to enable "
                    "real drawdown-based kill-switching."
                )
                self._warned_missing_peak = True

        # Issue #197: same pattern for daily_pnl. Provider if
        # configured, else legacy fallback (0) with one-shot WARNING.
        if self._daily_pnl_provider is not None:
            try:
                daily_pnl = self._daily_pnl_provider()
            except Exception as exc:  # noqa: BLE001 — provider errors must never break the order path
                logger.warning(
                    "OrderFlow._portfolio_to_state_impl: daily_pnl_provider "
                    "raised %s: %s — falling back to daily_pnl=0 (issue #197)",
                    type(exc).__name__,
                    exc,
                )
                # Disable for the rest of the process — a flapping
                # provider would otherwise spam the log every submit_market.
                self._daily_pnl_provider = None
                daily_pnl = Decimal("0")
        else:
            daily_pnl = Decimal("0")
            if not self._warned_missing_daily_pnl:
                logger.warning(
                    "OrderFlow._portfolio_to_state_impl: daily_pnl_provider "
                    "not configured — RISK_DAILY_LOSS guard will short-circuit "
                    "to 0%% (issue #197). Pass a persistent daily-pnl tracker "
                    "to enable real daily-loss-based kill-switching."
                )
                self._warned_missing_daily_pnl = True

        # The PortfolioState validator (src/risk/gate.py:168-171) requires
        # ``peak_equity >= total_equity``. If the persistent peak is BELOW
        # current NAV (cold start, deleted peak file, NAV jump), bump it
        # to current NAV — a "high-water mark" by definition only goes up.
        if peak < total:
            peak = total

        return PortfolioState(
            total_equity=total,
            cash=free_cash,
            positions=positions,
            peak_equity=peak,
            daily_pnl=daily_pnl,
        )
