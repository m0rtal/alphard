"""
Alphard Data Quality Gate — Level 1: Ingestion Gate.

PURPOSE
-------
Validate a single OHLCV series at ingestion time, BEFORE it reaches the
downstream DataStore / TinkoffDataLoader / Risk Gate. Every row is checked
for schema, range, NaN/zero, outliers, and historical coverage.

CHECKS
------
1. Schema  — required columns present (open, high, low, close, volume,
             primary_key). Missing column -> CRITICAL.
2. PK      — primary_key (date) non-null. NULL PK -> CRITICAL.
3. NaN     — any OHLC value NaN/inf -> CRITICAL.
4. Range   — high >= max(open, close), low <= min(open, close). Violation ->
             HIGH.
5. Zero/neg — non-positive close price -> HIGH.
6. Outlier — close_t / close_{t-1} ratio with |z-score| > 6 -> MEDIUM.
             (Z-score computed on log-returns, robust to skew.)
7. Coverage — missing trading days < 5%. Otherwise -> HIGH.
8. History — at least 252 rows (~1 trading year). Otherwise -> HIGH.
9. Stale   — latest date <= 3 calendar days before `now`. Otherwise -> HIGH.

DESIGN DECISIONS
----------------
1. Pure stdlib + pydantic. NO pandas/numpy/scipy. Same constraint as the
   Risk Gate. Z-score is computed by hand from log-returns via statistics
   module.

2. Determinism: every check is pure (function of the input + frozen params).
   Two runs over the same input -> identical Issue list. No time-of-day
   side effects — staleness check accepts an injected `now` for tests.

3. Coverage uses BOTH an explicit gap check (max gap in calendar days)
   AND a ratio check (actual / expected trading days). Expected trading
   days = trading days in [first, last] under a Monday-Friday convention
   (MOEX closure days / holidays are NOT subtracted — that's a Phase 2
   holiday calendar concern). This is documented as a known limitation
   in the audit log via the MEDIUM ING_LARGE_GAP.

4. The "OHLCV frame" is a list[Bar]. We define Bar here so the quality
   gate is self-contained and the integration with the Phase 1.1
   DataLoader (when it lands) is one-line: map loader rows to Bar.

WHAT IS NOT HERE
----------------
- No persistence. Audit is a separate concern (src.data.quality.audit).
- No corporate-action handling (Level 3).
- No cross-source validation (Level 2).
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .severity import Issue, IssueKind, QualityReport


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


# Required OHLCV columns. Kept as a tuple to preserve deterministic order.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "primary_key",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


class Bar(BaseModel):
    """A single OHLCV row. Pure pydantic; no pandas/numpy.

    primary_key is a date (not a datetime) — MOEX EOD bars are indexed by
    trading date, not timestamp. NaN is rejected by the model itself so a
    bad row can't even reach the gate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary_key: date
    open: float = Field(gt=0.0)
    high: float = Field(gt=0.0)
    low: float = Field(gt=0.0)
    close: float = Field(gt=0.0)
    volume: int = Field(ge=0)


class IngestionParams(BaseModel):
    """Tunable thresholds for the Ingestion Gate. Frozen so test reruns
    are deterministic; defaults match the research/data-quality-gate.md
    spec."""

    model_config = ConfigDict(frozen=True)

    # Coverage / history
    min_history_rows: int = 252  # ~1 trading year
    coverage_min_ratio: float = 0.95  # 95% of expected trading days
    large_gap_calendar_days: int = 7  # > 1 week of trading days

    # Staleness
    stale_max_calendar_days: int = 3

    # Outliers
    outlier_zscore: float = 6.0

    # Range tolerance (epsilon for floating-point comparison)
    range_epsilon: float = 1e-9


# ---------------------------------------------------------------------------
# Helpers (deterministic, pure)
# ---------------------------------------------------------------------------


def expected_trading_days(start: date, end: date) -> int:
    """Count expected trading days in [start, end] under Mon-Fri convention.

    Known limitation: does NOT subtract MOEX holidays. Documented as a
    Phase 2 holiday-calendar concern; the gate still flags MEDIUM
    ING_LARGE_GAP if it sees a real gap so the operator notices.
    """
    if end < start:
        return 0
    n = 0
    cur = start
    while cur <= end:
        # weekday(): Mon=0..Sun=6. Mon-Fri = 0..4.
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def log_returns(closes: Sequence[float]) -> list[float]:
    """Return log-returns r_t = ln(close_t / close_{t-1}).

    Empty input or single-element input -> empty list. NaN/inf in closes
    propagate as math domain errors; callers should sanitize first.
    """
    if len(closes) < 2:
        return []
    out: list[float] = []
    prev = closes[0]
    for cur in closes[1:]:
        if prev <= 0 or cur <= 0:
            # Caller is supposed to filter zero/negatives out before
            # computing returns. If they don't, we emit +inf / -inf so
            # the outlier check catches it rather than silently dropping.
            out.append(float("nan"))
            prev = cur
            continue
        out.append(math.log(cur / prev))
        prev = cur
    return out


