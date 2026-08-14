"""Alphard Broker Connector — Phase 1.3.

Purpose
-------
Abstract broker integration behind a single ``BrokerAccount`` ABC, with a
real implementation against Tinkoff Invest sandbox. Phase 1.3 is **sandbox
only** — production token is gated behind a separate config flag that is
not present in Phase 1.3.

Modules
-------
- ``orders`` — :class:`MarketOrder`, :class:`LimitOrder`, :class:`OrderStatus`,
  :class:`OrderResult`, plus minimal pydantic types used by the broker and
  the slicer.
- ``account`` — :class:`BrokerAccount` ABC, :class:`Balance`,
  :class:`AccountPosition`. The ABC is what execution code calls — never
  the Tinkoff implementation directly.
- ``slicer`` — :class:`OrderSlicer`: split large orders into 5% ADV chunks,
  capped at 30 minutes wall-clock, rate-limited through the existing
  :class:`~src.data.token_bucket.TokenBucket`.
- ``tinkoff_account`` — :class:`TinkoffAccount`, the real implementation
  against the ``tinkoff.investments`` SDK. Token from ``$TINKOFF_SANDBOX_TOKEN``.
  The connection is required to **call** :meth:`RiskGate.evaluate` before
  every :meth:`TinkoffAccount.place_order`; this is not overridable.

Hard rules enforced here (not just documented in skills)
--------------------------------------------------------
1. ``RiskGate.evaluate()`` is invoked from
   :meth:`TinkoffAccount.place_order` and from the ABC helper
   :meth:`BrokerAccount.submit_intent` — there is no public method that
   submits an intent without the pre-trade gate. Subclasses cannot bypass
   this without forking the ABC.
2. No margin / short-selling by default — ``RiskLimits.allow_short`` must
   be ``True`` for a SELL intent that would open or extend a short to be
   permitted. Enforcement lives in :func:`RiskGate.evaluate` /
   :meth:`src.risk.gate.RiskGate._check_side`.
3. Sandbox-only until 30-day paper validation passes. :class:`TinkoffAccount`
   refuses to start without ``TINKOFF_SANDBOX_TOKEN``; a production token is
   not accepted by this class — there is no parameter for it (yet).
4. Tinkoff REST rate budget: 60 requests per second with burst 5. Enforced
   via the existing token-bucket machinery; the broker adapter shares a
   ``TokenBucket`` between ``place_order`` and ``cancel_order`` so the
   total request rate is what is bounded.
"""

from __future__ import annotations

from .account import AccountPosition, Balance, BrokerAccount, OrderRejectedByRisk  # noqa: F401
from .orders import (  # noqa: F401
    AccountOrder,
    LimitOrder,
    MarketOrder,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from .slicer import ADVRequired, OrderSlicer, SlicerResult  # noqa: F401
from .tinkoff_account import (  # noqa: F401
    TinkoffAccount,
    TinkoffConfig,
    TinkoffSDKUnavailable,
    TinkoffTokenMissing,
)

__all__ = [
    "AccountOrder",
    "AccountPosition",
    "ADVRequired",
    "Balance",
    "BrokerAccount",
    "LimitOrder",
    "MarketOrder",
    "OrderRejectedByRisk",
    "OrderResult",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "OrderSlicer",
    "SlicerResult",
    "TinkoffAccount",
    "TinkoffConfig",
    "TinkoffSDKUnavailable",
    "TinkoffTokenMissing",
]
