"""
Alphard Data Quality Gate — Severity classification.

PURPOSE
-------
Deterministic, dependency-free severity classification for data quality issues.
A given (gate, code, context) tuple MUST always produce the same Severity enum
value — there is no LLM, no statistical inference, no randomness. This makes
audit logs replayable and regulators happy.

SEVERITY LADDER (lowest -> highest)
-----------------------------------
LOW      — informational; record and continue. No alert.
MEDIUM   — record, flag the row/ticker, continue. Log only.
HIGH     — skip the ticker / refresh window; Telegram alert. Do not trade.
CRITICAL — hard reject; HOLD all orders for this ticker; Telegram + email.

DESIGN DECISIONS
----------------
1. Pure stdlib + pydantic. Same constraint as src/risk/gate.py: the data
   quality gate is the LAST line of defence before signals reach the risk
   gate. Fewer deps = fewer supply-chain failure modes.

2. Severity is decided by a static lookup table (IssueKind -> Severity), NOT
   by threshold tuning inside the gate. The gate reports facts; this module
   classifies them. This means retuning severity means editing one table,
   not chasing if/elif branches.

3. Decisions are deterministic: identical Issue objects -> identical Severity.
   No timezone-dependent comparisons, no hash-randomised sets, no dict
   iteration order relied on for decisions.

4. Audit log: every issue carries (gate_name, code, severity, message, ts).
   Persistence is the caller's job (Postgres data_quality_events table is
   filled by src.data.quality.audit, not here).

WHAT IS NOT HERE
----------------
- No I/O, no logging config, no Postgres client. AuditLog is a thin
  protocol that the audit module wires to psycopg / sqlite / stdout.
- No threshold tuning (those live next to the checks that emit issues).
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    """
    Ordered severity ladder. Ordering matters because QualityReport aggregates
    by MAX(severity) — see QualityReport.worst_severity().
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @classmethod
    def worst(cls, *values: "Severity") -> "Severity | None":
        """Return the highest severity among the given values.

        Empty input -> None (no issues, no worst). Deterministic.
        """
        if not values:
            return None
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        max_idx = -1
        for v in values:
            try:
                idx = order.index(v)
            except ValueError:  # pragma: no cover — exhaustiveness guard
                continue
            if idx > max_idx:
                max_idx = idx
        if max_idx < 0:
            return None
        return order[max_idx]


# ---------------------------------------------------------------------------
# Issue catalog
# ---------------------------------------------------------------------------
#
# A static catalog keeps severity assignments auditable in one place.
# To change "what counts as CRITICAL" you edit this table, not the gate.
#
# Codes follow the pattern: <GATE>_<KIND>. The gate that emits the code
# is responsible for filling in the facts (counts, dates, ratios). This
# module only decides how bad those facts are.
# ---------------------------------------------------------------------------


class IssueKind(str, Enum):
    """Closed catalog of issue kinds the gates may emit."""

    # Level 1 — Ingestion Gate
    ING_MISSING_COLUMNS = "ING_MISSING_COLUMNS"
    ING_NULL_PRIMARY_KEY = "ING_NULL_PRIMARY_KEY"
    ING_RANGE_VIOLATION = "ING_RANGE_VIOLATION"
    ING_ZERO_OR_NEGATIVE_PRICE = "ING_ZERO_OR_NEGATIVE_PRICE"
    ING_NAN_PRICE = "ING_NAN_PRICE"
    ING_OUTLIER = "ING_OUTLIER"
    ING_COVERAGE_LOW = "ING_COVERAGE_LOW"
    ING_INSUFFICIENT_HISTORY = "ING_INSUFFICIENT_HISTORY"
    ING_STALE_DATA = "ING_STALE_DATA"
    ING_LARGE_GAP = "ING_LARGE_GAP"
    ING_LOW_VOLUME = "ING_LOW_VOLUME"

    # Level 2 — Cross-Source
    XSC_CORRELATION_LOW = "XSC_CORRELATION_LOW"
    XSC_DIVERGENCE_HIGH = "XSC_DIVERGENCE_HIGH"
    XSC_SOURCE_MISSING = "XSC_SOURCE_MISSING"

    # Level 3 — Historical
    HST_SPLIT_DETECTED = "HST_SPLIT_DETECTED"
    HST_SPLIT_UNADJUSTED = "HST_SPLIT_UNADJUSTED"
    HST_DELISTED = "HST_DELISTED"
    HST_FUTURE_ROW = "HST_FUTURE_ROW"