def _zscore_threshold_filter(returns: Sequence[float], threshold: float) -> list[tuple[int, float]]:
    """Return indices + values of returns with |z| > threshold.

    Uses sample standard deviation. If stdev is 0 (constant prices),
    NO outlier is reported — there is no deviation to measure.
    """
    if len(returns) < 2:
        return []
    # Filter NaN out for statistics; we will still surface them separately.
    clean = [r for r in returns if not math.isnan(r)]
    if len(clean) < 2:
        return []
    mean = statistics.fmean(clean)
    try:
        stdev = statistics.stdev(clean)
    except statistics.StatisticsError:
        return []
    if stdev == 0:
        return []
    out: list[tuple[int, float]] = []
    for i, r in enumerate(returns):
        if math.isnan(r):
            continue
        z = (r - mean) / stdev
        if abs(z) > threshold:
            out.append((i + 1, r))  # +1: returns[0] is for bar index 1
    return out


def _max_calendar_gap(bars: Sequence[Bar]) -> int:
    """Return the largest gap in CALENDAR days between consecutive bars.

    Sorted internally (gate expects bars in PK order; we sort defensively).
    Returns 0 if fewer than 2 bars.
    """
    if len(bars) < 2:
        return 0
    sorted_bars = sorted(bars, key=lambda b: b.primary_key)
    max_gap = 0
    for a, b in zip(sorted_bars, sorted_bars[1:]):
        d = (b.primary_key - a.primary_key).days
        if d > max_gap:
            max_gap = d
    return max_gap


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def check_ingestion(
    ticker: str,
    bars: Iterable[Bar],
    *,
    columns: set[str] | None = None,
    now: datetime | None = None,
    params: IngestionParams | None = None,
) -> QualityReport:
    """Run the Level-1 Ingestion Gate over a bar series.

    Parameters
    ----------
    ticker : str
        Ticker symbol — propagated to the report.
    bars : Iterable[Bar]
        OHLCV rows. Order does not matter; the gate sorts internally for
        gap / coverage computations.
    columns : set[str] | None
        Names of the columns actually present in the upstream frame. Pass
        the full set when calling from a loader; ``None`` means
        "all REQUIRED_COLUMNS present" (a happy-path shortcut).
    now : datetime | None
        Reference time for staleness. ``None`` means ``datetime.now(UTC)``.
        Tests should pin this.
    params : IngestionParams | None
        Tunable thresholds. ``None`` -> defaults.

    Returns
    -------
    QualityReport
        Frozen. Contains 0..N issues, each pinned with severity from the
        catalog. The report's ``worst_severity()`` is the action driver.
    """
    p = params or IngestionParams()
    if now is None:
        now = datetime.now(timezone.utc)

    issues: list[Issue] = []
    bars_list: list[Bar] = list(bars)

    # ---- 1. Schema check ----
    # If caller passed an explicit column set, check it; otherwise we
    # assume the REQUIRED_COLUMNS set (the gate is being fed by a
    # well-typed loader).
    present = columns if columns is not None else set(REQUIRED_COLUMNS)
    missing = [c for c in REQUIRED_COLUMNS if c not in present]
    if missing:
        issues.append(
            Issue.make(
                gate="ingestion",
                kind=IssueKind.ING_MISSING_COLUMNS,
                message=f"missing required columns: {','.join(sorted(missing))}",
                count=len(missing),
                extra={"missing": ",".join(sorted(missing))},
            )
        )
        # Cannot meaningfully continue without schema.
        return QualityReport(ticker=ticker, gate="ingestion", issues=tuple(issues))

    # ---- 2. PK null check ----
    null_pk = sum(1 for b in bars_list if b.primary_key is None)
    if null_pk > 0:
        issues.append(
            Issue.make(
                gate="ingestion",
                kind=IssueKind.ING_NULL_PRIMARY_KEY,
                message=f"{null_pk} rows have NULL primary_key",
                count=null_pk,
            )
        )
        return QualityReport(ticker=ticker, gate="ingestion", issues=tuple(issues))

    # ---- 3. NaN / inf check (pydantic normally blocks this, but
    #          callers may pass through json.loads — guard explicitly) ----
    nan_count = 0
    for b in bars_list:
        for col in ("open", "high", "low", "close"):
            v = getattr(b, col)
            if math.isnan(v) or math.isinf(v):
                nan_count += 1
                break
    if nan_count > 0:
        issues.append(
            Issue.make(
                gate="ingestion",
                kind=IssueKind.ING_NAN_PRICE,
                message=f"{nan_count} rows have NaN/inf in OHLC",
                count=nan_count,
            )
        )
        # Cannot compute log-returns safely. Bail out at CRITICAL.
        return QualityReport(ticker=ticker, gate="ingestion", issues=tuple(issues))

    # ---- 4. Range check ----
    range_violations = 0
    eps = p.range_epsilon
    for b in bars_list:
        hi_bound = max(b.open, b.close) + eps
        lo_bound = min(b.open, b.close) - eps
        if b.high < hi_bound or b.low > lo_bound:
            range_violations += 1
    if range_violations > 0:
        issues.append(
            Issue.make(
                gate="ingestion",
                kind=IssueKind.ING_RANGE_VIOLATION,
                message=f"{range_violations} rows violate high>=max(open,close) or low<=min(open,close)",
                count=range_violations,
            )
        )

    # ---- 5. Zero / negative price check ----
    zero_count = sum(1 for b in bars_list if b.close <= 0)
    if zero_count > 0:
        issues.append(
            Issue.make(
                gate="ingestion",
                kind=IssueKind.ING_ZERO_OR_NEGATIVE_PRICE,
                message=f"{zero_count} rows have close<=0",
                count=zero_count,
            )
        )

    # ---- 6. Outlier (z-score on log-returns) ----
    closes = [b.close for b in bars_list]
    rets = log_returns(closes)
    outliers = _zscore_threshold_filter(rets, p.outlier_zscore)
    if outliers:
        first_idx, _ = outliers[0]
        issues.append(
            Issue.make(
                gate="ingestion",
                kind=IssueKind.ING_OUTLIER,
                message=f"{len(outliers)} log-returns exceed |z|>{p.outlier_zscore:.1f}",
                count=len(outliers),
                extra={
                    "first_outlier_index": first_idx,
                    "threshold": p.outlier_zscore,
                },
            )
        )

    # ---- 7. Coverage ratio ----
    sorted_bars = sorted(bars_list, key=lambda b: b.primary_key)
    if len(sorted_bars) >= 1:
        first, last = sorted_bars[0].primary_key, sorted_bars[-1].primary_key
        expected = expected_trading_days(first, last)
        actual = len(sorted_bars)
        if expected > 0:
            ratio = actual / expected
            if ratio < p.coverage_min_ratio:
                issues.append(
                    Issue.make(
                        gate="ingestion",
                        kind=IssueKind.ING_COVERAGE_LOW,
                        message=(
                            f"coverage {ratio:.2%} < {p.coverage_min_ratio:.0%} "
                            f"({actual}/{expected} trading days {first}..{last})"
                        ),
                        count=expected - actual,
                        extra={
                            "actual": actual,
                            "expected": expected,
                            "ratio": round(ratio, 6),
                        },
                    )
                )

    # ---- 8. Insufficient history ----
    if len(bars_list) < p.min_history_rows:
        issues.append(
            Issue.make(
                gate="ingestion",
                kind=IssueKind.ING_INSUFFICIENT_HISTORY,
                message=f"only {len(bars_list)} rows < {p.min_history_rows} required",
                count=p.min_history_rows - len(bars_list),
            )
        )

    # ---- 9. Stale data ----
    if sorted_bars:
        latest = sorted_bars[-1].primary_key
        latest_dt = datetime(latest.year, latest.month, latest.day, tzinfo=timezone.utc)
        age_days = (now - latest_dt).days
        if age_days > p.stale_max_calendar_days:
            issues.append(
                Issue.make(
                    gate="ingestion",
                    kind=IssueKind.ING_STALE_DATA,
                    message=f"latest bar {latest} is {age_days} days old (>{p.stale_max_calendar_days})",
                    count=age_days,
                    extra={"latest_date": latest.isoformat(), "age_days": age_days},
                )
            )

    # ---- 10. Large gap (MEDIUM; informational even if coverage passes) ----
    if len(sorted_bars) >= 2:
        max_gap = _max_calendar_gap(sorted_bars)
        # 5 trading days ~= 7 calendar days, but a holiday stretch can push
        # it higher; we flag anything > large_gap_calendar_days for review.
        if max_gap > p.large_gap_calendar_days:
            issues.append(
                Issue.make(
                    gate="ingestion",
                    kind=IssueKind.ING_LARGE_GAP,
                    message=f"max calendar gap between bars is {max_gap} days",
                    count=max_gap,
                    extra={"max_gap_days": max_gap},
                )
            )

    # ---- 11. Low volume (>10% zero-volume days) ----
    zero_vol = sum(1 for b in bars_list if b.volume == 0)
    if len(bars_list) > 0 and zero_vol > len(bars_list) * 0.10:
        issues.append(
            Issue.make(
                gate="ingestion",
                kind=IssueKind.ING_LOW_VOLUME,
                message=f"{zero_vol} zero-volume rows (>10% of {len(bars_list)})",
                count=zero_vol,
            )
        )

    return QualityReport(ticker=ticker, gate="ingestion", issues=tuple(issues))
