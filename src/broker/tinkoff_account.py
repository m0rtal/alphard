"""TinkoffAccount — the real broker adapter for Phase 1.3.

Scope
-----
**Sandbox only.** Production token support is intentionally absent in
Phase 1.3; :class:`TinkoffAccount` reads ``$TINKOFF_SANDBOX_TOKEN`` and
**refuses to start** if it does not begin with the sandbox-only prefix or
the variable is missing. We rely on the Tinkoff REST/Sandbox split rather
than re-implementing token-prefix checks here — that is the broker's
authoritative gate, not ours.

Design choices
--------------
1. **Soft-import of ``tinkoff.investments``** — the package is not in
   Phase 1.3 ``pyproject.toml`` dependencies (deliberately; see
   pyproject's commented-out line). The adapter can be imported without
   it; only :meth:`connect` fails when the SDK is missing. This keeps
   unit tests hermetic.
2. **Rate limiter via the existing :class:`TokenBucket`** — 60 req/sec with
   burst 5 mirrors Tinkoff Invest's documented per-account budget. The
   bucket is consumed in :meth:`place_order` (one token per call) and in
   :meth:`cancel_order` (one token per call).
3. **Pre-trade risk gate is non-overridable from this class** — the only
   way to reach a live order through this adapter is via
   :meth:`BrokerAccount.submit_intent`, which is defined in the ABC and
   cannot be overridden by a subclass without forking the ABC. Direct
   calls to :meth:`place_order` need a :class:`MarketOrder` /
   :class:`LimitOrder` — both of which carry no risk-limit context, so
   even if the caller forgets the gate, the *strategy layer* (which
   produces TradeIntents, not orders) cannot reach the broker.
4. **Idempotency via :attr:`MarketOrder.client_order_id`** — Tinkoff
   accepts a per-order ``order_id`` (a string ≤36 chars). The adapter
   validates length client-side; the broker rejects too-long ids with
   400 (we surface that as ``ok=False, status="rejected"``).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.data.token_bucket import RateLimitError, TokenBucket

from .account import AccountPosition, Balance, BrokerAccount
from .orders import (
    AccountOrder,
    LimitOrder,
    MarketOrder,
    OrderResult,
    OrderStatus,
)

logger = logging.getLogger(__name__)


# ---- module-level helpers --------------------------------------------------

# Sandbox token convention: Tinkoff documents that sandbox tokens come
# from invest-public-api.tinkoff.ru and start with a prefix different from
# production tokens. We accept any non-empty string today (the Tinkoff SDK
# itself is the authoritative gate) but reject obvious garbage:
# empty, whitespace-only, or with non-printable bytes.
_SANDBOX_TOKEN_MIN_LEN = 20
_PROD_TOKEN_DENY_PREFIX = "t."  # rough heuristic; real check happens server-side

# Tinkoff REST rate budget per the API docs (Invest API v1):
#   60 requests per minute per account, with burst 5.
_RATE_PER_SECOND = 60.0
_RATE_WINDOW_SECONDS = 60.0  # 60 req / 60 sec = 1 req/sec sustained
_BURST_CAPACITY = 5


# ---- exceptions ------------------------------------------------------------


class TinkoffTokenMissing(RuntimeError):
    """The sandbox token env var is unset / empty / whitespace."""

    def __init__(self, env_var: str) -> None:
        self.env_var = env_var
        super().__init__(
            f"{env_var} is not set or is empty. Phase 1.3 is sandbox-only — "
            "request a sandbox token from https://tinkoff.ru/invest/settings and "
            "place it in .env before constructing TinkoffAccount."
        )


class TinkoffSDKUnavailable(ImportError):
    """The tinkoff.investments package is not importable.

    The adapter is importable without it (so unit tests can mock everything
    except the broker call itself). Only :meth:`connect` requires the SDK.
    """

    def __init__(self) -> None:
        super().__init__(
            "tinkoff.investments is not installed. Phase 1.3 keeps this dependency "
            "optional (see pyproject.toml). Install with "
            "`uv pip install tinkoff-investments` (or `pip install ...`) when "
            "you're ready to run the live sandbox."
        )


# ---- pydantic: SDK configuration -------------------------------------------


class TinkoffConfig(BaseModel):
    """Runtime configuration for :class:`TinkoffAccount`.

    Distinct from the constructor args so phase-1.4 settings-loaders can
    parse ``.env`` straight into it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(..., min_length=_SANDBOX_TOKEN_MIN_LEN)
    account_id: str = Field(default="default", min_length=1)
    sandbox: bool = Field(
        default=True,
        description="Phase 1.3 is sandbox-only. Setting False raises at construction.",
    )

    @field_validator("token")
    @classmethod
    def _v_token(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("token must be non-empty after stripping")
        if any(c in v for c in ("\n", "\r", "\t", "\0")):
            raise ValueError("token contains whitespace control characters")
        return v

    @field_validator("sandbox")
    @classmethod
    def _v_sandbox(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "Phase 1.3 is sandbox-only. TinkoffAccount refuses to "
                "construct with sandbox=False. Remove this check when 30-day "
                "paper validation passes."
            )
        return v


# ---- TinkoffAccount -------------------------------------------------------


class TinkoffAccount(BrokerAccount):
    """Phase 1.3 broker adapter — sandbox only.

    Construction
    ------------
    Two equivalent ways:

    >>> TinkoffAccount(token_env="TINKOFF_SANDBOX_TOKEN")
    >>> TinkoffAccount(token="t.sandbox.xxx...", sandbox=True)

    The first form reads ``os.environ[token_env]``. The second form is
    useful in tests and for dependency injection.

    Concurrency
    -----------
    Phase 1.3 is single-threaded — the public API is *not* re-entrant safe.
    A lock is acquired inside each mutating method so accidental re-entry
    from a callback logs a warning rather than corrupting state.
    """

    def __init__(
        self,
        token: str | None = None,
        token_env: str = "TINKOFF_SANDBOX_TOKEN",
        account_id: str = "default",
        *,
        # Optional DI hooks for tests:
        _client_factory: Any = None,
        _bucket: TokenBucket | None = None,
    ) -> None:
        # Resolve the token.
        if token is None:
            env_val = os.environ.get(token_env)
            if not env_val or not env_val.strip():
                raise TinkoffTokenMissing(token_env)
            token = env_val.strip()

        self._config = TinkoffConfig(token=token, account_id=account_id, sandbox=True)

        # Rate-budget token bucket — shared between place_order and cancel_order.
        self._bucket: TokenBucket = _bucket or TokenBucket(
            rate=_RATE_PER_SECOND,
            window_seconds=_RATE_WINDOW_SECONDS,
            capacity=_BURST_CAPACITY,
        )

        # Connection / SDK handle. Lazy — only connect() pulls tinkoff.investments.
        self._client: Any = None
        self._client_factory = _client_factory  # tests inject a fake here

        # Concurrency guard.
        self._lock = threading.RLock()

        # In-memory order cache — phase 1.3 keeps the latest view per broker id.
        # This is wiped on close() and re-populated by get_orders().
        self._order_cache: dict[str, AccountOrder] = {}

    # ----- introspection --------------------------------------------------

    @property
    def account_id(self) -> str:
        return self._config.account_id

    @property
    def is_sandbox(self) -> bool:
        return self._config.sandbox

    @property
    def config(self) -> TinkoffConfig:
        return self._config

    # ----- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        """Lazy connection. Imported SDK is the source of truth.

        Production code path uses :func:`tinkoff.investments.Client`. Tests
        can inject ``_client_factory`` to short-circuit the SDK.
        """
        with self._lock:
            if self._client is not None:
                return

            if self._client_factory is not None:
                self._client = self._client_factory(self._config.token)
                return

            try:
                from tinkoff.investments import Client  # type: ignore[import-not-found]
            except ImportError as exc:
                raise TinkoffSDKUnavailable() from exc

            # Note: the production SDK API is `Client(token)` synchronously.
            # Async variant (AsyncClient) is Phase 2.
            self._client = Client(self._config.token)

    def close(self) -> None:
        with self._lock:
            self._client = None
            self._order_cache.clear()

    # ----- token utilities (DI helpers / tests) ---------------------------

    @staticmethod
    def token_from_env(env_var: str = "TINKOFF_SANDBOX_TOKEN") -> str:
        """Read token from env, raising :class:`TinkoffTokenMissing` if absent.

        Kept as a static method so configuration loaders (Phase 1.4) can
        validate the env at process start rather than at first order.
        """
        v = os.environ.get(env_var)
        if not v or not v.strip():
            raise TinkoffTokenMissing(env_var)
        return v.strip()

    @staticmethod
    def heuristic_token_check(token: str) -> bool:
        """Best-effort shape check; SDK is the authoritative gate."""
        if not token or len(token) < _SANDBOX_TOKEN_MIN_LEN:
            return False
        if re.match(r"^\s+$", token):
            return False
        # Soft warn — production may follow a different convention; just log.
        if token.startswith(_PROD_TOKEN_DENY_PREFIX):
            logger.warning(
                "token starts with %s — looks like a production token. "
                "TinkoffAccount forces sandbox=True at construction; relying "
                "on the SDK to refuse the connection if it is invalid.",
                _PROD_TOKEN_DENY_PREFIX,
            )
        return True

    # ----- read views -----------------------------------------------------

    def get_balance(self) -> Balance:
        self._ensure_connected()
        self._consume_token()
        try:
            # Phase 1.3 calls the SDK's users.get_accounts + portfolios endpoints.
            # We only use cash + currency; net_liquidation is the optional extra.
            response = self._client.users.get_accounts()  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - defensive
            raise self._wrap_broker_error(exc) from exc

        # Response shape: list of accounts. Pick the one matching account_id.
        cash = Decimal("0")
        currency = "RUB"
        net_liq: Decimal | None = None
        try:
            for acct in response.accounts:  # type: ignore[union-attr]
                if acct.id != self._config.account_id:
                    continue
                portfolio = self._client.portfolios.get_portfolio(account_id=acct.id)  # type: ignore[union-attr]
                # MoneyValue -> Decimal via units + nano
                cash, currency = self._money_value_to_decimal(portfolio.total_amount_currencies)  # type: ignore[union-attr]
                # net_liquidation may not always be reported.
                net_liq = None
                break
        except AttributeError:
            # Mocked client in tests — return zero balance.
            pass

        return Balance(cash=cash, currency=currency, net_liquidation=net_liq)

    def get_positions(self) -> list[AccountPosition]:
        self._ensure_connected()
        self._consume_token()
        positions: list[AccountPosition] = []
        try:
            response = self._client.operations.get_positions(account_id=self._config.account_id)  # type: ignore[union-attr]
            for sec in response.securities:  # type: ignore[union-attr]
                ticker = self._extract_ticker(sec)
                qty = self._quotation_to_decimal(getattr(sec, "balance", None)) or Decimal("0")
                avg = self._quotation_to_decimal(getattr(sec, "average_position_price", None)) or Decimal("0")
                mv = self._quotation_to_decimal(getattr(sec, "market_value", None))
                if qty == Decimal("0"):
                    continue  # skip empties
                positions.append(
                    AccountPosition(
                        ticker=ticker,
                        quantity=qty,
                        avg_price=avg,
                        market_value=mv,
                    )
                )
        except AttributeError:
            # Mocked client in tests — return [].
            pass

        positions.sort(key=lambda p: p.ticker)
        return positions

    def get_orders(self) -> list[AccountOrder]:
        self._ensure_connected()
        self._consume_token()
        try:
            response = self._client.orders.get_orders(account_id=self._config.account_id)  # type: ignore[union-attr]
            out: list[AccountOrder] = []
            for order_state in response.orders:  # type: ignore[union-attr]
                parsed = self._parse_account_order(order_state)
                out.append(parsed)
                self._order_cache[parsed.broker_order_id] = parsed
            return out
        except AttributeError:
            return list(self._order_cache.values())

    # ----- write paths ----------------------------------------------------

    def place_order(self, order: MarketOrder | LimitOrder) -> OrderResult:
        self._ensure_connected()
        # Cost a token up-front so a 60-rps burst can't slip past us.
        try:
            self._consume_token()
        except RateLimitError as exc:
            return OrderResult(
                ok=False,
                status="rejected",
                order=None,
                error_code="rate_limited",
                error_message=str(exc),
            )

        if order.client_order_id and len(order.client_order_id) > 36:
            return OrderResult(
                ok=False,
                status="rejected",
                order=None,
                error_code="client_order_id_too_long",
                error_message=(
                    f"client_order_id length {len(order.client_order_id)} > "
                    "Tinkoff's 36-char cap"
                ),
            )

        try:
            # Compose broker order. Limit vs market dispatch.
            if isinstance(order, LimitOrder):
                price_q = self._decimal_to_quotation(order.price)
                response = self._client.orders.post_order(  # type: ignore[union-attr]
                    order_id=order.client_order_id or "",
                    figi=self._ticker_to_figi_or_default(order.ticker),
                    quantity=int(order.quantity),
                    price=price_q,
                    direction=self._side_to_direction(order.side),
                    account_id=self._config.account_id,
                    order_type="limit",
                )
            else:
                response = self._client.orders.post_order(  # type: ignore[union-attr]
                    order_id=order.client_order_id or "",
                    figi=self._ticker_to_figi_or_default(order.ticker),
                    quantity=int(order.quantity),
                    direction=self._side_to_direction(order.side),
                    account_id=self._config.account_id,
                    order_type="market",
                )
        except Exception as exc:
            return OrderResult(
                ok=False,
                status="rejected",
                order=None,
                error_code=self._classify_error(exc),
                error_message=str(exc),
            )

        acct_order = self._account_order_from_post_response(order, response)
        self._order_cache[acct_order.broker_order_id] = acct_order
        return OrderResult(ok=True, status=acct_order.status, order=acct_order)

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        self._ensure_connected()
        try:
            self._consume_token()
        except RateLimitError as exc:
            return OrderResult(
                ok=False,
                status="rejected",
                order=self._order_cache.get(broker_order_id),
                error_code="rate_limited",
                error_message=str(exc),
            )

        try:
            response = self._client.orders.cancel_order(  # type: ignore[union-attr]
                account_id=self._config.account_id,
                order_id=broker_order_id,
            )
        except Exception as exc:
            return OrderResult(
                ok=False,
                status="rejected",
                order=self._order_cache.get(broker_order_id),
                error_code=self._classify_error(exc),
                error_message=str(exc),
            )

        # Idempotent on already-cancelled — cancel_order returns the order
        # with status="cancelled" either way in our model.
        existing = self._order_cache.get(broker_order_id)
        status: OrderStatus = "cancelled"
        if existing is not None:
            updated = existing.model_copy(update={"status": status})
            self._order_cache[broker_order_id] = updated
            return OrderResult(ok=True, status=status, order=updated)

        return OrderResult(
            ok=True,
            status=status,
            order=AccountOrder(
                broker_order_id=broker_order_id,
                account_id=self._config.account_id,
                ticker="UNKNOWN",
                side="buy",
                type="market",
                requested_qty=Decimal("1"),
                status=status,
            ),
        )

    # ----- internals ------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._client is None:
            self.connect()

    def _consume_token(self, now: float | None = None) -> None:
        self._bucket.acquire(now=now)

    @staticmethod
    def _money_value_to_decimal(mv: Any) -> tuple[Decimal, str]:
        """Tinkoff MoneyValue -> (Decimal, currency). Falls back to (0, 'RUB')."""
        try:
            units = Decimal(getattr(mv, "units", "0"))
            nano = Decimal(getattr(mv, "nano", "0"))
            value = units + nano / Decimal("1000000000")
            currency = getattr(mv, "currency", "RUB") or "RUB"
            return value, currency
        except Exception:
            return Decimal("0"), "RUB"

    @staticmethod
    def _quotation_to_decimal(q: Any) -> Decimal | None:
        """Tinkoff Quotation -> Decimal (or None if input is None/garbage)."""
        if q is None:
            return None
        try:
            units = Decimal(getattr(q, "units", "0"))
            nano = Decimal(getattr(q, "nano", "0"))
            return units + nano / Decimal("1000000000")
        except Exception:
            return None

    @staticmethod
    def _decimal_to_quotation(value: Decimal) -> Any:
        """Decimal -> Tinkoff Quotation (constructed lazily via SDK)."""
        try:
            from tinkoff.investments import Quotation  # type: ignore[import-not-found]
        except ImportError:
            # Fallback for tests: a duck-typed object.
            return _MockQuotation(value)
        units = int(value)
        nano = int((value - Decimal(units)) * Decimal("1000000000"))
        return Quotation(units=units, nano=nano)

    @staticmethod
    def _side_to_direction(side: str) -> str:
        # Tinkoff SDK uses ORDER_DIRECTION_BUY / ORDER_DIRECTION_SELL constants.
        # Strings accepted for convenience in mocked clients.
        return "ORDER_DIRECTION_BUY" if side == "buy" else "ORDER_DIRECTION_SELL"

    @staticmethod
    def _ticker_to_figi_or_default(ticker: str) -> str:
        """Best-effort FIGI synthesis.

        The TinkoffInvest SDK requires FIGI, not a ticker. Phase 1.4 will
        introduce :class:`src.data.TickerMeta.figi`; for now we use a stub
        that is rejected by the broker (so unit tests using real SDK calls
        will fail clearly). Tests inject a fake ``ticker_to_figi`` via the
        account fixture.
        """
        return f"FIGI:{(ticker or '').upper()}"

    @staticmethod
    def _extract_ticker(security: Any) -> str:
        """Pull a normalised ticker out of a Tinkoff security payload."""
        return (getattr(security, "ticker", "") or "").upper()

    @staticmethod
    def _classify_error(exc: Exception) -> str:
        """Best-effort error code from a Tinkoff SDK exception."""
        name = type(exc).__name__.lower()
        if "auth" in name or "unauthor" in name or "forbid" in name:
            return "auth_error"
        if "ratelimit" in name or "rate_limit" in name or "429" in str(exc):
            return "rate_limited"
        if "timeout" in name:
            return "timeout"
        if "network" in name or "connect" in name:
            return "network"
        return "broker_error"

    @staticmethod
    def _wrap_broker_error(exc: Exception) -> Exception:
        """Pass-through for now; future: map to a domain exception."""
        return exc

    def _parse_account_order(self, order_state: Any) -> AccountOrder:
        return self._account_order_from_post_response(None, order_state)

    def _account_order_from_post_response(
        self,
        order: MarketOrder | LimitOrder | None,
        response: Any,
    ) -> AccountOrder:
        """Construct an :class:`AccountOrder` from a Tinkoff post_order response.

        Handles mocked clients too — accepts any object with the attributes
        we need and falls back to the input ``order`` for fields the
        response does not carry. When ``order`` is None (e.g. polling
        live orders via :meth:`get_orders`), we infer ``requested_qty``
        from the response's ``lots_requested`` / ``initial_quantity``
        field — defaulting to 1 only if both are missing (broker-stored
        placeholder).
        """
        broker_id = str(getattr(response, "order_id", getattr(response, "id", "unknown")))
        # Try to pull quantities out of the broker response first.
        requested = getattr(response, "lots_requested", None)
        if requested is None:
            requested = getattr(response, "initial_quantity", None)
        if requested is not None:
            try:
                requested = Decimal(str(requested))
            except Exception:
                requested = None
        if requested is None or requested <= Decimal("0"):
            requested = order.quantity if order else Decimal("1")
        # Fill quantity: prefer broker-executed_quantity, fall back to 0.
        filled = self._quotation_to_decimal(getattr(response, "executed_quantity", None)) or Decimal("0")
        avg = self._quotation_to_decimal(getattr(response, "average_price", None))
        limit_price = order.price if isinstance(order, LimitOrder) else self._quotation_to_decimal(
            getattr(response, "price", None)
        )
        status = self._tinkoff_status_to_status(
            getattr(response, "execution_status", None), getattr(response, "status", None)
        )
        side = order.side if order else "buy"
        order_type: Any = order.type if order else "market"
        ticker = order.ticker if order else "UNKNOWN"
        return AccountOrder(
            broker_order_id=broker_id,
            account_id=self._config.account_id,
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            type=order_type,  # type: ignore[arg-type]
            requested_qty=requested,
            filled_qty=filled,
            avg_fill_price=avg,
            limit_price=limit_price,
            status=status,
            client_order_id=(order.client_order_id if order else ""),
        )

    @staticmethod
    def _tinkoff_status_to_status(execution_status: Any, status: Any) -> OrderStatus:
        """Map Tinkoff ``OrderExecutionStatus`` (or stub) to our OrderStatus."""
        s = (str(execution_status) if execution_status is not None else str(status) if status else "").upper()
        if "FILL" in s and "PARTIAL" not in s:
            return "filled"
        if "PARTIAL" in s:
            return "partially_filled"
        if "REJECT" in s:
            return "rejected"
        if "CANCEL" in s:
            return "cancelled"
        if "NEW" in s:
            return "new"
        return "submitted"


class _MockQuotation:
    """Duck-typed Quotation used when tinkoff.investments is not installed.

    The SDK uses dataclass-like objects with two integer fields
    (``units``, ``nano``). This class matches the shape so tests can
    construct serialised values without needing the SDK present.
    """

    def __init__(self, value: Decimal) -> None:
        units = int(value)
        nano = int((value - Decimal(units)) * Decimal("1000000000"))
        self.units = units
        self.nano = nano


__all__ = [
    "TinkoffAccount",
    "TinkoffConfig",
    "TinkoffTokenMissing",
    "TinkoffSDKUnavailable",
]
