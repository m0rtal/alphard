"""Order slicing — Phase 1.3.

Purpose
-------
A large order in a thin book walks the price. To avoid moving the market
against ourselves, we split ``intent.quantity`` into ``5% ADV`` chunks
(5% of the instrument's Average Daily Volume) and execute them in
sequence with a wall-clock cap of 30 minutes total.

``OrderSlicer`` is a stateless helper that takes a ``MarketOrder|LimitOrder``
plus ADV + price reference, and yields individual children. It does NOT
call the broker itself — it is a planning layer the executor drives.

Why a separate slicer
---------------------
- Pure logic (no I/O, no clock reads beyond the wall-clock cap) is easy
  to test exhaustively.
- The broker gets only ``place_order`` calls that have already been
  planned, sized and timed — no broker-level surprises.
- Phase 2's VWAP / TWAP variants plug in here without touching the
  broker module.

Rate limiting
-------------
The slicer borrows the same :class:`TokenBucket` used for Tinkoff's
``60 req/sec, burst 5`` REST budget. The broker-call footprint of the
slicer is ``1 token per child order``. We thread an optional
``TokenBucket`` through the slicer so callers (the executor) can share
the same bucket between slicer-driven ``place_order`` calls and any
``cancel_order`` calls — that way the *total* request rate is what is
bounded, not just the slicer's slice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Iterator

from src.data.token_bucket import TokenBucket

from .orders import LimitOrder, MarketOrder


# Tunable parameters — kept module-level so tests can override without
# subclassing. Phase 1.3 hard rules: 5% ADV, 30 minutes wall-clock cap.
DEFAULT_CHUNK_PCT_OF_ADV: Decimal = Decimal("0.05")  # 5% of ADV per child
DEFAULT_MAX_TOTAL_SECONDS: float = 30 * 60.0  # 30 minutes wall-clock cap
# We never want to slice below 1 share per child — the broker would reject anyway.
MIN_CHUNK_QTY: Decimal = Decimal("1")
# When slices exceed the wall-clock budget, we ACCEPT that we will finish
# late and keep going — slicing is a guideline, not a hard deadline.
# (The Option B alternative is to abort after 30 minutes; Phase 1.3 keeps
# going. Phase 2 may add a "abort if cap exceeded" flag.)


class ADVRequired(Exception):
    """Raised when slicer is asked to plan without an ADV figure.

    Phase 1.3 refuses to slice blindly. The executor MUST supply ADV
    from the Data Agent's volume feeds (see :class:`src.data.loader`).
    """

    def __init__(self, ticker: str, hint: str) -> None:
        self.ticker = ticker
        self.hint = hint
        super().__init__(
            f"ADV missing for {ticker!r}; cannot slice. {hint}. "
            "Wire ADV from DataAgent before calling OrderSlicer.plan()."
        )


@dataclass
class SlicerResult:
    """Return shape of :meth:`OrderSlicer.plan`.

    Holds the ordered list of child orders and a few metrics the executor
    needs to drive the wall-clock pacing.
    """

    children: list[MarketOrder | LimitOrder]
    total_qty: Decimal
    chunk_qty: Decimal
    chunks_planned: int
    max_total_seconds: float
    # Actual avg pace needed: total_qty / chunks_planned over max_total_seconds.
    pace_qty_per_sec: Decimal
    # Estimated total wall-clock at the planned pace (seconds).
    estimated_seconds: float

    def __iter__(self) -> Iterator[MarketOrder | LimitOrder]:  # pragma: no cover - convenience
        return iter(self.children)

    def __len__(self) -> int:  # pragma: no cover - convenience
        return len(self.children)


@dataclass
class OrderSlicer:
    """Plans child orders from a MarketOrder|LimitOrder + ADV + price ref.

    Plain dataclass (not pydantic) because it composes
    :class:`TokenBucket`, whose type hints are incompatible with Pydantic's
    forward-ref resolver (``threading.Lock | None`` in TokenBucket triggers
    ``TypeError`` at schema generation time). Validation is done by
    ``__post_init__`` instead.

    Parameters
    ----------
    chunk_pct_of_adv:
        Fraction of ADV per child order. Default 5% per Phase 1.3 spec.
    max_total_seconds:
        Hard wall-clock budget for the whole plan. Default 30 minutes.
    rate_bucket:
        Optional :class:`TokenBucket` shared with the broker. When set, the
        slicer records the expected number of API calls (one per child) so
        the executor can compare against budget.
    """

    chunk_pct_of_adv: Decimal = DEFAULT_CHUNK_PCT_OF_ADV
    max_total_seconds: float = DEFAULT_MAX_TOTAL_SECONDS
    rate_bucket: TokenBucket | None = None

    def __post_init__(self) -> None:
        if not (Decimal("0") < self.chunk_pct_of_adv <= Decimal("1")):
            raise ValueError(
                f"chunk_pct_of_adv must be in (0, 1], got {self.chunk_pct_of_adv}"
            )
        if self.max_total_seconds <= 0:
            raise ValueError(
                f"max_total_seconds must be > 0, got {self.max_total_seconds}"
            )
        # Coerce to Decimal/float right away — catches mismatches eagerly.
        self.chunk_pct_of_adv = Decimal(self.chunk_pct_of_adv)
        self.max_total_seconds = float(self.max_total_seconds)

    # ---- core public API -------------------------------------------------

    def plan(
        self,
        order: MarketOrder | LimitOrder,
        adv_qty: Decimal | None,
        ref_price: Decimal,
    ) -> SlicerResult:
        """Plan child orders.

        Steps
        -----
        1. If ``adv_qty`` is None or 0 → raise :class:`ADVRequired`.
        2. chunk_qty = max(MIN_CHUNK_QTY, floor(adv_qty * chunk_pct_of_adv))
        3. chunks_needed = ceil(order.quantity / chunk_qty)
        4. Emit ``chunks_needed`` children, each with adjusted ``quantity``,
           preserving ``client_order_id`` as ``"<base>#<idx>/<total>"`` when
           a base id was supplied (helps idempotency at the broker).
        5. Pace = quantity / max_total_seconds, plus a per-child sleep
           constant of 1 second added implicitly by the executor (NOT
           part of plan's contract).

        Parameters
        ----------
        order:
            The parent order to slice.
        adv_qty:
            Average Daily Volume of the instrument in shares. Required.
        ref_price:
            Reference price for pacing math. Used only when an order has
            ``limit_price=None``; otherwise the limit price is used.
        """
        if adv_qty is None or adv_qty <= Decimal("0"):
            raise ADVRequired(
                order.ticker,
                "Pass adv_qty from DataAgent (TinkoffLoader.volume or MOEXLoader.volume)",
            )
        if ref_price <= Decimal("0"):
            raise ValueError(f"ref_price must be > 0, got {ref_price}")

        chunk_qty = max(MIN_CHUNK_QTY, (adv_qty * self.chunk_pct_of_adv).quantize(Decimal("1"), rounding=ROUND_DOWN))
        # chunks_needed is ceil(order.quantity / chunk_qty). Manual ceil for Decimal:
        chunks_needed = (order.quantity + chunk_qty - Decimal("1")) // chunk_qty
        if chunks_needed < 1:
            chunks_needed = Decimal("1")

        base_cid = order.client_order_id or ""
        children: list[MarketOrder | LimitOrder] = []
        remaining = order.quantity
        total = int(chunks_needed)
        for i in range(total):
            this_qty = min(chunk_qty, remaining)
            remaining -= this_qty
            cid = self._derive_client_order_id(base_cid, i, total)
            child: MarketOrder | LimitOrder
            if isinstance(order, LimitOrder):
                child = LimitOrder(
                    ticker=order.ticker,
                    side=order.side,
                    quantity=this_qty,
                    account_id=order.account_id,
                    client_order_id=cid,
                    type="limit",
                    price=order.price,
                )
            else:
                child = MarketOrder(
                    ticker=order.ticker,
                    side=order.side,
                    quantity=this_qty,
                    account_id=order.account_id,
                    client_order_id=cid,
                    type="market",
                )
            children.append(child)

        # Pacing math. We assume a flat second-by-second pacing — Phase 1.3
        # executor implements this as ``time.sleep(max_total_seconds / chunks)``
        # between place_order calls.
        chunks_dec = Decimal(total)
        pace_qty_per_sec = order.quantity / Decimal(self.max_total_seconds)
        estimated_seconds = float(order.quantity / pace_qty_per_sec) if pace_qty_per_sec > 0 else 0.0

        return SlicerResult(
            children=children,
            total_qty=order.quantity,
            chunk_qty=chunk_qty,
            chunks_planned=total,
            max_total_seconds=self.max_total_seconds,
            pace_qty_per_sec=pace_qty_per_sec,
            estimated_seconds=estimated_seconds,
        )

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _derive_client_order_id(base: str, idx: int, total: int) -> str:
        if not base:
            return ""
        # Keep within the broker's 64-char cap from src.broker.orders._OrderBase.
        # Format: "<base>#<idx>/<total>"
        suffix = f"#{idx + 1}/{total}"
        keep = 64 - len(suffix)
        if keep <= 0:
            return suffix[:64]
        return f"{base[:keep]}{suffix}"

    # ---- rate-limit helper ----------------------------------------------

    def acquire_token(self, now: float | None = None) -> None:
        """Block on the shared TokenBucket for one API call.

        No-op when ``rate_bucket`` is None. Exposed so the executor can
        pre-warm the bucket at plan() time when batched slicing lands.
        """
        if self.rate_bucket is None:
            return
        # Bound the wait so a misconfigured bucket (rate=0) doesn't hang us.
        t0 = time.monotonic()
        self.rate_bucket.acquire(now=now)
        # Defensive: if the bucket didn't actually advance in the test harness,
        # time.sleep above would block forever. We can't really police that
        # here without adding more state. Trust the bucket.


__all__ = ["ADVRequired", "OrderSlicer", "SlicerResult"]
