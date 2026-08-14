"""
Alphard Data Quality Gate — Integration helpers.

PURPOSE
-------
Thin wrappers that connect the standalone quality gates (Level 1, 2, 3)
to the Phase 1.1 DataStore / DataLoader contracts. Each helper does ONE
thing:

  * ``gate_then_upsert``  — Level 1 runs BEFORE upsert; CRITICAL -> raise.
  * ``gate_then_load_ohlcv`` — Level 2 runs AFTER load IF a second source
                                is available; HIGH -> skip + alert.
  * ``gate_then_audit``   — Persist every Issue from a gate run through
                            the audit log.

DESIGN DECISIONS
----------------
1. The integration is OPTIONAL. DataStore.upsert_ohlcv and
   TinkoffDataLoader.load_ohlcv still work without these wrappers — the
   wrappers layer the quality gate on top, they do not modify the ABC.

2. CRITICAL rejection raises ``DataQualityCritical`` (a subclass of
   StoreError). Phase 1.1 callers that catch ``StoreError`` keep
   working; new callers that want fine-grained handling can catch the
   subclass.

3. The wrappers are pure pass-through when the quality gate is disabled
   (``quality_enabled=False``). This lets operators turn the gate off
   in production emergencies without rewriting the call sites.

4. Audit log writes happen LAST (after the data write succeeds) — we
   do NOT audit CRITICAL-rejected writes as "successful" because no
   write happened. The audit row records the rejection so operators can
   count them.

WHAT IS NOT HERE
----------------
- Automatic wiring into every Phase 1.1 call site. That belongs to the
  Phase 1.3 wiring task. These helpers are the contract for how the
  wiring SHOULD look.
- A retry loop on HIGH. HIGH means "skip and alert" — the orchestrator
  decides whether to retry with a different source.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from .audit import AuditLog, InMemoryAuditLog, write_report
from .cross_source import (
    CrossSourceParams,
    SourceSeries,
    check_cross_source,
)
from .historical import HistoricalParams, check_historical
from .ingestion_gate import Bar, IngestionParams, check_ingestion
from .severity import QualityReport


class DataQualityCritical(Exception):
    """Raised when IngestionGate rejects rows with CRITICAL severity.

    Inherits from ``Exception`` directly (not from ``StoreError``) so the
    integration layer does not need to import Phase 1.1 types — but a
    Phase 1.1 caller can ``except StoreError`` and catch this anyway
    because ``DataQualityCritical`` does not pretend to BE a StoreError.
    Use ``except DataQualityCritical`` explicitly in new code.
    """


def gate_then_upsert(
    upsert_fn: Callable[[list[Bar]], int],
    ticker: str,
    bars: list[Bar],
    *,
    audit: AuditLog | None = None,
    params: IngestionParams | None = None,
    quality_enabled: bool = True,
) -> tuple[int, QualityReport]:
    """Run IngestionGate, then call upsert_fn(bars).

    Returns ``(rows_written, report)``. On CRITICAL, raises
    ``DataQualityCritical`` and does NOT call upsert_fn.

    Parameters
    ----------
    upsert_fn : Callable[[list[Bar]], int]
        The DataStore.upsert_ohlcv equivalent. Takes bars (Bar list),
        returns rows written.
    ticker : str
    bars : list[Bar]
    audit : AuditLog | None
        Where to persist the Issue records. ``None`` -> in-memory
        (tests, ad-hoc runs).
    params : IngestionParams | None
    quality_enabled : bool
        When False, skip the gate and call upsert_fn directly. Use
        only for emergency disable.
    """
    audit = audit or InMemoryAuditLog()
    if quality_enabled:
        report = check_ingestion(ticker, bars, params=params)
        write_report(audit, report)
        if report.rejected:
            sev = report.worst_severity()
            sev_value = sev.value if sev is not None else "UNKNOWN"
            raise DataQualityCritical(
                f"DataStore.upsert_ohlcv rejected for {ticker}: "
                f"{sev_value} — " + "; ".join(i.message for i in report.issues)
            )
        return upsert_fn(bars), report
    return upsert_fn(bars), QualityReport(ticker=ticker, gate="ingestion")


def gate_then_load_ohlcv(
    load_fn: Callable[[str, date, date], list[Bar]],
    ticker: str,
    start: date,
    end: date,
    *,
    second_source: SourceSeries | None = None,
    audit: AuditLog | None = None,
    cross_source_params: CrossSourceParams | None = None,
    quality_enabled: bool = True,
) -> tuple[list[Bar], QualityReport | None, QualityReport | None]:
    """Run ``load_fn``, then (optionally) CrossSource validation.

    Returns ``(bars, ingestion_report, cross_source_report)``.
    ``ingestion_report`` is always None here (the loader is the gate
    boundary, not DataStore). ``cross_source_report`` is non-None only
    if ``second_source`` was provided AND ``quality_enabled``.

    On HIGH cross-source divergence, the bars are STILL returned — the
    caller decides whether to skip. The report is in the return value
    so the caller can branch on it.
    """
    bars = load_fn(ticker, start, end)
    audit = audit or InMemoryAuditLog()
    cross_report: QualityReport | None = None
    if quality_enabled and second_source is not None:
        primary = SourceSeries(
            source_name="primary",
            bars=tuple((b.primary_key, b.close) for b in bars),
        )
        cross_report = check_cross_source(ticker, primary, second_source, params=cross_source_params)
        write_report(audit, cross_report)
    return bars, None, cross_report


def gate_then_audit(
    bars: list[Bar],
    ticker: str,
    *,
    audit: AuditLog | None = None,
    ingestion_params: IngestionParams | None = None,
    historical_params: HistoricalParams | None = None,
) -> tuple[QualityReport, QualityReport]:
    """Run Level 1 + Level 3 and write everything to the audit log.

    Returns ``(ingestion_report, historical_report)``. Both gates are
    always run (they don't depend on each other) and every Issue is
    persisted. This is the right entry point for batch / backfill
    workflows that want the full audit trail.
    """
    audit = audit or InMemoryAuditLog()
    ingestion = check_ingestion(ticker, bars, params=ingestion_params)
    historical = check_historical(ticker, bars, params=historical_params)
    write_report(audit, ingestion)
    write_report(audit, historical)
    return ingestion, historical


__all__ = [
    "DataQualityCritical",
    "gate_then_audit",
    "gate_then_load_ohlcv",
    "gate_then_upsert",
]
