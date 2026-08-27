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
        if raw:
            # Probe the first element to decide whether to coerce.
            # Guard with truthy check so empty iterables fall through
            # to pydantic validation as `()` (the default_factory),
            # instead of raising IndexError on raw[0].
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
    """Return one log-return per adjacent close pair: ``len(closes) - 1``
    elements total.

    Pair semantics: ``out[i] = log(closes[i+1] / closes[i])`` if both
    closes are finite and positive; ``out[i] = float('nan')`` otherwise
    (a gap return). When the FIRST close is bad, the corresponding
    leading return is a NaN gap — not a silent drop — so the output
    length always matches ``len(closes) - 1`` (issue #278).

    Last-valid-baseline (issue #271): when a close is bad, the
    corresponding return is NaN and the baseline cursor is NOT
    advanced — the next valid bar pairs against the same baseline
    instead of against the corrupted value. This prevents a single
    zero/negative bar from poisoning every subsequent return.

    Issue #271 (regression): the previous implementation advanced the
    ``prev`` cursor unconditionally, so when ``cur`` was zero/negative,
    ``prev`` became zero/negative too. Every subsequent return was NaN
    because ``prev <= 0`` fired on every iteration, even when the
    actual adjacent closes were perfectly fine. That NaN-poisoned
    series then slipped through ``_pearson`` (which did not detect NaN
    as a missing case) and through ``check_cross_source`` (which
    compared ``NaN < threshold`` and got False), silently bypassing
    the Level-2 gate.

    Issue #278 (regression-of-regression): the post-#271 fix tracked
    ``last_valid`` instead of ``prev`` so a bad bar never advanced the
    baseline — but for the LEADING edge it still silently skipped the
    first slot, producing a length-shortened output (``len(closes) - 2``
    instead of ``len(closes) - 1``). That length asymmetry tripped the
    ``n != len(ys)`` guard in ``_pearson`` and caused
    ``check_cross_source`` to emit a misleading ``XSC_SOURCE_MISSING -
    zero variance`` message even when the real cause was a leading-bad
    bar in one source.
    """
    out: list[float] = []
    n = len(closes)
    if n < 2:
        return out
    # ``last_valid`` is the most recent CLOSE that is finite and > 0,
    # or None if we have not yet seen one. It is consumed as the
    # denominator of the next return; it is updated to ``cur`` only
    # when ``cur`` is valid. This two-state machine produces the
    # desired gap semantics:
    #   * pre-baseline (last_valid is None): the FIRST valid close
    #     latches the baseline (no return emitted — there is no
    #     previous close to compare against). Subsequent bars in this
    #     state emit a NaN gap-return (the pair with the immediately
    #     preceding bar is bad) AND, if valid, become the new
    #     baseline.
    #   * post-baseline (last_valid is set): every bar emits a return
    #     (real if the bar is valid, NaN gap otherwise) and ``last_valid``
    #     advances only on valid bars.
    last_valid: float | None = None
    for i, cur in enumerate(closes):
        is_valid = math.isfinite(cur) and cur > 0
        if not is_valid:
            if last_valid is None:
                # Pre-baseline bad bar: the current slot is the
                # right-hand side of the pair (closes[i-1], closes[i]),
                # and ``closes[i-1]`` was also bad (we never latched),
                # so the gap-return is NaN. Emit a NaN-gap here to
                # maintain ``len(out) == len(closes) - 1`` (issue
                # #278). Stay in pre-baseline.
                if i >= 1:
                    out.append(float("nan"))
            else:
                # Post-baseline bad bar: emit NaN gap; do NOT advance
                # ``last_valid`` so the next valid bar pairs against the
                # same baseline (issue #271 fix).
                out.append(float("nan"))
            continue
        # ``cur`` is valid.
        if last_valid is None:
            if i == 0:
                # First slot, valid. Latch baseline; no return emitted
                # because there is no previous close to pair against.
                last_valid = cur
            else:
                # Pre-baseline slot, valid: the pair (closes[i-1],
                # closes[i]) has ``closes[i-1]`` bad → NaN gap. Emit
                # the gap-return and latch ``cur`` as the new baseline
                # for the NEXT iteration.
                out.append(float("nan"))
                last_valid = cur
            continue
        # Post-baseline: emit the real log-return for the pair
        # (``last_valid``, ``cur``), then advance the baseline.
        out.append(math.log(cur / last_valid))
        last_valid = cur
    return out