# Static severity table. Order of keys is irrelevant; lookup is O(1).
_ISSUE_SEVERITY: Final[dict[IssueKind, Severity]] = {
    # ---- CRITICAL: hard reject, HOLD orders ----
    # Missing columns means the schema is broken — refuse to even read.
    IssueKind.ING_MISSING_COLUMNS: Severity.CRITICAL,
    # NULL primary key means the index is corrupted — refuse to dedupe.
    IssueKind.ING_NULL_PRIMARY_KEY: Severity.CRITICAL,
    # NaN in OHLCV means a corrupted upstream feed — refuse to compute.
    IssueKind.ING_NAN_PRICE: Severity.CRITICAL,
    # Future-dated rows means clock skew or a data leak.
    IssueKind.HST_FUTURE_ROW: Severity.CRITICAL,
    # ---- HIGH: skip ticker, alert ----
    # Zero/negative price is never valid in a market-data feed.
    IssueKind.ING_ZERO_OR_NEGATIVE_PRICE: Severity.HIGH,
    # Range violation (high < low etc.) means corrupted row.
    IssueKind.ING_RANGE_VIOLATION: Severity.HIGH,
    # Coverage < 95% of expected trading days means gaps we can't trust.
    IssueKind.ING_COVERAGE_LOW: Severity.HIGH,
    # Stale data (>3 trading days) means the feed is broken.
    IssueKind.ING_STALE_DATA: Severity.HIGH,
    # <252 days history = not enough signal — same as research paper uses.
    IssueKind.ING_INSUFFICIENT_HISTORY: Severity.HIGH,
    # Cross-source divergence > 1% on a 5-day rolling window — possible split.
    IssueKind.XSC_DIVERGENCE_HIGH: Severity.HIGH,
    # Detected split that has NOT been adjusted in the source — refuse raw.
    IssueKind.HST_SPLIT_UNADJUSTED: Severity.HIGH,
    # Delisted ticker — last trading day flagged, future rows blocked.
    IssueKind.HST_DELISTED: Severity.HIGH,
    # No second source available at all — single-source blind.
    IssueKind.XSC_SOURCE_MISSING: Severity.HIGH,
    # ---- MEDIUM: use with flag ----
    # |z-score| > 6 but row passes range checks — possible corporate action.
    IssueKind.ING_OUTLIER: Severity.MEDIUM,
    # Pearson correlation between sources < 0.99 — possible phase shift.
    IssueKind.XSC_CORRELATION_LOW: Severity.MEDIUM,
    # Large gap (>5 trading days) — known exchange closure / halt / backfill miss.
    IssueKind.ING_LARGE_GAP: Severity.MEDIUM,
    # Split detected and ALREADY adjusted — informational for audit trail.
    IssueKind.HST_SPLIT_DETECTED: Severity.MEDIUM,
    # ---- LOW: log info only ----
    # > 10% zero-volume days — illiquid but not corrupted.
    IssueKind.ING_LOW_VOLUME: Severity.LOW,
}


def severity_for(kind: IssueKind) -> Severity:
    """Return the deterministic Severity for a given IssueKind.

    Raises KeyError if the kind is not in the catalog. This is intentional:
    it forces every new check to declare its severity at registration time,
    instead of silently defaulting to LOW.
    """
    return _ISSUE_SEVERITY[kind]


# ---------------------------------------------------------------------------
# Issue + Report models
# ---------------------------------------------------------------------------


class Issue(BaseModel):
    """A single finding from a quality gate.

    The kind decides severity (via severity_for()), but severity is also
    pinned into the model so audit-log readers don't need to consult the
    catalog to know what action was taken.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str = Field(min_length=1, max_length=32)
    kind: IssueKind
    severity: Severity
    message: str = Field(min_length=1, max_length=512)
    count: int = Field(default=0, ge=0)
    extra: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @classmethod
    def make(
        cls,
        *,
        gate: str,
        kind: IssueKind,
        message: str,
        count: int = 0,
        extra: dict[str, str | int | float | bool] | None = None,
    ) -> "Issue":
        """Build an Issue with severity pinned from the catalog."""
        return cls(
            gate=gate,
            kind=kind,
            severity=severity_for(kind),
            message=message,
            count=count,
            extra=extra or {},
        )


class QualityReport(BaseModel):
    """Aggregated result of running one or more gates on a ticker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(min_length=1, max_length=32)
    gate: str = Field(min_length=1, max_length=32)
    issues: tuple[Issue, ...] = Field(default_factory=tuple)

    def worst_severity(self) -> Severity | None:
        """Return the worst severity across all issues.

        CRITICAL > HIGH > MEDIUM > LOW. No issues -> None.
        Deterministic — same Issue set -> same Severity.
        """
        if not self.issues:
            return None
        return Severity.worst(*(i.severity for i in self.issues))

    @property
    def passed(self) -> bool:
        """A report passes only if there are zero issues."""
        return len(self.issues) == 0

    @property
    def rejected(self) -> bool:
        """A report rejects (hard fail) iff it has any CRITICAL issue."""
        return self.worst_severity() == Severity.CRITICAL

    @property
    def skipped(self) -> bool:
        """A report skips iff worst severity is HIGH (no CRITICAL)."""
        w = self.worst_severity()
        return w == Severity.HIGH

    def by_severity(self, sev: Severity) -> tuple[Issue, ...]:
        """Return issues at exactly this severity. Deterministic order."""
        return tuple(i for i in self.issues if i.severity == sev)