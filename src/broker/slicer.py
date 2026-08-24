"""OrderSlicer — split orders into 5% ADV chunks.

Tinkoff API doesn't support TWAP/VWAP/iceberg natively. This module
implements custom slicing: for large orders, split into 5%-of-ADV
chunks, max 30 minutes total, with rate-limit TokenBucket.

Use: OrderSlicer.slice(intent, adv_shares, risk_limits) -> list[slice_batch]
where each slice_batch has cumulative_pct + start_at + end_at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal


@dataclass
class SliceBatch:
    """One slice of a parent order."""

    ticker: str
    side: str
    quantity: Decimal
    cumulative_pct: Decimal  # % of parent order, 0-100
    start_at: datetime
    end_at: datetime


class OrderSlicer:
    """Pure-Python order slicer. No broker SDK dependency.

    Slice into 5%-of-ADV chunks. Total time = quantity / (5% ADV) minutes.
    """

    CHUNK_PCT = Decimal("5")  # each chunk = 5% of ADV (not of parent)
    MAX_DURATION = timedelta(minutes=30)
    # Tinkoff rate limit: 60 req/sec, burst 5
    MIN_INTERVAL_MS = 1000 // 60  # ~16ms between requests

    def __init__(self, adv_shares: Decimal, parent_qty: Decimal):
        """Slicer for one parent order.

        adv_shares: Average Daily Volume in shares for the ticker.
        parent_qty: Total quantity of the parent order.
        """
        if adv_shares <= Decimal("0"):
            raise ValueError("adv_shares must be > 0")
        if parent_qty <= Decimal("0"):
            raise ValueError("parent_qty must be > 0")
        self.adv_shares = adv_shares
        self.parent_qty = parent_qty

    def slice(self, start_at: datetime | None = None) -> list[SliceBatch]:
        """Split parent into 5% ADV chunks.

        Each chunk = max(5% ADV, parent_qty / n_chunks_needed).

        Returns at least 1 chunk. Empty if parent_qty fits in single chunk.

        Issue #198: the previous implementation scheduled slices at
        ``MAX_DURATION / n_chunks`` interval, where ``n_chunks`` was the
        raw ``parent_qty / (5% ADV)`` ratio. For an extreme ratio (e.g.
        parent=10_000_000, adv=1_000) the ratio is 200_000 chunks and
        the per-chunk interval collapses to 9ms — well below the Tinkoff
        SLA of 60 req/sec (~16ms). The fix caps ``n_chunks`` to
        ``MAX_DURATION / MIN_INTERVAL_MS`` (=112_500 for the current
        constants), guaranteeing every consecutive slice is at least
        ``MIN_INTERVAL_MS`` apart. Chunk size is also clamped to >= 1
        share so an absurdly-large n_chunks ceiling does not produce
        0-quantity slices (parent_qty / 112_500 must still be >= 1).
        """
        if start_at is None:
            start_at = datetime.now(timezone.utc)

        chunks = []

        # If parent fits in 5% ADV single chunk
        if self.parent_qty <= self.adv_shares * self.CHUNK_PCT / Decimal("100"):
            chunks.append(
                SliceBatch(
                    ticker="",
                    side="",
                    quantity=self.parent_qty,
                    cumulative_pct=Decimal("100"),
                    start_at=start_at,
                    end_at=start_at,
                )
            )
            return chunks

        # Multiple chunks: 5% ADV per batch
        chunk_size = self.adv_shares * self.CHUNK_PCT / Decimal("100")
        n_chunks = max(1, int((self.parent_qty / chunk_size).to_integral_value()))

        # Issue #198: cap n_chunks so per-chunk interval >= MIN_INTERVAL_MS.
        # The previous "cap n_chunks so total duration <= MAX_DURATION" loop
        # was dead code: for any N, N * (MAX_DURATION / N) == MAX_DURATION,
        # so the inner break fired on iteration 1 and n_chunks was never
        # decremented. The real rate-limit constraint is per-chunk INTERVAL,
        # not total duration — cap by MAX_DURATION / MIN_INTERVAL_MS instead.
        max_chunks = int(self.MAX_DURATION.total_seconds() * 1000 / self.MIN_INTERVAL_MS)
        n_chunks = max(1, min(n_chunks, max_chunks))

        actual_chunk_size = self.parent_qty / Decimal(n_chunks)
        # Defence-in-depth: if parent_qty < n_chunks (theoretical only,
        # given we already capped n_chunks), bump each chunk to >= 1 share
        # so no 0-quantity slices are emitted. parent_qty is a positive
        # Decimal so actual_chunk_size is always >= 1/n_chunks — keep
        # the clamp explicit for clarity.
        if actual_chunk_size < Decimal("1"):
            n_chunks = max(1, int(self.parent_qty))
            actual_chunk_size = self.parent_qty / Decimal(n_chunks)
        interval = self.MAX_DURATION / n_chunks

        for i in range(n_chunks):
            cumulative = min(self.parent_qty, (i + 1) * actual_chunk_size)
            chunk_qty = min(actual_chunk_size, self.parent_qty - cumulative + actual_chunk_size)
            chunks.append(
                SliceBatch(
                    ticker="",
                    side="",
                    quantity=chunk_qty,
                    cumulative_pct=cumulative / self.parent_qty * Decimal("100"),
                    start_at=start_at + i * interval,
                    end_at=start_at + (i + 1) * interval,
                )
            )

        return chunks
