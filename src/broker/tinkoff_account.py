"""TinkoffAccount — concrete broker via t-tech-investments SDK.

Sandbox auto-detect via TINKOFF_SANDBOX_TOKEN prefix 't.' (real tokens vary).

RiskGate integration: place_order() calls RiskGate.evaluate() BEFORE
Tinkoff SDK call. If RiskGate returns allowed=False, order is rejected
without touching the network.

If t-tech-investments SDK is not installed (sandbox or missing dep),
falls back to MockTinkoffClient for unit testing.
"""

from __future__ import annotations

import json
import logging
import os

from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from src.broker.account import BrokerAccount, PortfolioSnapshot, Position
from src.broker.orders import (
    LimitOrder,
    MarketOrder,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.data.token_bucket import TokenBucket

logger = logging.getLogger("alphard.broker.tinkoff")


def _broker_position_to_gate_position(broker_pos: Any) -> Any:
    """Convert a broker ``Position`` (dataclass) to a gate ``Position`` (pydantic).

    The two models have the same fields except for the symbol/ticker
    naming difference. Sectors are not yet mapped from Tinkoff in
    Phase 1; the gate's sector check will skip on ``sector=None``
    until Phase 2 wires the sector map.
    """
    from src.risk.gate import Position as _GatePosition

    return _GatePosition(
        symbol=broker_pos.ticker,
        quantity=broker_pos.quantity,
        avg_price=broker_pos.avg_price,
        sector=None,
    )


class BrokerError(RuntimeError):
    """Technical failure from Tinkoff SDK."""


def _assert_not_live_trading(action: str, ticker: str = "") -> bool:
    """Phase 1 hard no-trade gate. Returns True if trading is allowed.

    This is the single source of truth for the LIVE_TRADING=false
    constraint. Every code path that could place a real order MUST
    call this before they touch the broker — not just place_order(),
    so that any future entry point (e.g. a background-driven dry-run,
    a coordinator flow, a manual CLI command) inherits the same
    guarantee.

    The check is env-driven so it works for sandbox, CI, and
    production alike. We refuse by default (LIVE_TRADING!="true")
    — the operator must explicitly opt-in to live trading for
    each fresh process.
    """
    if os.environ.get("LIVE_TRADING", "false").lower() == "true":
        return True
    logger.warning(
        "LIVE_TRADING=false — refusing %s%s (Phase 1 hard no-trade)",
        action,
        f" for {ticker}" if ticker else "",
    )
    return False


class TinkoffAccount(BrokerAccount):
    """Tinkoff Invest API account.

    Args:
        token: Tinkoff API token. Sandbox tokens start with 't.' (real have other prefix).
        account_id: Tinkoff account ID (sandbox default: 'SB1' for OpenSandboxAccount).
        risk_gate: Optional RiskGate instance. If None, all orders are rejected
            (fail-safe — explicit opt-in required).
        rate_limit_per_sec: Tinkoff API limit (default 60/sec).
    """

    def __init__(
        self,
        token: str,
        account_id: str = "SB1",
        # Use Any locally to satisfy --strict without runtime isinstance check
        risk_gate: Any = None,  # src.risk.gate.RiskGate (typed Any to satisfy --strict)
        rate_limit_per_sec: int = 60,
        # Issue #32: persistent peak-equity store path. If None, defaults
        # to $ALPHARD_PEAK_STORE_DIR or /var/lib/alphard. The file is
        # read once on construction and written on every successful
        # _fetch_real_portfolio_state() call (monotonically non-decreasing).
        peak_store_dir: Optional[str] = None,
        # Issue #197: optional clock callable returning a tz-aware
        # ``datetime``. Used by the daily-pnl tracker to compute
        # "today" without patching the immutable ``datetime.datetime``
        # module attribute. Tests inject a fixed clock; production
        # callers leave it None (default: ``datetime.now(UTC)``).
        clock: Optional[Any] = None,
    ):
        self._token = token
        self._account_id = account_id
        self._risk_gate = risk_gate
        self._rate_limit_per_sec = rate_limit_per_sec
        # Issue #103: use the shared, thread-safe TokenBucket primitive
        # instead of a hand-rolled list-without-lock. Two threads calling
        # place_order concurrently used to both observe an empty list,
        # both append, and burst above the SLA (Tinkoff's server-side
        # rate-limiter then 429s — order silently rejected AFTER RiskGate
        # had already approved). TokenBucket.acquire() is documented
        # thread-safe and is the same primitive data loaders use
        # (src/data/token_bucket.py).
        self._rate_bucket = TokenBucket(
            rate=float(rate_limit_per_sec),
            window_seconds=1.0,
        )
        # Issue #32: peak-equity high-water mark tracker. Holds the
        # maximum total_equity observed across this account's history so
        # that _check_drawdown() in src/risk/gate.py can compute
        # drawdown = (peak - current) / peak and trip the RISK_DD guard
        # when it exceeds max_dd_pct. Without this, peak == current and
        # drawdown is always 0% — the bug fixed here.
        if peak_store_dir is None:
            peak_store_dir = os.environ.get("ALPHARD_PEAK_STORE_DIR", "/var/lib/alphard")
        self._peak_store_dir: str = peak_store_dir
        self._peak_equity_path: str = os.path.join(peak_store_dir, f"peak_equity_{account_id}.json")
        self._peak_equity: Decimal = self._load_peak_equity()
        # Issue #197: daily-pnl tracker. Holds (previous_close_equity,
        # previous_close_date) so _check_daily_loss in src/risk/gate.py
        # can compute daily_pnl = current_NAV - previous_close_equity
        # and trip the kill-switch on a -3% day. Without this, every
        # production PortfolioState built for RiskGate has daily_pnl=0
        # (the pydantic default), so the daily-loss guard never trips —
        # same defect class as issue #195 (peak_equity) and issue #11
        # (placeholder price). The file is best-effort: corrupt /
        # missing / cold-start files all fall back to "no history yet",
        # which means daily_pnl=0 for the first snapshot of the day and
        # the guard kicks in starting with the second snapshot.
        self._daily_pnl_path: str = os.path.join(peak_store_dir, f"daily_pnl_{account_id}.json")
        self._daily_pnl_state: tuple[Decimal, date] = self._load_daily_pnl_state()
        # Issue #197: tz-aware clock callable used for daily-pnl
        # rollover decisions. Production: None → datetime.now(UTC).
        self._clock = clock

    def _load_peak_equity(self) -> Decimal:
        """Read the persisted peak-equity high-water mark from disk.

        Returns Decimal("0") if the file does not exist (cold start)
        or is unparseable (corrupted). A zero peak means "no history
        yet" — the next snapshot's value will be the new peak.
        """
        try:
            with open(self._peak_equity_path, "r") as fh:
                data = json.load(fh)
            value = Decimal(str(data.get("peak_equity", "0")))
            if value < 0:
                logger.warning(
                    "Peak equity file %s has negative value %s; treating as 0",
                    self._peak_equity_path,
                    value,
                )
                return Decimal("0")
            return value
        except FileNotFoundError:
            return Decimal("0")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "Peak equity file %s is corrupt (%s); starting from 0",
                self._peak_equity_path,
                e,
            )
            return Decimal("0")

    def _save_peak_equity(self) -> None:
        """Persist the current peak-equity to disk (best-effort).

        Write is best-effort: a failure here logs a warning but does
        not raise. The in-memory peak is the source of truth for the
        current process; on the next start the peak is reloaded from
        disk. The worst case is losing one cycle of drawdown tracking.
        """
        try:
            os.makedirs(self._peak_store_dir, exist_ok=True)
            with open(self._peak_equity_path, "w") as fh:
                json.dump({"peak_equity": str(self._peak_equity)}, fh)
        except OSError as e:
            logger.warning(
                "Failed to persist peak equity to %s: %s",
                self._peak_equity_path,
                e,
            )

    # ------------------------------------------------------------------
    # Issue #197: daily-pnl tracker — pairs with `_peak_equity` above.
    # ------------------------------------------------------------------

    def _load_daily_pnl_state(self) -> tuple[Decimal, date]:
        """Read the persisted ``(previous_close_equity, previous_close_date)`` tuple.

        Returns ``(Decimal("0"), date.min)`` on cold start, missing
        file, corrupt JSON, or future date — the same defensive
        behaviour as ``_load_peak_equity``. The sentinel
        ``date.min`` is intentionally not equal to today, so the very
        first call after a cold start will take the
        "stamp previous_close = current NAV" branch (see
        ``_compute_daily_pnl``) instead of computing a bogus P&L.
        """
        try:
            with open(self._daily_pnl_path, "r") as fh:
                data = json.load(fh)
            equity = Decimal(str(data.get("previous_close_equity", "0")))
            date_str = data.get("previous_close_date")
            if not date_str or not isinstance(date_str, str):
                return Decimal("0"), date.min
            parsed_date = date.fromisoformat(date_str)
            if equity < 0:
                logger.warning(
                    "Daily-pnl file %s has negative equity %s; treating as 0",
                    self._daily_pnl_path,
                    equity,
                )
                return Decimal("0"), date.min
            return equity, parsed_date
        except FileNotFoundError:
            return Decimal("0"), date.min
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "Daily-pnl file %s is corrupt (%s); starting from cold",
                self._daily_pnl_path,
                e,
            )
            return Decimal("0"), date.min

    def _save_daily_pnl_state(self, equity: Decimal, stamp_date: date) -> None:
        """Persist the ``(previous_close_equity, previous_close_date)`` tuple.

        Best-effort: an OSError here is logged but does not raise. The
        in-memory tuple is the source of truth for the current
        process; on the next start it's reloaded from disk.
        """
        try:
            os.makedirs(self._peak_store_dir, exist_ok=True)
            with open(self._daily_pnl_path, "w") as fh:
                json.dump(
                    {
                        "previous_close_equity": str(equity),
                        "previous_close_date": stamp_date.isoformat(),
                    },
                    fh,
                )
        except OSError as e:
            logger.warning(
                "Failed to persist daily-pnl state to %s: %s",
                self._daily_pnl_path,
                e,
            )

    def _compute_daily_pnl(self, current_equity: Decimal, today: date) -> tuple[Decimal, bool]:
        """Compute ``daily_pnl`` for the gate, refreshing the on-disk pivot.

        Returns ``(daily_pnl, refreshed)`` where ``refreshed=True`` means
        the tracker stamped a new ``previous_close_equity`` (either a
        cold start or a calendar rollover) and the call that triggered
        this should treat ``daily_pnl == 0`` as "no history for today
        yet" rather than as a real profit.

        Calendar model (issue #197):
          - On the first snapshot of a new trading day, the previous
            trading day's closing NAV becomes the new
            ``previous_close_equity``. ``daily_pnl = 0`` for that
            snapshot — we have no "yesterday close" reference yet, so
            the kill-switch is intentionally silent on the very first
            tick of the day (same as the existing ``daily_pnl >= 0``
            short-circuit in ``_check_daily_loss``).
          - On every subsequent snapshot of the same day,
            ``daily_pnl = current_equity - previous_close_equity``. A
            -3% day will trip the kill-switch on the second snapshot
            onward, as designed.
          - A weekend / holiday rollover means the gap may be larger
            than a single day, but the math still holds — any P&L
            since the last close is what ``_check_daily_loss`` needs.

        Trading-hour granularity: the file's stamp is calendar-date,
        not minute-level. Intra-day ``previous_close_equity`` rolls
        forward once per calendar date, matching the gate's intent
        ("kill if today's drawdown vs. yesterday's close > limit").
        """
        prev_equity, prev_date = self._daily_pnl_state
        # Calendar rollover or cold start: stamp a new pivot.
        # On a cold start, prev_equity == 0 and prev_date == date.min,
        # so the comparison `prev_date != today` is True and we take
        # the "first snapshot of the day" path.
        if prev_date != today:
            self._daily_pnl_state = (current_equity, today)
            # Best-effort persist. ``_save_daily_pnl_state`` already
            # catches OSError, but a JSONEncodeError / programming
            # bug in the dump path must NOT break the order — log
            # and continue. The in-memory state is already updated
            # above, so the current process sees the new pivot;
            # the next process startup may re-load an older pivot
            # from disk and re-stamp on the first call.
            try:
                self._save_daily_pnl_state(current_equity, today)
            except Exception as exc:  # noqa: BLE001 — best-effort persist, see issue #197
                logger.warning(
                    "Failed to persist daily-pnl pivot (issue #197): %s: %s",
                    type(exc).__name__,
                    exc,
                )
            return Decimal("0"), True
        # Same-day subsequent snapshot: real P&L vs. the stamped pivot.
        return current_equity - prev_equity, False

    def _fetch_daily_pnl(self, current_equity: Decimal) -> Decimal:
        """Public wrapper used by integration tests / OrderFlow path.

        Uses ``datetime.now(timezone.utc).date()`` as the trading-day
        key. Callers that need a deterministic date for testing can
        drive ``_compute_daily_pnl`` directly. This wrapper exists so
        that ``OrderFlow.daily_pnl_provider`` can call
        ``tinker._fetch_daily_pnl(nav)`` and stay agnostic of the
        rollover semantics.
        """
        if self._clock is not None:
            today = self._clock().date()
        else:
            today = datetime.now(timezone.utc).date()
        pnl, _refreshed = self._compute_daily_pnl(current_equity, today)
        return pnl

    def is_sandbox(self) -> bool:
        """Sandbox if token starts with 't.' (Tinkoff convention)."""
        return self._token.startswith("t.")

    def _rate_limit_acquire(self) -> None:
        """Block until we have capacity for 1 more request.

        Issue #103: delegates to the shared ``TokenBucket`` primitive
        which is documented thread-safe (``threading.Lock`` inside
        ``acquire``). The previous hand-rolled list-without-lock
        implementation could burst above the SLA under concurrent
        ``place_order`` calls.
        """
        self._rate_bucket.acquire()

    def _build_intent_and_state(
        self,
        order: MarketOrder | LimitOrder,
        client: Any = None,
    ) -> tuple[Any, Any]:
        """Build TradeIntent + PortfolioState for RiskGate.

        Issue #11 (CRITICAL): the historical implementation used
        ``Decimal("1")`` as a placeholder price for MarketOrder and
        ``Decimal("100000")`` as a hardcoded NAV. Both silently bypassed
        RISK_POSITION / RISK_SECTOR. The fix:

        1. For ``MarketOrder``: fetch a live quote via the Tinkoff
           ``market_data.get_last_prices`` API. If the quote is
           unavailable (network blip, SDK exception, instrument
           suspended), refuse the order — fail-safe. Never substitute a
           fake price.
        2. For ``LimitOrder``: use ``order.price`` as before.
        3. PortfolioState: fetch real NAV via ``get_portfolio()``. If the
           fetch fails, refuse the order — fail-safe.

        The ``client`` parameter is required for MarketOrder; it is the
        same ``Client`` instance used by the broker call so we don't
        open a second connection per order.
        """
        from src.risk.gate import TradeIntent

        order_type = OrderType.LIMIT if isinstance(order, LimitOrder) else OrderType.MARKET
        _ = order_type  # currently unused, reserved for Phase 2

        if isinstance(order, MarketOrder):
            # Fail-safe: refuse market orders when no live quote is
            # available. The caller (``place_order``) MUST pass a
            # connected ``client``; we open one here only if absent.
            if client is None:
                from t_tech.invest import Client as _Client

                client = _Client(self._token)
            price = self._fetch_live_quote_price(client, order.ticker)
        else:
            price = order.price

        intent = TradeIntent(
            symbol=order.ticker,
            # BUGFIX (C-4): the previous expression
            #   "buy" if order.side.value.lower() == "sell" else order.side.value.lower()
            # silently inverted SELL → BUY. Pass the side through as-is.
            side=order.side.value.lower(),
            quantity=order.quantity,
            price=price,
        )

        # Real portfolio state via the broker. Failure is fail-safe:
        # if we cannot determine current NAV, we refuse to evaluate the
        # trade rather than guess against a fake 100 000₽ baseline.
        state = self._fetch_real_portfolio_state()
        return intent, state

    def _fetch_live_quote_price(self, client: Any, ticker: str) -> Decimal:
        """Fetch the latest market price for a ticker via Tinkoff.

        Raises ``BrokerError`` if the quote is unavailable. Never
        returns a placeholder — the previous ``Decimal("1")``
        placeholder is the bug we are fixing here.
        """
        try:
            figi = self._ticker_to_figi(client, ticker)
            resp = client.market_data.get_last_prices(figi=[figi])
            for lp in getattr(resp, "last_prices", []):
                if getattr(lp, "figi", None) == figi:
                    q_price = getattr(lp, "price", None)
                    # Structural check (not isinstance) so the function
                    # works with both the real t_tech.invest.Quotation
                    # and MagicMock test doubles.
                    if q_price is None or not (hasattr(q_price, "units") and hasattr(q_price, "nano")):
                        raise BrokerError(f"no usable quote for {ticker} (figi={figi})")
                    units = int(getattr(q_price, "units", 0))
                    nano = int(getattr(q_price, "nano", 0))
                    price = Decimal(units) + Decimal(nano) / Decimal("1000000000")
                    if price <= Decimal("0"):
                        raise BrokerError(f"non-positive quote for {ticker}: {price}")
                    return price
            raise BrokerError(f"no quote in response for {ticker} (figi={figi})")
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"live quote fetch failed for {ticker}: {e}") from e

    def _fetch_real_portfolio_state(self) -> Any:
        """Fetch real NAV/cash/positions from Tinkoff.

        Raises ``BrokerError`` on failure. Returns ``PortfolioState``
        populated with live values. The previous hardcoded
        ``Decimal("100000")`` is the bug we are fixing here.

        Issue #32: peak_equity is now the running high-water mark read
        from disk on construction and updated here (monotonically
        non-decreasing per process). The RiskGate's ``_check_drawdown``
        computes drawdown as ``(peak - current) / peak`` — with peak
        always equal to current, the guard never trips in production,
        even after a 20-30% drawdown. Persisting the high-water mark
        means a single bad order in one cycle triggers the guard in the
        next.
        """
        from src.risk.gate import PortfolioState

        snapshot = self.get_portfolio()
        # Issue #42: a zero-NAV account (cold sandbox, or gRPC response
        # missing total_amount_currencies) must raise a domain error
        # rather than letting pydantic escape with ValidationError —
        # PortfolioState requires gt=0 on both total_equity and peak_equity.
        if snapshot.cash <= 0:
            raise BrokerError(
                f"Portfolio NAV is {snapshot.cash} for account {self._account_id}; "
                "cannot evaluate risk. Fund the account or check that "
                "total_amount_currencies is present in the gRPC response."
            )
        # Issue #32: update the high-water mark BEFORE building the
        # snapshot so peak_equity >= total_equity (the PortfolioState
        # invariant). Persist best-effort; an OSError here does not
        # break the order path.
        if snapshot.cash > self._peak_equity:
            self._peak_equity = snapshot.cash
            self._save_peak_equity()
        # Issue #197: compute daily_pnl from the persisted
        # ``previous_close_equity`` so ``_check_daily_loss`` in
        # ``src/risk/gate.py`` actually has a real P&L figure to
        # evaluate. Without this, ``state.daily_pnl`` defaults to
        # ``Decimal("0")`` and the gate short-circuits — the daily-loss
        # kill-switch is silently a no-op in production.
        # Use ``self._clock`` if configured (test injection), else
        # fall back to ``datetime.now(UTC).date()`` in production.
        if self._clock is not None:
            today = self._clock().date()
        else:
            today = datetime.now(timezone.utc).date()
        daily_pnl = self._compute_daily_pnl(snapshot.cash, today)[0]
        # Issue #191: derive free cash as `NAV − Σ(quantity × avg_price)`
        # so `PortfolioState.cash` reflects tradeable cash, not NAV. The
        # bug is latent today (no `_check_*` in `src/risk/gate.py` reads
        # `state.cash`), but the field's semantic contract is "tradeable
        # cash"; a future cash-adequacy / buy-in-cash cap would silently
        # over-approve against NAV if this fix is missing. Use the same
        # `quantity × avg_price` formula already used by
        # `Position.market_value` at `src/risk/gate.py:135-137` so the
        # math stays consistent with `_check_sector_exposure`.
        positions_value = sum(
            (p.quantity * p.avg_price for p in snapshot.positions),
            Decimal("0"),
        )
        free_cash = snapshot.cash - positions_value
        if free_cash < Decimal("0"):
            # Tinkoff contract guarantees positions ≤ NAV, but a partial /
            # synthetic snapshot could exceed it; clamp rather than let
            # `PortfolioState.cash` ValidationError (`ge=Decimal("0")`).
            free_cash = Decimal("0")
        return PortfolioState(
            total_equity=snapshot.cash,  # Tinkoff returns total_amount_currencies as cash-side NAV
            cash=free_cash,
            positions=[_broker_position_to_gate_position(p) for p in snapshot.positions],
            peak_equity=self._peak_equity,
            daily_pnl=daily_pnl,
        )

    def get_portfolio(self) -> PortfolioSnapshot:
        """Real call to Tinkoff. Returns empty mock if SDK unavailable.

        Live API contract (t_tech.invest SDK 1.49):
        - client.users.get_accounts() → .accounts (list of Account)
        - Account.id is the Tinkoff account_id
        - client.operations.get_portfolio(account_id=...) → PortfolioResponse
        - PortfolioResponse has .positions (list[PortfolioPosition]) and
          .total_amount_currencies (Money). Money has .units + .nano.
        """
        self._rate_limit_acquire()
        try:
            from t_tech.invest import Client

            with Client(self._token) as client:
                acc_response = client.users.get_accounts()
                ops = None
                for a in acc_response.accounts:
                    if a.id == self._account_id:
                        ops = a
                        break
                if ops is None:
                    raise BrokerError(f"account {self._account_id} not found")
                portfolio = client.operations.get_portfolio(account_id=self._account_id)
                positions = []
                if getattr(portfolio, "positions", None):
                    for p in portfolio.positions:
                        # average_position_price is a Quotation (.units + .nano)
                        # OR a legacy Money-like object (.value float) depending
                        # on SDK/mock version. Handle both.
                        avg_price_q = getattr(p, "average_position_price", None) or getattr(
                            p, "average_buy_price", None
                        )
                        if avg_price_q is None:
                            avg_price = Decimal("0")
                        elif hasattr(avg_price_q, "units") and hasattr(avg_price_q, "nano"):
                            avg_price = Decimal(str(getattr(avg_price_q, "units", 0))) + Decimal(
                                str(getattr(avg_price_q, "nano", 0))
                            ) / Decimal("1000000000")
                        else:
                            # Legacy: value is a float-like (e.g. Money.value).
                            try:
                                avg_price = Decimal(str(avg_price_q.value))
                            except (AttributeError, TypeError, ValueError):
                                avg_price = Decimal(str(avg_price_q))
                        positions.append(
                            Position(
                                ticker=getattr(p, "ticker", ""),
                                quantity=Decimal(str(getattr(p, "quantity", 0))),
                                avg_price=avg_price,
                            )
                        )
                total_amount = getattr(portfolio, "total_amount_currencies", None) or getattr(
                    portfolio, "total_amount", None
                )
                cash = Decimal("0")
                if total_amount is None:
                    # Issue #42: distinguish "gRPC gave us no NAV field"
                    # from "account genuinely holds 0 RUB". The default
                    # Decimal("0") conflates a parse/contract failure
                    # with a real zero balance; _fetch_real_portfolio_state
                    # raises BrokerError in either case, but logs the
                    # contract mismatch so the operator can investigate.
                    logger.warning(
                        "get_portfolio() for account %s returned neither "
                        "total_amount_currencies nor total_amount; "
                        "treating as zero NAV",
                        self._account_id,
                    )
                elif hasattr(total_amount, "units") and hasattr(total_amount, "nano"):
                    cash = Decimal(str(getattr(total_amount, "units", 0))) + Decimal(
                        str(getattr(total_amount, "nano", 0))
                    ) / Decimal("1000000000")
                else:
                    # Legacy Money.value
                    try:
                        cash = Decimal(str(total_amount.value))
                    except (AttributeError, TypeError, ValueError):
                        cash = Decimal(str(total_amount))
                return PortfolioSnapshot(
                    account_id=self._account_id,
                    cash=cash,
                    positions=positions,
                    timestamp=datetime.now(timezone.utc),
                )
        except Exception as e:
            raise BrokerError(f"Tinkoff portfolio fetch failed: {e}") from e

    def get_positions(self) -> list[Position]:
        return self.get_portfolio().positions

    def place_order(self, order: MarketOrder | LimitOrder) -> OrderStatus:
        """RiskGate first. Reject before touching broker.

        If risk_gate is None, order is rejected (fail-safe default).
        LIVE_TRADING gate: if env LIVE_TRADING=false, refuse ALL orders.
        This is a hard guarantee for Phase 1: real token may be present
        but no orders are placed regardless of RiskGate.
        """
        if not _assert_not_live_trading("LIVE_TRADING=false — refusing order", order.ticker):
            return OrderStatus.REJECTED
        if self._risk_gate is None:
            logger.warning("RiskGate not configured — rejecting all orders (fail-safe)")
            return OrderStatus.REJECTED

        # Issue #11: We open one Client connection and pass it through
        # both the `_build_intent_and_state` (for live quote + portfolio
        # fetch) and the post_order call. This avoids opening the same
        # connection twice and keeps the rate-limit semantics consistent.
        self._rate_limit_acquire()
        try:
            from t_tech.invest import (
                Client,
                OrderDirection,
                OrderType,
                Quotation,
            )

            with Client(self._token) as client:
                intent, state = self._build_intent_and_state(order, client=client)
                decision = self._risk_gate.evaluate(intent, state)

                if not decision.allowed:
                    logger.warning(
                        "RiskGate blocked order for %s: %s",
                        order.ticker,
                        decision.violations,
                    )
                    return OrderStatus.REJECTED

                # RiskGate approved. Submit to broker.
                direction = (
                    OrderDirection.ORDER_DIRECTION_BUY
                    if order.side == OrderSide.BUY
                    else OrderDirection.ORDER_DIRECTION_SELL
                )
                figi = self._ticker_to_figi(client, order.ticker)
                if isinstance(order, MarketOrder):
                    resp = client.orders.post_order(
                        figi=figi,
                        quantity=int(order.quantity),
                        account_id=self._account_id,
                        direction=direction,
                        order_type=OrderType.ORDER_TYPE_MARKET,
                    )
                else:
                    # Limit order — wrap price into Quotation.
                    # Issue #100: the wire format caps nano at 9 fractional
                    # digits (int<0, 1e9)). Input prices with more than 9
                    # fractional digits used to silently truncate via
                    # ``int((price - int(price)) * 1e9)``, e.g.
                    # ``Decimal("100.0000000001")`` would round-trip as
                    # ``Decimal("100.0")`` — operator places a limit at
                    # a price they did not intend. Floor to 9 fractional
                    # digits explicitly via ``quantize(Decimal("1e-9"))``
                    # with ``ROUND_DOWN`` so the truncation behaviour is
                    # at least deterministic and a warning is logged when
                    # the input had > 9 digits (operator should re-quote).
                    price_9 = order.price.quantize(Decimal("1e-9"), rounding=ROUND_DOWN)
                    fractional = price_9 - Decimal(int(price_9))
                    nano = int(fractional * Decimal(1_000_000_000))
                    # Belt-and-braces: if quantize happened to produce a
                    # non-9-digit residue (e.g. due to a negative price
                    # sneaking in), clamp into the wire-format range.
                    if not 0 <= nano < 1_000_000_000:
                        nano = max(0, min(nano, 999_999_999))
                    if order.price != price_9:
                        logger.warning(
                            "LimitOrder price %s has > 9 fractional digits; "
                            "rounded down to %s (units=%d, nano=%d). "
                            "Re-quote to wire precision to avoid silent loss.",
                            order.price,
                            price_9,
                            int(price_9),
                            nano,
                        )
                    price_q = Quotation(
                        units=int(price_9),
                        nano=nano,
                    )
                    resp = client.orders.post_order(
                        figi=figi,
                        quantity=int(order.quantity),
                        price=price_q,
                        account_id=self._account_id,
                        direction=direction,
                        order_type=OrderType.ORDER_TYPE_LIMIT,
                    )
                # PostOrderResponse.execution_report_status is OrderExecutionReportStatus
                # enum (real SDK) or a plain string (legacy/test mocks). Handle both.
                ers = resp.execution_report_status
                raw_name: str = getattr(ers, "name", None) or str(ers)
                return self._map_status(raw_name)
        except BrokerError as e:
            # Fail-safe: refuse the order if we cannot determine a real
            # price / portfolio state. Logger logs the broker-side reason.
            logger.error(
                "Refusing order for %s due to broker-side precondition: %s",
                order.ticker,
                e,
            )
            return OrderStatus.REJECTED
        except Exception as e:
            raise BrokerError(f"Tinkoff order submit failed: {e}") from e

    def cancel_order(self, order_id: str) -> OrderStatus:
        self._rate_limit_acquire()
        try:
            from t_tech.invest import Client

            with Client(self._token) as client:
                client.orders.cancel_order(account_id=self._account_id, order_id=order_id)
                return OrderStatus.CANCELLED
        except Exception as e:
            raise BrokerError(f"Tinkoff cancel failed: {e}") from e

    @staticmethod
    def _ticker_to_figi(client: Any, ticker: str) -> str:
        """Map ticker to FIGI via Tinkoff instruments API.

        Issue #187: the previous whitelist ``("TQBR", "TQOB")`` excluded
        TQTE ETFs, TQCB corporate/muni bonds, and every SPB foreign-share
        class (SPBXM, TQBS, TQDE, TQNO, TQLV, TQPI). All of those are
        tradable at the broker and supported by the data loaders, but
        the broker was rejecting them at FIGI-mapping time with a
        misleading ``"not found in TQBR/TQOB instrument universe"``
        error. The whitelist now defers to ``_TRADABLE_CLASS_CODES`` in
        ``src/data/tinkoff_loader.py`` — single source of truth shared
        with the data layer.

        Issue #13 (C.1): the historical implementation had
        ``except Exception: pass`` followed by a silent fallback
        ``return ticker`` that sent the literal string ``"SBER"`` to
        ``post_order``. That masked ``LoaderAuthError`` (compromised
        token), ``ConnectionError`` (Tinkoff API down), HTTP 4xx/5xx,
        and rate-limit responses. The order would then fail with
        ``INVALID_ARGUMENT`` deep in the broker, after RiskGate had
        already approved and the rate-limit token was spent.

        The fix: every error path raises ``BrokerError`` with a
        descriptive message. Operators MUST see the actual failure
        reason (auth, network, instrument not found) in the logs.
        """
        # Import here to keep the broker module loadable even if the
        # data package is not on the path (e.g. CLI-only deploys).
        from src.data.tinkoff_loader import _TRADABLE_CLASS_CODES

        try:
            response = client.instruments.find_instrument(query=ticker)
        except Exception as exc:
            raise BrokerError(f"instruments.find_instrument failed for {ticker}: {exc}") from exc
        for inst in getattr(response, "instruments", []):
            if getattr(inst, "ticker", None) == ticker and getattr(inst, "class_code", None) in _TRADABLE_CLASS_CODES:
                return str(inst.figi)
        # No matching tradable instrument — refuse, do NOT silently
        # return the ticker (which would then be sent as a FIGI and
        # produce a confusing INVALID_ARGUMENT deep in the broker).
        # The error message lists the class_codes Tinkoff actually
        # returned so operators can distinguish "wrong ticker" from
        # "right ticker in a class we don't trade" (the latter should
        # never happen given _TRADABLE_CLASS_CODES mirrors the loader
        # universe — if it does, the constant has drifted, file an
        # issue).
        seen_classes = sorted(
            str(c)
            for c in {getattr(inst, "class_code", None) for inst in getattr(response, "instruments", [])}
            if c is not None
        )
        raise BrokerError(
            f"ticker {ticker!r} not found in tradable instrument universe "
            f"(Tinkoff returned {len(getattr(response, 'instruments', []))} "
            f"matches with class_codes {seen_classes!r}; "
            f"expected one of {sorted(_TRADABLE_CLASS_CODES)!r})"
        )

    @staticmethod
    def _map_status(raw: str) -> OrderStatus:
        # raw is OrderExecutionReportStatus enum name (e.g. "EXECUTION_REPORT_STATUS_FILL")
        mapping: dict[str, OrderStatus] = {
            "EXECUTION_REPORT_STATUS_FILL": OrderStatus.FILLED,
            "EXECUTION_REPORT_STATUS_PARTIALLYFILL": OrderStatus.FILLED,
            "EXECUTION_REPORT_STATUS_REJECTED": OrderStatus.REJECTED,
            "EXECUTION_REPORT_STATUS_CANCELLED": OrderStatus.CANCELLED,
        }
        return mapping.get(raw, OrderStatus.SUBMITTED)


def from_env(
    env: Optional[dict[str, str]] = None,
    risk_gate: Any = None,
) -> "TinkoffAccount":
    """Construct TinkoffAccount from environment variables.

    Issue #26: `risk_gate` is forwarded to the constructor so callers
    can share a single RiskGate instance between the gate stage and the
    broker stage. If None, the broker falls back to a fail-safe default
    (all orders rejected, see TinkoffAccount.place_order).
    """
    if env is None:
        env = dict(os.environ)  # cast _Environ[str] to dict[str, str]
    sandbox_token = env.get("TINKOFF_SANDBOX_TOKEN")
    real_token = env.get("TINKOFF_REAL_TOKEN")
    account_id = env.get("TINKOFF_ACCOUNT_ID", "SB1")

    # Prefer REAL token (full universe, 200 req/min)
    if real_token and real_token.strip():
        return TinkoffAccount(token=real_token, account_id=account_id, risk_gate=risk_gate)
    if sandbox_token and sandbox_token.strip() and sandbox_token != "placeholder_get_from_tbank":
        return TinkoffAccount(token=sandbox_token, account_id=account_id, risk_gate=risk_gate)
    raise BrokerError("No TINKOFF_SANDBOX_TOKEN or TINKOFF_REAL_TOKEN set")
