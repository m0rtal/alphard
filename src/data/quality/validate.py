"""OHLCV data-quality validators.

These run after every ``upsert_ohlcv`` call (inside the backfill
control loop) and as a standalone cron'd health check
(``scripts/validate_ohlcv.py``). The decision-making code treats
these bars as the primary signal, so a CRITICAL finding here MUST
block the upsert — silent garbage in = silent garbage out.

Three severity levels:
  - ``CRITICAL``  — bar is structurally invalid (high < low,
    negative volume, NaN). Never accepted. The backfill loop
    rejects the entire ``rows`` payload and skips the upsert.
  - ``WARNING``   — bar is structurally valid but suspicious
    (return > 50% in one day, > 5 consecutive calendar-day gaps).
    Logged and recorded but does not block the upsert.
  - ``INFO``      — operational notes (e.g. ticker has < 30
    bars total — surface for the user to decide if it matters).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.models import OHLCVRow


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    ticker: str
    ts: date
    code: str  # short machine-readable, e.g. "high_lt_low"
    detail: str  # human-readable, e.g. "high=10.00 low=12.00"

    def is_blocking(self) -> bool:
        """CRITICAL issues must block the upsert; WARNING/INFO must not."""
        return self.severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# Single-bar invariants
# ---------------------------------------------------------------------------


def validate_bar(row: "OHLCVRow") -> list[Issue]:
    """Check one OHLCV row for structural validity.

    Rules (all CRITICAL):
      - high >= max(open, close)
      - low  <= min(open, close)
      - high >= low
      - volume >= 0
      - open / high / low / close are positive (Tinkoff can emit
        zero/negative prices only for delisted/zero-lot tickers;
        treat as CRITICAL and let the caller decide).
    """
    issues: list[Issue] = []
    o, h, l, c, v = row.open, row.high, row.low, row.close, row.volume

    if h < l:
        issues.append(
            Issue(
                Severity.CRITICAL,
                row.ticker,
                row.ts,
                "high_lt_low",
                f"high={h} low={l}",
            )
        )
    if l > o:
        issues.append(Issue(Severity.CRITICAL, row.ticker, row.ts, "low_gt_open", f"low={l} open={o}"))
    if l > c:
        issues.append(Issue(Severity.CRITICAL, row.ticker, row.ts, "low_gt_close", f"low={l} close={c}"))
    if h < o:
        issues.append(Issue(Severity.CRITICAL, row.ticker, row.ts, "high_lt_open", f"high={h} open={o}"))
    if h < c:
        issues.append(Issue(Severity.CRITICAL, row.ticker, row.ts, "high_lt_close", f"high={h} close={c}"))
    if v < 0:
        issues.append(Issue(Severity.CRITICAL, row.ticker, row.ts, "neg_volume", f"volume={v}"))
    for name, val in (("open", o), ("high", h), ("low", l), ("close", c)):
        if val <= 0:
            issues.append(
                Issue(
                    Severity.CRITICAL,
                    row.ticker,
                    row.ts,
                    f"non_positive_{name}",
                    f"{name}={val}",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Series-level invariants
# ---------------------------------------------------------------------------


def validate_series(rows: list["OHLCVRow"]) -> list[Issue]:
    """Cross-bar checks: temporal continuity, daily-return outliers.

    Pre-condition: ``rows`` is sorted by ts ascending. The backfill
    upsert path must enforce this ordering before calling.
    """
    issues: list[Issue] = []
    if not rows:
        return issues

    # 1) Outlier daily move > 50% absolute return — likely a stock
    #    split the upstream archive didn't account for, or a price
    #    glitch. WARN so the operator can investigate.
    for prev, cur in zip(rows, rows[1:]):
        if prev.close <= 0 or cur.ts <= prev.ts:
            continue
        try:
            ret = (cur.close - prev.close) / prev.close
        except ArithmeticError:
            continue
        if abs(ret) > Decimal("0.5"):
            issues.append(
                Issue(
                    Severity.WARNING,
                    cur.ticker,
                    cur.ts,
                    "return_gt_50pct",
                    f"prev_close={prev.close} cur_close={cur.close} ret={ret:.2%}",
                )
            )

    # 2) Gap detection: calendar-day gap > 5 trading days = ~7
    #    calendar days (weekends + holidays) between sessions.
    #    Anything > 14 calendar days is suspicious for a Russian
    #    liquid share.
    prev_ts = rows[0].ts
    for cur in rows[1:]:
        delta = (cur.ts - prev_ts).days
        if delta > 14:
            issues.append(
                Issue(
                    Severity.WARNING,
                    cur.ticker,
                    cur.ts,
                    "long_gap",
                    f"prev_ts={prev_ts} gap_days={delta}",
                )
            )
        prev_ts = cur.ts

    return issues


# ---------------------------------------------------------------------------
# Aggregate reporting
# ---------------------------------------------------------------------------


def summarize(issues: Iterable[Issue]) -> dict[str, int]:
    """Bucket issue counts by severity for log output."""
    out = {s.value: 0 for s in Severity}
    for i in issues:
        out[i.severity.value] += 1
    return out


def blocking(issues: Iterable[Issue]) -> list[Issue]:
    """Filter to only those issues that should reject an upsert."""
    return [i for i in issues if i.is_blocking()]


def worst_tickers(issues: Iterable[Issue], *, limit: int = 10) -> list[tuple[str, int]]:
    """Group by ticker, sort by issue count desc — surfaces the
    tickers that need the operator's attention most."""
    counts: dict[str, int] = {}
    for i in issues:
        counts[i.ticker] = counts.get(i.ticker, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
