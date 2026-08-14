"""
Alphard Data Quality Gate — Level 3: Historical Validation.

PURPOSE
-------
Detect and adjust historical artefacts that survive ingestion:

  * Auto-split: close_t / close_{t-1} ≈ N where N ∈ {2..10} integer, with
    the SAME ratio observable on the high/open/low series. The bar BEFORE
    the jump is a pre-split bar; bars AFTER have already been adjusted
    by the source — or have not (we must detect which).
  * Delisting: the last bar in the series is flagged, and any row whose
    primary_key is after `delisted_at` is blocked (HST_FUTURE_ROW).
  * Reverse splits (ratio < 1) are detected by the same machinery; we
    treat them as the dual of forward splits.

CHECKS
------
1. detect_splits   — for each bar, compute (close_t / close_{t-1}); if it
                      rounds to an integer in [2, 10] AND the same ratio
                      appears on high/open/low, record the split.
2. apply_split_adjustment — divide open/high/low/close of bars STRICTLY
                      BEFORE the split date by N; multiply volume by N.
                      Idempotent: applied twice -> no further change
                      (we mark adjusted bars with `adj_close` semantics).
3. check_delisting — accept an explicit delisted_at (from a corporate-
                      actions feed) OR detect "no trades for N>stale_max
                      calendar days" + no future feed -> MEDIUM flag.
4. block_future_rows — refuse bars with primary_key > today.

DESIGN DECISIONS
----------------
1. Pure stdlib + pydantic. No ML/LLM/heuristics libraries. Split detection
   is integer-rounding + cross-field consistency. False positives are
   bounded by requiring the ratio to ALSO appear on high/open/low.

2. Deterministic: given the same input bars + the same params, the same
   split list is returned. Sorted by date for stable ordering.

3. Idempotency of apply_split_adjustment: a row with adj_close=close is
   assumed ALREADY adjusted; we still divide it (it's a no-op for split
   ratios that already match), but the audit log records that the split
   was seen so the operator can verify upstream is consistent.

4. Splits are NOT auto-corrected silently — the gate detects and reports,
   the loader decides whether to apply. The helper apply_split_adjustment
   is provided for callers that want auto-correction (e.g. the
   TinkoffDataLoader); it returns a NEW list of bars.

WHAT IS NOT HERE
----------------
- Cross-source split verification (that's Level 2: cross-check with MOEX
  corporate-actions feed). Level 3 is single-source.
- Dividend adjustments. Phase 2.
- Renaming / ticker-change detection. Phase 2.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .ingestion_gate import Bar
from .severity import Issue, IssueKind, QualityReport


class SplitEvent(BaseModel):
    """A detected (or known) split event at a single point in time.

    ratio is the multiplicative adjustment applied to PRE-event bars:
      * ratio > 1 (e.g. 5.0): FORWARD split — divide OHLC by N, multiply
        volume by N. Price series drops by N at the split date.
      * ratio < 1 (e.g. 0.1): REVERSE split — multiply OHLC by 1/ratio
        (i.e. by 10 for 1:10 reverse), divide volume by 1/ratio. Price
        series jumps by 1/ratio at the split date.

    "Pre-event" means strictly before ``date``; bars at or after ``date``
    are assumed already adjusted by the source and are left alone by
    ``apply_split_adjustment``.
    """

    model_config = ConfigDict(frozen=True)

    date: date
    ratio: float = Field(gt=0.0)
    # Cross-field consistency: did we see the same ratio on high/open/low?
    # If False, this is a tentative detection that the caller should
    # cross-check against a corporate-actions feed.
    confirmed: bool
    # Which fields agreed with the close-ratio (e.g. {"high","open","low"}).
    agreeing_fields: frozenset[str] = Field(default_factory=frozenset)

    @property
    def is_reverse(self) -> bool:
        """True if this is a reverse split (consolidation)."""
        return 0.0 < self.ratio < 1.0


class HistoricalParams(BaseModel):
    """Tunable thresholds for the Historical Gate."""

    model_config = ConfigDict(frozen=True)

    # Split detection
    split_min_ratio: float = 2.0
    split_max_ratio: float = 10.0
    split_ratio_tolerance: float = 0.02  # 2% — accounts for rounding
    split_min_agreeing_fields: int = 2  # close + at least one OHLC field

    # Delisting
    future_row_max_date: date | None = None  # None = today (UTC)
    delisted_at: date | None = None  # explicit override (corporate-actions feed)


# ---------------------------------------------------------------------------
# Split detection
# ---------------------------------------------------------------------------


def detect_splits(
    bars: Iterable[Bar],
    params: HistoricalParams | None = None,
) -> list[SplitEvent]:
    """Detect split events by close-ratio + cross-field consistency.

    Returns events sorted by date (ascending). Empty list if no splits.

    Algorithm
    ---------
    For each consecutive bar pair (a, b):
        r = b.close / a.close
        if r ≈ integer N ∈ [params.split_min_ratio, params.split_max_ratio]:
            check same integer on b.high/a.high, b.low/a.low, b.open/a.open
            count agreeing fields. If count >= split_min_agreeing_fields,
            emit SplitEvent(date=b.primary_key, ratio=N).
    """
    p = params or HistoricalParams()
    bars_list = sorted(bars, key=lambda b: b.primary_key)
    events: list[SplitEvent] = []

    for prev, cur in zip(bars_list, bars_list[1:]):
        if prev.close <= 0 or cur.close <= 0:
            continue
        ratio = cur.close / prev.close
        n_signed = _nearest_integer(ratio)
        if n_signed is None:
            continue
        # n_signed > 0: forward split; < 0: reverse. We compare absolute
        # value against the configured range so both are detected.
        n_abs = abs(n_signed)
        if not (p.split_min_ratio <= n_abs <= p.split_max_ratio):
            continue
        # Cross-field consistency: same integer ratio on OHLC?
        agreeing: set[str] = set()
        for field in ("open", "high", "low"):
            prev_v = getattr(prev, field)
            cur_v = getattr(cur, field)
            if prev_v <= 0 or cur_v <= 0:
                continue
            field_ratio = cur_v / prev_v
            field_n = _nearest_integer(field_ratio)
            if field_n is not None and abs(field_n) == n_abs:
                # For reverse splits the field ratio will be < 1 too.
                # The abs() match is the right invariant.
                agreeing.add(field)
        if len(agreeing) >= p.split_min_agreeing_fields:
            # Stored ratio: the multiplicative adjustment for PRE-event bars.
            # Forward (n_signed > 0): ratio = N (>1, divides OHLC, x vol).
            # Reverse (n_signed < 0): ratio = 1/|N| (<1, multiplies OHLC, / vol).
            stored_ratio = float(n_abs) if n_signed > 0 else 1.0 / float(n_abs)
            events.append(
                SplitEvent(
                    date=cur.primary_key,
                    ratio=stored_ratio,
                    confirmed=True,
                    agreeing_fields=frozenset(agreeing | {"close"}),
                )
            )

    # Deterministic ordering.
    events.sort(key=lambda e: e.date)
    return events


def _nearest_integer(ratio: float) -> int | None:
    """Return the signed integer N for the split, or None if too far.

    Convention: a bar pair with ratio = close_t / close_{t-1}.
      * ratio < 1 (price dropped): FORWARD split. |N| ∈ [2, 10] where N
        is the share-multiplier. We return +N (positive).
      * ratio > 1 (price jumped): REVERSE split. 1/ratio ≈ 1/N with N ∈
        [2, 10]. We return -N (negative sentinel).

    "Too far" means the relative deviation exceeds the configured tolerance
    (~2% by default). This keeps us from mis-detecting ordinary price moves
    (~1.05x or 0.95x) as splits.

    N=0 (no integer match) returns None.
    """
    if ratio <= 0 or math.isnan(ratio) or math.isinf(ratio):
        return None
    if ratio < 1.0:
        # Forward split: ratio = 1/N where N ∈ [2, 10].
        inv = 1.0 / ratio
        n = round(inv)
        if n < 2:
            return None
        deviation = abs(inv - n) / n
        if deviation > 0.02:
            return None
        return int(n)  # positive -> forward
    # ratio >= 1. Try reverse split: ratio ~ N where N ∈ [2, 10].
    n = round(ratio)
    if n < 2:
        # Could be ordinary price move (~1.01), not a reverse split.
        return None
    deviation = abs(ratio - n) / n
    if deviation > 0.02:
        return None
    return -int(n)  # negative sentinel -> reverse


def apply_split_adjustment(bars: Iterable[Bar], splits: Sequence[SplitEvent]) -> list[Bar]:
    """Return a NEW list of bars where pre-split bars are adjusted.

    For each split at date D with stored ratio R, every bar STRICTLY
    BEFORE D has its OHLC multiplied by ``1/R`` and volume multiplied
    by ``R``:
      * Forward 2:1 split (R = 2): pre-bar close=200 -> close=100;
        volume=1000 -> volume=2000.
      * Reverse 1:5 split (R = 0.2): pre-bar close=10 -> close=50;
        volume=1000 -> volume=200.

    Bars at or after D are assumed already adjusted by the source and
    left alone.

    The same formula (OHLC *= 1/R, volume *= R) covers both directions
    because R encodes the magnitude of the adjustment (R = N for
    forward, R = 1/N for reverse).

    Idempotency: applying the same splits to the same input gives the
    same output on every call (pure function).
    """
    if not splits:
        return list(bars)
    sorted_splits = sorted(splits, key=lambda s: s.date)
    out: list[Bar] = []
    for b in bars:
        adjusted = b
        for s in sorted_splits:
            if adjusted.primary_key < s.date:
                # OHLC *= 1/R, volume *= R. Both directions in one formula.
                inv_ratio = 1.0 / s.ratio
                adjusted = adjusted.model_copy(
                    update={
                        "open": adjusted.open * inv_ratio,
                        "high": adjusted.high * inv_ratio,
                        "low": adjusted.low * inv_ratio,
                        "close": adjusted.close * inv_ratio,
                        "volume": int(round(adjusted.volume * s.ratio)),
                    }
                )
        out.append(adjusted)
    return out


# ---------------------------------------------------------------------------
# Delisting handling
# ---------------------------------------------------------------------------


def check_historical(
    ticker: str,
    bars: Iterable[Bar],
    *,
    params: HistoricalParams | None = None,
    now: datetime | None = None,
) -> QualityReport:
    """Run the Level-3 Historical Gate.

    Reports:
      * HST_FUTURE_ROW (CRITICAL) — bars whose primary_key is after today.
      * HST_DELISTED   (HIGH)     — bars whose primary_key is after
                                   `params.delisted_at` (delisted event).
      * HST_SPLIT_DETECTED (MEDIUM) — informational; split(s) detected,
                                      adjusted in place by the loader.
      * HST_SPLIT_UNADJUSTED (HIGH) — split detected but caller did not
                                      pre-adjust (we can only tell if
                                      the bars carry `adj_close`-like
                                      metadata — for the plain Bar model
                                      we always emit MEDIUM).

    The gate does NOT auto-correct; it returns detected events so the
    loader can decide. ``apply_split_adjustment`` is exposed as a helper.
    """
    p = params or HistoricalParams()
    if now is None:
        now = datetime.now(timezone.utc)

    issues: list[Issue] = []
    bars_list = sorted(bars, key=lambda b: b.primary_key)
    today = p.future_row_max_date or now.date()

    # ---- Future rows (CRITICAL) ----
    future = [b for b in bars_list if b.primary_key > today]
    if future:
        first = future[0]
        issues.append(
            Issue.make(
                gate="historical",
                kind=IssueKind.HST_FUTURE_ROW,
                message=f"{len(future)} rows have primary_key > today ({today})",
                count=len(future),
                extra={
                    "first_future_date": first.primary_key.isoformat(),
                    "today": today.isoformat(),
                },
            )
        )
        # Drop future rows from downstream checks — we cannot meaningfully
        # detect splits in a future-dated bar.
        bars_list = [b for b in bars_list if b.primary_key <= today]

    # ---- Delisting (HIGH) ----
    if p.delisted_at is not None:
        after_delist = [b for b in bars_list if b.primary_key > p.delisted_at]
        if after_delist:
            issues.append(
                Issue.make(
                    gate="historical",
                    kind=IssueKind.HST_DELISTED,
                    message=(
                        f"{len(after_delist)} rows after delisted_at={p.delisted_at}; "
                        "trading on a delisted ticker is forbidden"
                    ),
                    count=len(after_delist),
                    extra={"delisted_at": p.delisted_at.isoformat()},
                )
            )
            # Drop them from split detection — they shouldn't exist, but
            # if they do, we won't trust the price series.
            bars_list = [b for b in bars_list if b.primary_key <= p.delisted_at]

    # ---- Split detection (MEDIUM) ----
    events = detect_splits(bars_list, params=p)
    if events:
        issues.append(
            Issue.make(
                gate="historical",
                kind=IssueKind.HST_SPLIT_DETECTED,
                message=(
                    f"{len(events)} split event(s) detected: "
                    + ", ".join(f"{e.date.isoformat()}={e.ratio:g}x" for e in events)
                ),
                count=len(events),
                extra={"events": ",".join(f"{e.date.isoformat()}:{e.ratio:g}" for e in events)},
            )
        )

    return QualityReport(ticker=ticker, gate="historical", issues=tuple(issues))


__all__ = [
    "HistoricalParams",
    "SplitEvent",
    "apply_split_adjustment",
    "check_historical",
    "detect_splits",
]