def _pearson_with_reason(xs: list[float], ys: list[float]) -> tuple[float | None, str, str]:
    """Pearson correlation between two same-length series, with a reason code.

    Returns ``(correlation, reason_code, detail)`` where ``reason_code``
    is one of:

    * ``"ok"``              — finite correlation computed successfully.
    * ``"length_mismatch"`` — ``len(xs) != len(ys)`` or ``n < 2``;
                             detail reports the two lengths.
    * ``"nan_gap"``         — one or more paired entries are non-finite
                             (typically a NaN-poisoned return from a
                             bad close in one source); detail reports
                             the count and the first few indices.
    * ``"zero_variance"``   — stdev of ``xs`` or ``ys`` is zero
                             (genuinely flat series); detail names the
                             side that is flat.

    Issue #271 (regression): pre-fix this function did not detect NaN
    in either series, so ``sum(xs)`` with one NaN element returned NaN,
    which silently propagated through ``varx/vary`` (NaN == 0 is False,
    so the zero-variance guard did not trip) and produced a final
    correlation of NaN. Callers compared NaN with thresholds and
    always got False, silently bypassing the gate.

    Issue #278 (regression-of-regression): the previous fix returned
    ``None`` from three distinct guards (length-mismatch, NaN, and
    zero-variance) with no way for the caller to tell them apart.
    ``check_cross_source`` collapsed every ``None`` return into one
    hardcoded "zero variance in one source" message, which was
    factually wrong for both the length-mismatch case (a leading-bad
    bar tripping ``n != len(ys)``) and the NaN-gap case (a single
    bad close making one of the returns NaN). This function restores
    the discriminator so the gate can emit an accurate message.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None, "length_mismatch", f"series length mismatch ({n} vs {len(ys)})"
    # Collect NaN-gap indices first so the detail field can report them.
    bad_idx: list[int] = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        if not (math.isfinite(x) and math.isfinite(y)):
            bad_idx.append(i)
    if bad_idx:
        sample = bad_idx[:5]
        return (
            None,
            "nan_gap",
            f"{len(bad_idx)} NaN gap return(s) at index {sample} " f"(bad close in one source)",
        )
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
        # Identify which side is flat so the operator can act on it.
        flat = "xs" if varx == 0 else ("ys" if vary == 0 else "xs,ys")
        return None, "zero_variance", f"zero variance in {flat} — cannot correlate"
    return cov / math.sqrt(varx * vary), "ok", ""


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Backward-compat wrapper around :func:`_pearson_with_reason`.

    Returns just the correlation value (or ``None`` on any failure).
    Prefer :func:`_pearson_with_reason` in new code — it surfaces the
    failure mode so the gate can emit an accurate message (issue #278).
    """
    corr, _, _ = _pearson_with_reason(xs, ys)
    return corr


def _align(
    series_a: SourceSeries, series_b: SourceSeries
) -> tuple[list[date], list[float], list[float], int, int]:  # noqa: E501
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
                message=(
                    f"only {len(common)} aligned date(s); need >= {p.correlation_min_aligned_points}"  # noqa: E501
                ),  # noqa: E501
                count=p.correlation_min_aligned_points - len(common),
                extra={"aligned_count": len(common)},
            )
        )
        return QualityReport(ticker=ticker, gate="cross_source", issues=tuple(issues))

    # Correlation on log-returns (drops one index; matches series length).
    rets_a = _log_returns(closes_a)
    rets_b = _log_returns(closes_b)
    corr, reason, detail = _pearson_with_reason(rets_a, rets_b)

    if corr is None:
        # Issue #278: the previous implementation collapsed every
        # ``_pearson(...) is None`` return into one hardcoded "zero
        # variance" message, which was factually wrong for both the
        # length-mismatch case (leading-bad bar tripping ``n != len(ys)``)
        # and the NaN-gap case (a single bad close making one of the
        # returns NaN). The reason code distinguishes the three
        # failure modes; the message uses the precise detail so an
        # operator paging on ``XSC_SOURCE_MISSING`` can tell "MOEX
        # feed is stuck/flat" apart from "one bar arrived as NaN".
        if reason == "zero_variance":
            message = detail  # already names the flat side
        elif reason == "nan_gap":
            message = f"{detail} (one source has a bad close — leading, middle, or trailing)"
        else:  # length_mismatch
            message = (  # pragma: no cover — defensive branch: _log_returns
                # is now invariant-length (issue #278), so rets_a and
                # rets_b are guaranteed equal length. The branch is kept
                # so that any future refactor that introduces a length
                # divergence surfaces an accurate operator-facing message
                # instead of the pre-#278 misleading "zero variance"
                # string.
                f"return series length mismatch: A={len(rets_a)}, B={len(rets_b)} "
                f"({detail}) — possible leading-bad bar in one source"
            )
        issues.append(
            Issue.make(
                gate="cross_source",
                kind=IssueKind.XSC_SOURCE_MISSING,
                message=message,
                extra={
                    "reason": reason,
                    "len_a": len(rets_a),
                    "len_b": len(rets_b),
                },
            )
        )
        return QualityReport(ticker=ticker, gate="cross_source", issues=tuple(issues))

    if corr < p.correlation_min:
        issues.append(
            Issue.make(
                gate="cross_source",
                kind=IssueKind.XSC_CORRELATION_LOW,
                message=(
                    f"Pearson correlation {corr:.4f} < {p.correlation_min:.4f} "
                    f"on {len(common)} aligned dates"  # noqa: E501
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
            window_a = closes_a[i : i + win]  # noqa: E203
            window_b = closes_b[i : i + win]  # noqa: E203
            # Issue #271: guard against BOTH non-positive AND NaN closes.
            # Pre-fix the `c <= 0` test did not catch NaN (NaN <= 0 is
            # False), so a NaN bar leaked into ``math.log`` (producing
            # NaN) and the resulting ``abs(NaN - x)`` was NaN, which
            # never exceeded the threshold. The divergence issue was
            # silently skipped.
            if any(not (math.isfinite(c) and c > 0) for c in window_a + window_b):
                continue  # pragma: no cover — defensive: when any close
                # is bad, _log_returns surfaces a NaN-return and
                # _pearson_with_reason returns ("nan_gap", ...), so
                # check_cross_source returns early before reaching the
                # rolling divergence loop. The guard stays so that a
                # future refactor that decouples rolling divergence
                # from correlation does not regress to the pre-#271
                # silent NaN-skip defect.
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
                        "window_end": (max_window_end.isoformat() if max_window_end is not None else ""),  # noqa: E501
                    },
                )
            )

    return QualityReport(ticker=ticker, gate="cross_source", issues=tuple(issues))


__all__ = [
    "CrossSourceParams",
    "SourceSeries",
    "check_cross_source",
]
