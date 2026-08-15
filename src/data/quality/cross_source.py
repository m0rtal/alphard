"""
Alphard Data Quality Gate — Level 2: Cross-Source Validation.

PURPOSE
-------
Compare the SAME ticker's price series from TWO independent sources
(typically Tinkoff vs MOEX ISS) and flag divergence that is too large to
plausibly be market noise. The most common cause of large divergence is
an unadjusted corporate action (split / dividend / M&A) that one source
applied and the other did not.

CHECKS
------
1. Pearson correlation on log-returns, computed pairwise over the
   common-date range. Threshold: 0.99 (LOW = correlated, HIGH = suspect).
2. Rolling 5-day divergence: |ln(p_tinkoff) - ln(p_moex)| on aligned
   closes. Mean divergence > 1% on the rolling window -> HIGH.

DESIGN DECISIONS
----------------
1. Pure stdlib + pydantic. NO pandas/numpy/scipy. Pearson correlation
   is computed by hand from the closed-form formula; rolling windows
   are simple Python loops over the aligned series.

2. Determinism: same aligned (date, tinkoff, moex) triples -> same
   correlation and same divergence numbers. No randomness, no
   timezone-dependent comparisons (timestamps come in as ``date``
   objects only).

3. The two series are joined on ``primary_key`` (date). Bars present in
   only one source are DROPPED from the analysis — we cannot compare
   what isn't there. The drop count is reported via ``extra`` so the
   operator can see coverage loss.

4. Length guard: Pearson correlation with < 5 aligned points is
   meaningless. We short-circuit to XSC_SOURCE_MISSING (HIGH) when
   alignment yields fewer than 5 common dates; otherwise the gate
   reports a valid correlation.

WHAT IS NOT HERE
----------------
- Lead/lag detection (Phase 2: a one-tick delay between sources is not
  a quality issue, it's a market microstructure artefact).
- Volume cross-check (Phase 2: Tinkoff volumes are lot-normalised,
  MOEX volumes are deal-counts; not directly comparable).
- Cross-check against the corporate-actions feed (that's Level 3's
  job once we have the split events).
"""

from __future__ import annotations

import math
from datetime import date


from pydantic import BaseModel, ConfigDict, Field

from .severity import Issue, IssueKind, QualityReport


class CrossSourceParams(BaseModel):
    """Tunable thresholds for the Cross-Source Gate."""

    model_config = ConfigDict(frozen=True)

    # Correlation
    correlation_min: float = 0.99
    correlation_min_aligned_points: int = 5

    # Rolling divergence
    rolling_window_days: int = 5
    rolling_max_mean_divergence: float = 0.01  # 1%


