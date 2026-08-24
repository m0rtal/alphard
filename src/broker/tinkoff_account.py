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

from datetime import date, datetime, timedelta, timezone
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


# Moscow-trading-day helper for daily P&L rollover (issue #197).
# The Moscow Exchange (MOEX) trades Mon-Fri; weekends/holidays must
# not split an "intraday" P&L into two separate days. We use
# ``MSK`` (UTC+3) to align with the broker's trading-day boundary.
_MSK = timezone(timedelta(hours=3))


def _msk_today() -> date:
    """Return the current date in Moscow time (UTC+3).

    Used by ``_fetch_real_portfolio_state`` to detect a trading-day
    rollover and refresh ``previous_close_equity``. Always returns a
    ``date`` — never a datetime — so JSON serialisation is trivial.
    """
    return datetime.now(tz=_MSK).date()


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
        # Issue #197: persist a sibling "daily_pnl_basis" tracker alongside
        # the peak-equity HWM so that ``PortfolioState.daily_pnl`` (which
        # drives ``RiskGate._check_daily_loss``) reflects the realised
        # today-vs-previous-close delta rather than defaulting to 0. The
        # previous hardcoded ``Decimal("0")`` meant ``RISK_DAILY_LOSS``
        # never tripped in production — same exploit class as issues
        # #195 (peak_equity not wired) and #11 (placeholder price
        # bypass). Same file-naming convention as the peak store:
        # one file per ``account_id``, best-effort writes, missing file
        # is treated as "no history yet" (daily_pnl == 0, no violation).
        self._daily_pnl_basis_path: str = os.path.join(peak_store_dir, f"daily_pnl_basis_{account_id}.json")
        self._previous_close_equity: Decimal = Decimal("0")
        self._last_trading_day: date | None = None
        self._load_daily_pnl_basis()

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

    def _load_daily_pnl_basis(self) -> None:
        """Read the persisted daily-P&L basis from disk.

        The basis is a ``(previous_close_equity, last_trading_day)``
        pair; on first read in a new trading day,
        ``_fetch_real_portfolio_state`` rolls it over. A missing /
        corrupt file is treated as "no history yet" — i.e.
        ``previous_close_equity == 0`` and ``last_trading_day is None``
        — so ``daily_pnl`` for the first call is 0 (no violation
        possible) and the upcoming snapshot becomes the new basis.
        """
        try:
            with open(self._daily_pnl_basis_path, "r") as fh:
                data = json.load(fh)
            value = Decimal(str(data.get("previous_close_equity", "0")))
            if value < 0:
                logger.warning(
                    "Daily-PnL basis file %s has negative value %s; treating as 0",
                    self._daily_pnl_basis_path,
                    value,
                )
                value = Decimal("0")
            self._previous_close_equity = value
            last_day_raw = data.get("last_trading_day")
            if isinstance(last_day_raw, str):
                try:
                    self._last_trading_day = date.fromisoformat(last_day_raw)
                except ValueError:
                    logger.warning(
                        "Daily-PnL basis file %s has unparseable last_trading_day %r; "
                        "ignoring and treating as cold-start",
                        self._daily_pnl_basis_path,
                        last_day_raw,
                    )
                    self._last_trading_day = None
        except FileNotFoundError:
            # Cold start: keep defaults — daily_pnl == 0 on the first
            # call, no risk violation possible.
            return
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "Daily-PnL basis file %s is corrupt (%s); starting cold",
                self._daily_pnl_basis_path,
                e,
            )
            self._previous_close_equity = Decimal("0")
            self._last_trading_day = None

    def _save_daily_pnl_basis(self) -> None:
        """Persist the current daily-P&L basis to disk (best-effort).

        Write is best-effort: a failure here logs a warning but does
        not raise. The in-memory basis is the source of truth for the
        current process; on the next start the basis is reloaded from
        disk. The worst case is losing one trading day of basis —
        ``_check_daily_loss`` will then evaluate against 0 for one
        call (no violation possible), which is safer than crashing the
        order path.
        """
        try:
            os.makedirs(self._peak_store_dir, exist_ok=True)
            payload: dict[str, str] = {
                "previous_close_equity": str(self._previous_close_equity),
            }
            if self._last_trading_day is not None:
                payload["last_trading_day"] = self._last_trading_day.isoformat()
            with open(self._daily_pnl_basis_path, "w") as fh:
                json.dump(payload, fh)
        except OSError as e:
            logger.warning(
                "Failed to persist daily-PnL basis to %s: %s",
                self._daily_pnl_basis_path,
                e,
            )

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

        Issue #197: ``daily_pnl`` is computed as
        ``current_nav − previous_close_equity`` (where
        ``previous_close_equity`` is the NAV observed on the last
        trading day's rollover), persisted per-process via a sibling
        JSON file. Cold-start (no file) → ``daily_pnl == 0`` (no
        violation possible). Day rollover (today's MSK date != stored
        ``last_trading_day``) → stamp the new basis with the current
        NAV and report ``daily_pnl == 0`` (the new trading day just
        started). Without this wiring, ``_check_daily_loss`` defaults
        to ``daily_pnl == 0`` and ``RISK_DAILY_LOSS`` never trips —
        same kill-switch dead-code pattern as issue #195.
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
        # Issue #197: compute ``daily_pnl`` from the persisted basis
        # before constructing ``PortfolioState``. Two cases:
        #
        # 1. Trading-day rollover (stored ``last_trading_day`` differs
        #    from today's MSK date OR the basis is cold): stamp
        #    ``previous_close_equity = current_nav`` and report
        #    ``daily_pnl == 0``. This is the "first snapshot of the
        #    new day" semantics — the kill-switch is silent until we
        #    have a real intraday delta to evaluate.
        #
        # 2. Same trading day: ``daily_pnl = current_nav −
        #    previous_close_equity``. A negative value trips
        #    ``_check_daily_loss`` when it exceeds
        #    ``RiskLimits.max_daily_loss_pct``.
        today = _msk_today()
        if self._last_trading_day != today:
            self._previous_close_equity = snapshot.cash
            self._last_trading_day = today
            self._save_daily_pnl_basis()
            daily_pnl = Decimal("0")
        else:
            daily_pnl = snapshot.cash - self._previous_close_equity
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