class SourceSeries(BaseModel):
    """A single OHLCV series from one upstream source.

    The ``bars`` list is sorted internally; callers do not need to sort.
    Bars whose primary_key is not in BOTH series are dropped during
    alignment (see :func:`align_and_compare`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_name: str = Field(min_length=1, max_length=32)
    bars: tuple[tuple[date, float], ...] = Field(default_factory=tuple)
    # We use a tuple of (date, close) pairs to keep the model tiny and
    # avoid pulling in the ingestion Bar here — Level 2 only needs
    # date + close, not the full OHLCV.
    #
    # Empty / tuple of (date, close) is enough to compute log-returns
    # and the Pearson correlation against another source.

    def __init__(self, **data: object) -> None:
        # Convenience: accept iterable of Bar-like objects too.
        raw = data.get("bars")
        if raw is not None:
            # Probe the first element to decide whether to coerce.
            first = raw[0]  # type: ignore[index]
            if not isinstance(first, tuple):
                coerced: list[tuple[date, float]] = []
                for b in raw:  # type: ignore[attr-defined]
                    coerced.append((b.primary_key, b.close))
                data["bars"] = tuple(coerced)
        super().__init__(**data)


# ---------------------------------------------------------------------------
# Math helpers (pure, deterministic)
# ---------------------------------------------------------------------------


def _log_returns(closes: list[float]) -> list[float]:
    """ln(close_t / close_{t-1}) for t=1..len-1."""
    out: list[float] = []
    prev = closes[0]
    for cur in closes[1:]:
        if prev <= 0 or cur <= 0:
            out.append(float("nan"))
        else:
            out.append(math.log(cur / prev))
        prev = cur
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation between two same-length series.

    Returns None if stdev is zero on either series or len < 2.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = 0.0
    varx = 0.0
    vary = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        dy = y - my
        cov += dx * dy
        varx += dx * dx
        vary += dy * dy
    if varx == 0 or vary == 0:
        return None
    return cov / math.sqrt(varx * vary)


def _align(series_a: SourceSeries, series_b: SourceSeries) -> tuple[list[date], list[float], list[float], int, int]:
    """Inner-join two SourceSeries on primary_key.

    Returns
    -------
    aligned_dates : list[date]
        Sorted list of common dates.
    closes_a, closes_b : list[float]
        Close prices on those dates from each source.
    dropped_a, dropped_b : int
        Number of bars in each series that did NOT have a matching date
        in the other series.
    """
    by_a = {d: c for d, c in series_a.bars}
    by_b = {d: c for d, c in series_b.bars}
    common = sorted(set(by_a) & set(by_b))
    dropped_a = len(by_a) - len(common)
    dropped_b = len(by_b) - len(common)
    closes_a = [by_a[d] for d in common]
    closes_b = [by_b[d] for d in common]
    return common, closes_a, closes_b, dropped_a, dropped_b


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def check_cross_source(
    ticker: str,
    series_a: SourceSeries,
    series_b: SourceSeries,
    *,
    params: CrossSourceParams | None = None,
) -> QualityReport:
    """Run the Level-2 Cross-Source Gate over two aligned price series.

    Both series are aligned on date; pairs without a match in the OTHER
    series are dropped. Pearson correlation is computed on log-returns
    (Phase 1.2 design: log-returns are scale-invariant, so any constant
    offset between sources — e.g. differing spliAdjusted conventions —
    cancels out).

    Returns a QualityReport whose gate is "cross_source" and whose
    issues describe any divergence.
    """
    p = params or CrossSourceParams()
    issues: list[Issue] = []

    common, closes_a, closes_b, dropped_a, dropped_b = _align(series_a, series_b)

    # Drop count matters for coverage context. Log at LOW if non-zero,
    # so operators see coverage drift without false-alerting.
    if dropped_a or dropped_b:
        issues.append(
            Issue.make(
                gate="cross_source",
                kind=IssueKind.XSC_SOURCE_MISSING,
                message=(
                    f"alignment dropped {dropped_a} bar(s) from {series_a.source_name} "
                    f"and {dropped_b} bar(s) from {series_b.source_name}"
                ),
                count=dropped_a + dropped_b,
                extra={
                    "dropped_a": dropped_a,
                    "dropped_b": dropped_b,
                    "source_a": series_a.source_name,
                    "source_b": series_b.source_name,
                },
            )
        )

    if len(common) < p.correlation_min_aligned_points:
        issues.append(
            Issue.make(
                gate="cross_source",
                kind=IssueKind.XSC_SOURCE_MISSING,
                message=(f"only {len(common)} aligned date(s); need >= {p.correlation_min_aligned_points}"),
                count=p.correlation_min_aligned_points - len(common),
                extra={"aligned_count": len(common)},
            )
        )
        return QualityReport(ticker=ticker, gate="cross_source", issues=tuple(issues))

    # Correlation on log-returns (drops one index; matches series length).
    rets_a = _log_returns(closes_a)
    rets_b = _log_returns(closes_b)
    corr = _pearson(rets_a, rets_b)

    if corr is None:
        issues.append(
            Issue.make(
                gate="cross_source",
                kind=IssueKind.XSC_SOURCE_MISSING,
                message="zero variance in one source — cannot correlate",
            )
        )
        return QualityReport(ticker=ticker, gate="cross_source", issues=tuple(issues))

    if corr < p.correlation_min:
        issues.append(
            Issue.make(
                gate="cross_source",
                kind=IssueKind.XSC_CORRELATION_LOW,
                message=(
                    f"Pearson correlation {corr:.4f} < {p.correlation_min:.4f} " f"on {len(common)} aligned dates"
                ),
                count=1,
                extra={
                    "correlation": round(corr, 6),
                    "aligned_count": len(common),
                    "threshold": p.correlation_min,
                },
            )
        )

    # Rolling divergence on closes (mean of |log_a - log_b| over a 5-day window).
    win = p.rolling_window_days
    if len(closes_a) >= win:
        max_mean_div = 0.0
        max_window_end: date | None = None
        for i in range(len(closes_a) - win + 1):
            window_a = closes_a[i : i + win]
            window_b = closes_b[i : i + win]
            if any(c <= 0 for c in window_a + window_b):
                continue
            mean_div = sum(abs(math.log(a) - math.log(b)) for a, b in zip(window_a, window_b)) / win
            if mean_div > max_mean_div:
                max_mean_div = mean_div
                max_window_end = common[i + win - 1]
        if max_mean_div > p.rolling_max_mean_divergence:
            issues.append(
                Issue.make(
                    gate="cross_source",
                    kind=IssueKind.XSC_DIVERGENCE_HIGH,
                    message=(
                        f"max {win}-day mean |log-divergence|={max_mean_div:.4f} "
                        f"> {p.rolling_max_mean_divergence:.4f} "
                        f"(window ended {max_window_end})"
                    ),
                    count=1,
                    extra={
                        "max_mean_divergence": round(max_mean_div, 6),
                        "threshold": p.rolling_max_mean_divergence,
                        "window_end": (max_window_end.isoformat() if max_window_end is not None else ""),
                    },
                )
            )

    return QualityReport(ticker=ticker, gate="cross_source", issues=tuple(issues))


__all__ = [
    "CrossSourceParams",
    "SourceSeries",
    "check_cross_source",
]
