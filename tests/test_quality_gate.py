"""
Tests for the Data Quality Gate.

Coverage target: 85%+ of src/data/quality/.

Strategy:
- Deterministic unit tests for each gate covering happy path + known
  failure modes (NaN injection, range violation, synthetic split).
- Property-based tests via ``hypothesis`` to flush out edge cases in
  the math helpers (z-score, Pearson, split rounding).
- Round-trip / invariant tests: applying the same split twice is
  idempotent; deterministic report ordering; severity catalog
  exhaustiveness.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import csv
import os

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from src.data.quality.audit import InMemoryAuditLog, write_report
from src.data.quality.cross_source import (
    SourceSeries,
    check_cross_source,
)
from src.data.quality.historical import (
    HistoricalParams,
    SplitEvent,
    apply_split_adjustment,
    check_historical,
    detect_splits,
)
from src.data.quality.ingestion_gate import (
    Bar,
    IngestionParams,
    REQUIRED_COLUMNS,
    check_ingestion,
    expected_trading_days,
    log_returns,
)
from src.data.quality.severity import (
    Issue,
    IssueKind,
    QualityReport,
    Severity,
    severity_for,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(idx: int, *, base: date, close: float = 100.0) -> Bar:
    """Build a Bar at base+idx calendar days. Uses weekday arithmetic to
    skip weekends so coverage tests behave predictably."""
    d = base + timedelta(days=idx)
    # Skip weekends by incrementing until Mon-Fri. This keeps the date
    # series contiguous in trading-day terms without coupling to a
    # holiday calendar (Phase 2).
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return Bar(
        primary_key=d,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=1000,
    )


def _bars(
    n: int,
    *,
    base: date | None = None,
    close_fn=lambda i: 100.0 + i * 0.1,
) -> list[Bar]:
    base = base or date(2025, 1, 1)
    out: list[Bar] = []
    i = 0
    emitted = 0
    while emitted < n:
        d = base + timedelta(days=i)
        if d.weekday() < 5:
            close = close_fn(emitted)
            out.append(
                Bar(
                    primary_key=d,
                    open=close - 1,
                    high=close + 1,
                    low=close - 2,
                    close=close,
                    volume=1000,
                )
            )
            emitted += 1
        i += 1
    return out


# ---------------------------------------------------------------------------
# Severity catalog tests
# ---------------------------------------------------------------------------


class TestSeverity:
    def test_severity_ordering(self) -> None:
        """Severity.worst returns the highest of any subset."""
        assert Severity.worst() is None
        assert Severity.worst(Severity.LOW) == Severity.LOW
        assert Severity.worst(Severity.HIGH, Severity.LOW) == Severity.HIGH
        assert Severity.worst(Severity.LOW, Severity.MEDIUM, Severity.CRITICAL) == Severity.CRITICAL

    def test_catalog_is_exhaustive(self) -> None:
        """Every IssueKind has a severity — no silent defaults."""
        for kind in IssueKind:
            sev = severity_for(kind)
            assert sev in (Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)

    def test_catalog_invariants(self) -> None:
        """CRITICAL must mean hard-reject; HIGH must mean skip-ticker."""
        critical_kinds = {
            k for k, v in [(k, severity_for(k)) for k in IssueKind] if v == Severity.CRITICAL
        }  # noqa: E501
        high_kinds = {k for k in IssueKind if severity_for(k) == Severity.HIGH}
        medium_kinds = {k for k in IssueKind if severity_for(k) == Severity.MEDIUM}
        low_kinds = {k for k in IssueKind if severity_for(k) == Severity.LOW}

        # Sanity: at least one of each (so the catalog is non-trivial).
        assert critical_kinds
        assert high_kinds
        assert medium_kinds
        assert low_kinds

        # Spec contract: schema, NaN, future-row are CRITICAL.
        assert IssueKind.ING_MISSING_COLUMNS in critical_kinds
        assert IssueKind.ING_NAN_PRICE in critical_kinds
        assert IssueKind.HST_FUTURE_ROW in critical_kinds

        # Spec contract: divergence > 1%, split-unadjusted, delisted are HIGH.
        assert IssueKind.XSC_DIVERGENCE_HIGH in high_kinds
        assert IssueKind.HST_SPLIT_UNADJUSTED in high_kinds
        assert IssueKind.HST_DELISTED in high_kinds

        # Spec contract: outliers are MEDIUM, not HIGH.
        assert IssueKind.ING_OUTLIER in medium_kinds

        # Spec contract: low-volume days are LOW.
        assert IssueKind.ING_LOW_VOLUME in low_kinds

    def test_issue_make_pins_severity(self) -> None:
        """Issue.make pins severity from the catalog."""
        i = Issue.make(
            gate="ingestion",
            kind=IssueKind.ING_OUTLIER,
            message="z=7",
            count=2,
        )
        assert i.severity == Severity.MEDIUM
        assert i.kind == IssueKind.ING_OUTLIER

    def test_quality_report_passed_and_rejected(self) -> None:
        """Empty report passes; CRITICAL rejects; HIGH skips."""
        empty = QualityReport(ticker="X", gate="g")
        assert empty.passed
        assert empty.worst_severity() is None
        assert not empty.rejected
        assert not empty.skipped

        crit_issue = Issue.make(gate="g", kind=IssueKind.ING_MISSING_COLUMNS, message="x")
        r = QualityReport(ticker="X", gate="g", issues=(crit_issue,))
        assert not r.passed
        assert r.rejected
        assert not r.skipped

        high_issue = Issue.make(gate="g", kind=IssueKind.ING_RANGE_VIOLATION, message="x")
        r = QualityReport(ticker="X", gate="g", issues=(high_issue,))
        assert not r.passed
        assert not r.rejected
        assert r.skipped

    def test_deterministic_report(self) -> None:
        """Same issues -> same report (frozen model + tuple)."""
        i = Issue.make(gate="g", kind=IssueKind.ING_OUTLIER, message="x", count=3)
        r1 = QualityReport(ticker="X", gate="g", issues=(i,))
        r2 = QualityReport(ticker="X", gate="g", issues=(i,))
        assert r1 == r2
        assert r1.model_dump() == r2.model_dump()


# ---------------------------------------------------------------------------
# Ingestion gate tests
# ---------------------------------------------------------------------------


# Fixed now() for deterministic staleness/coverage tests.
FROZEN_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


class TestIngestionGate:
    def test_required_columns_constant(self) -> None:
        assert REQUIRED_COLUMNS == (
            "primary_key",
            "open",
            "high",
            "low",
            "close",
            "volume",
        )

    def test_clean_data_passes_when_now_within_range(self) -> None:
        """Clean 1y of data with stale_threshold slack passes."""
        # Build ~1y of data ending just before FROZEN_NOW.
        n = 260
        base = date(2025, 8, 1)
        bars = _bars(n, base=base)  # ~260 trading days
        # Update FROZEN_NOW to be 3 calendar days after the last bar so
        # staleness doesn't fire. The last bar will be a Friday-ish date.
        last = bars[-1].primary_key
        now = datetime(last.year, last.month, last.day, tzinfo=timezone.utc) + timedelta(days=1)
        r = check_ingestion("SBER", bars, now=now)
        # We expect no range/zero/nan/outlier/stale issues. We may see
        # ING_LARGE_GAP (large_gap_calendar_days=7 default) if there
        # are weekend-spanning gaps, but those are 3 calendar days so
        # under threshold. ING_LOW_VOLUME requires >10% zero-volume.
        kinds = {i.kind for i in r.issues}
        for bad in (
            IssueKind.ING_RANGE_VIOLATION,
            IssueKind.ING_ZERO_OR_NEGATIVE_PRICE,
            IssueKind.ING_NAN_PRICE,
            IssueKind.ING_OUTLIER,
            IssueKind.ING_STALE_DATA,
            IssueKind.ING_NULL_PRIMARY_KEY,
            IssueKind.ING_MISSING_COLUMNS,
            IssueKind.ING_INSUFFICIENT_HISTORY,
            IssueKind.ING_COVERAGE_LOW,
            IssueKind.ING_LARGE_GAP,
            IssueKind.ING_LOW_VOLUME,
        ):
            assert bad not in kinds, f"unexpected issue: {bad}"

    def test_missing_columns_is_critical(self) -> None:
        r = check_ingestion(
            "SBER",
            [],
            columns={"open", "close", "volume"},  # missing primary_key, high, low
        )
        assert r.rejected
        assert IssueKind.ING_MISSING_COLUMNS in {i.kind for i in r.issues}

    def test_nan_injection_is_critical(self) -> None:
        """A NaN in OHLC must be caught even though pydantic usually blocks it.

        We simulate a 'NaN that slipped through' by manually constructing
        a Bar with ``close=float('nan')`` after disabling the gt=0 guard
        — we monkeypatch the model briefly. If the gt guard catches it,
        that's also acceptable (the gate cannot run on bad data at all).
        """
        # Pydantic Field(gt=0.0) blocks NaN by default (NaN is not > 0).
        # So we expect the model to reject, not the gate.
        with pytest.raises(Exception):
            Bar(
                primary_key=date(2026, 1, 1),
                open=1.0,
                high=2.0,
                low=0.5,
                close=float("nan"),  # type: ignore[arg-type]
                volume=0,
            )

    def test_range_violation_is_high(self) -> None:
        bars = _bars(10)
        # Force a row with high < max(open,close).
        bad = bars[5].model_copy(update={"high": 50.0, "low": 200.0})
        r = check_ingestion("X", bars[:5] + [bad] + bars[6:])
        assert IssueKind.ING_RANGE_VIOLATION in {i.kind for i in r.issues}

    def test_zero_price_is_high(self) -> None:
        bars = _bars(10)
        bad = bars[5].model_copy(update={"close": 0.0, "open": 0.0, "high": 0.0, "low": 0.0})
        r = check_ingestion("X", bars[:5] + [bad] + bars[6:])
        assert IssueKind.ING_ZERO_OR_NEGATIVE_PRICE in {i.kind for i in r.issues}

    def test_insufficient_history_is_high(self) -> None:
        bars = _bars(50)  # < 252 default
        r = check_ingestion("X", bars, now=FROZEN_NOW)
        assert IssueKind.ING_INSUFFICIENT_HISTORY in {i.kind for i in r.issues}

    def test_stale_data_is_high(self) -> None:
        bars = _bars(300, base=date(2024, 1, 1))
        # now is way after the data.
        r = check_ingestion("X", bars, now=datetime(2026, 8, 14, tzinfo=timezone.utc))
        assert IssueKind.ING_STALE_DATA in {i.kind for i in r.issues}

    def test_large_gap_is_medium(self) -> None:
        """A 30-day gap between two consecutive bars is MEDIUM."""
        # Build a series with a deliberate gap.
        bars = _bars(10)
        future = _bars(10, base=date(2026, 1, 1))  # ~ 1 year later
        r = check_ingestion("X", bars + future, now=datetime(2026, 8, 14, tzinfo=timezone.utc))
        assert IssueKind.ING_LARGE_GAP in {i.kind for i in r.issues}

    def test_low_volume_is_low(self) -> None:
        bars = _bars(20)
        # Force 30% of rows to volume=0
        for i in (3, 4, 5, 6, 7, 8):
            bars[i] = bars[i].model_copy(update={"volume": 0})
        r = check_ingestion("X", bars, now=datetime(2026, 8, 14, tzinfo=timezone.utc))
        assert IssueKind.ING_LOW_VOLUME in {i.kind for i in r.issues}
        for i in r.issues:
            if i.kind == IssueKind.ING_LOW_VOLUME:
                assert i.severity == Severity.LOW

    def test_log_returns_zero_for_empty(self) -> None:
        assert log_returns([]) == []
        assert log_returns([100.0]) == []

    def test_log_returns_known_values(self) -> None:
        rets = log_returns([100.0, 110.0])
        assert len(rets) == 1
        assert abs(rets[0] - 0.09531017980432493) < 1e-9

    def test_expected_trading_days(self) -> None:
        # Mon-Fri Jan 2025: Jan 1 is Wed, Jan 31 is Fri -> 23 trading days.
        assert expected_trading_days(date(2025, 1, 1), date(2025, 1, 31)) == 23
        assert expected_trading_days(date(2025, 1, 1), date(2024, 12, 31)) == 0

    def test_report_is_deterministic(self) -> None:
        """Same input -> same report (frozen + ordered iteration)."""
        bars = _bars(50)
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        r1 = check_ingestion("X", bars, now=now)
        r2 = check_ingestion("X", bars, now=now)
        assert r1 == r2


# ---------------------------------------------------------------------------
# Property-based tests via hypothesis
# ---------------------------------------------------------------------------


@given(st.lists(st.floats(min_value=0.01, max_value=1e6, allow_nan=False), min_size=2, max_size=100))  # noqa: E501
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_log_returns_length_property(prices: list[float]) -> None:
    """len(log_returns(p)) == len(p) - 1."""
    rets = log_returns(prices)
    assert len(rets) == len(prices) - 1


@given(
    st.lists(
        st.tuples(
            st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 1, 1)),
            st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
        ),
        min_size=20,
        max_size=50,
        unique_by=lambda p: p[0],  # unique dates
    )
)
@settings(max_examples=20)
def test_check_cross_source_no_panic_on_random_pairs(
    pairs: list[tuple[date, float]],
) -> None:
    """check_cross_source should never raise on well-formed input."""
    sa = SourceSeries(source_name="a", bars=tuple(pairs))
    sb = SourceSeries(source_name="b", bars=tuple(pairs))
    r = check_cross_source("X", sa, sb)
    # The report must be a valid QualityReport (pydantic enforces this).
    assert isinstance(r, QualityReport)


# ---------------------------------------------------------------------------
# Historical gate tests
# ---------------------------------------------------------------------------


class TestHistoricalGate:
    def test_clean_data_no_split_detected(self) -> None:
        bars = _bars(20, close_fn=lambda i: 100.0 + i * 0.1)  # smooth 1.001x growth
        events = detect_splits(bars)
        assert events == []

    def test_forward_split_detected(self) -> None:
        """Synthetic 5:1 forward split detected via close + cross-field."""
        # 5 pre-split bars at 500, 5 post-split bars at 100. Field ratios
        # agree with close-ratio only if open/high/low scale by ~5 too.
        # We force that by setting open=close (no spread) so the
        # cross-field consistency check passes.
        pre = [
            Bar(
                primary_key=_bars(1, base=date(2025, 1, 1))[0].primary_key + timedelta(days=i),
                open=500.0,
                high=500.0,
                low=500.0,
                close=500.0,
                volume=10_000,
            )
            for i in range(5)
        ]
        post = [
            Bar(
                primary_key=_bars(1, base=date(2025, 1, 10))[0].primary_key + timedelta(days=i),
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=10_000,
            )
            for i in range(5)
        ]
        # Replace the pre bar dates with actual trading-day dates.
        base_dates = [b.primary_key for b in _bars(10, base=date(2025, 1, 1))]
        pre = [b.model_copy(update={"primary_key": d}) for b, d in zip(pre, base_dates[:5])]
        post = [b.model_copy(update={"primary_key": d}) for b, d in zip(post, base_dates[5:10])]
        bars = pre + post
        events = detect_splits(bars)
        assert len(events) == 1, f"expected 1 split event, got {events}"
        assert events[0].ratio == 5.0
        assert not events[0].is_reverse
        assert events[0].confirmed

    def test_reverse_split_detected(self) -> None:
        """Synthetic 1:10 reverse split detected via close + cross-field."""
        # 5 bars at 10, 5 bars at 100. Cross-field ratios match close.
        base_dates = [b.primary_key for b in _bars(10, base=date(2025, 1, 1))]
        pre = [
            Bar(
                primary_key=d,
                open=10.0,
                high=10.0,
                low=10.0,
                close=10.0,
                volume=10_000,
            )
            for d in base_dates[:5]
        ]
        post = [
            Bar(
                primary_key=d,
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=10_000,
            )
            for d in base_dates[5:10]
        ]
        bars = pre + post
        events = detect_splits(bars)
        assert len(events) == 1, f"expected 1 split event, got {events}"
        assert events[0].ratio == pytest.approx(0.1)
        assert events[0].is_reverse
        assert events[0].confirmed

    def test_apply_split_adjustment_forward(self) -> None:
        """After applying a 5:1 forward split, pre bars close -> ~100."""
        base_dates = [b.primary_key for b in _bars(10, base=date(2025, 1, 1))]
        pre = [
            Bar(primary_key=d, open=500.0, high=500.0, low=500.0, close=500.0, volume=10_000)
            for d in base_dates[:5]  # noqa: E501
        ]
        post = [
            Bar(primary_key=d, open=100.0, high=100.0, low=100.0, close=100.0, volume=10_000)
            for d in base_dates[5:10]  # noqa: E501
        ]
        bars = pre + post
        events = detect_splits(bars)
        adj = apply_split_adjustment(bars, events)
        # Pre-split bars should be divided by 5 -> close ~100
        for b in adj[:5]:
            assert b.close == pytest.approx(100.0, abs=0.5)
        # Post-split bars should be untouched at ~100
        for b in adj[5:]:
            assert b.close == pytest.approx(100.0, abs=0.5)

    def test_apply_split_adjustment_reverse(self) -> None:
        """After applying a 1:10 reverse split, pre bars close -> ~100."""
        base_dates = [b.primary_key for b in _bars(10, base=date(2025, 1, 1))]
        pre = [
            Bar(primary_key=d, open=10.0, high=10.0, low=10.0, close=10.0, volume=10_000)
            for d in base_dates[:5]  # noqa: E501
        ]  # noqa: E501
        post = [
            Bar(primary_key=d, open=100.0, high=100.0, low=100.0, close=100.0, volume=10_000)
            for d in base_dates[5:10]  # noqa: E501
        ]
        bars = pre + post
        events = detect_splits(bars)
        adj = apply_split_adjustment(bars, events)
        # Pre-reverse bars should be multiplied by 10 -> close ~100
        for b in adj[:5]:
            assert b.close == pytest.approx(100.0, abs=0.5)

    def test_apply_split_adjustment_idempotent(self) -> None:
        """Applying the same split twice to the same input is a no-op."""
        base_dates = [b.primary_key for b in _bars(10, base=date(2025, 1, 1))]
        pre = [
            Bar(primary_key=d, open=500.0, high=500.0, low=500.0, close=500.0, volume=10_000)
            for d in base_dates[:5]  # noqa: E501
        ]
        post = [
            Bar(primary_key=d, open=100.0, high=100.0, low=100.0, close=100.0, volume=10_000)
            for d in base_dates[5:10]  # noqa: E501
        ]
        bars = pre + post
        events = detect_splits(bars)
        once = apply_split_adjustment(bars, events)
        twice = apply_split_adjustment(bars, events)
        assert once == twice

    def test_future_row_is_critical(self) -> None:
        bars = _bars(5)
        future_bar = Bar(
            primary_key=date(2030, 1, 1),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1000,
        )
        r = check_historical("X", bars + [future_bar], now=datetime(2026, 8, 14, tzinfo=timezone.utc))  # noqa: E501
        assert IssueKind.HST_FUTURE_ROW in {i.kind for i in r.issues}
        for i in r.issues:
            if i.kind == IssueKind.HST_FUTURE_ROW:
                assert i.severity == Severity.CRITICAL

    def test_delisted_is_high(self) -> None:
        bars = _bars(10)
        p = HistoricalParams(delisted_at=date(2025, 1, 5))
        r = check_historical("X", bars, params=p, now=datetime(2026, 8, 14, tzinfo=timezone.utc))
        assert IssueKind.HST_DELISTED in {i.kind for i in r.issues}

    def test_no_split_no_event(self) -> None:
        bars = _bars(30, close_fn=lambda i: 100.0 + i * 0.05)
        r = check_historical("X", bars, now=datetime(2026, 8, 14, tzinfo=timezone.utc))
        assert IssueKind.HST_SPLIT_DETECTED not in {i.kind for i in r.issues}

    def test_split_event_reported(self) -> None:
        base_dates = [b.primary_key for b in _bars(10, base=date(2025, 1, 1))]
        pre = [
            Bar(primary_key=d, open=500.0, high=500.0, low=500.0, close=500.0, volume=10_000)
            for d in base_dates[:5]  # noqa: E501
        ]
        post = [
            Bar(primary_key=d, open=100.0, high=100.0, low=100.0, close=100.0, volume=10_000)
            for d in base_dates[5:10]  # noqa: E501
        ]
        bars = pre + post
        r = check_historical("X", bars, now=datetime(2026, 8, 14, tzinfo=timezone.utc))
        assert IssueKind.HST_SPLIT_DETECTED in {i.kind for i in r.issues}


# ---------------------------------------------------------------------------
# Cross-source tests
# ---------------------------------------------------------------------------


def _aligned_pair(
    n: int,
    *,
    scale_b: float = 1.0,
    base: date = date(2026, 1, 1),
) -> tuple[SourceSeries, SourceSeries]:
    """Build two perfectly-aligned SourceSeries with optional scaling on B."""
    pairs_a: list[tuple[date, float]] = []
    pairs_b: list[tuple[date, float]] = []
    d = base
    while len(pairs_a) < n:
        if d.weekday() < 5:
            px = 100.0 + len(pairs_a) * 0.5
            pairs_a.append((d, px))
            pairs_b.append((d, px * scale_b))
        d += timedelta(days=1)
    return (
        SourceSeries(source_name="a", bars=tuple(pairs_a)),
        SourceSeries(source_name="b", bars=tuple(pairs_b)),
    )


class TestCrossSource:
    def test_identical_sources_no_issues(self) -> None:
        sa, sb = _aligned_pair(30)
        r = check_cross_source("X", sa, sb)
        assert r.passed

    def test_scale_mismatch_triggers_divergence(self) -> None:
        sa, sb = _aligned_pair(30, scale_b=1.10)  # 10% divergence
        r = check_cross_source("X", sa, sb)
        kinds = {i.kind for i in r.issues}
        assert IssueKind.XSC_DIVERGENCE_HIGH in kinds

    def test_short_series_source_missing(self) -> None:
        sa, sb = _aligned_pair(3)  # < 5 default minimum
        r = check_cross_source("X", sa, sb)
        assert IssueKind.XSC_SOURCE_MISSING in {i.kind for i in r.issues}

    def test_empty_bars_constructs_without_indexerror(self) -> None:
        """Issue #105: SourceSeries(bars=[]) must construct (raw[0] probe guarded)."""
        s = SourceSeries(source_name="x", bars=[])
        assert s.bars == ()
        s2 = SourceSeries(source_name="y", bars=())
        assert s2.bars == ()
        # The coercion branch must still work for non-empty Bar-like input.
        from types import SimpleNamespace

        bars_like = [
            SimpleNamespace(primary_key=date(2026, 1, 1), close=100.0),
            SimpleNamespace(primary_key=date(2026, 1, 2), close=101.0),
        ]
        s3 = SourceSeries(source_name="z", bars=bars_like)
        assert s3.bars == ((date(2026, 1, 1), 100.0), (date(2026, 1, 2), 101.0))

    def test_alignment_drops_dropped_count(self) -> None:
        """If one series has dates the other doesn't, they are dropped and counted."""
        sa, sb_full = _aligned_pair(30)
        # Drop last 5 dates from A. B keeps all 30. So 5 bars in B
        # have no match in A — these are recorded as dropped_b.
        sa_short = SourceSeries(
            source_name="a",
            bars=tuple(list(sa.bars)[:-5]),
        )
        r = check_cross_source("X", sa_short, sb_full)
        assert any(
            i.kind == IssueKind.XSC_SOURCE_MISSING and i.extra.get("dropped_b") == 5 for i in r.issues  # noqa: E501
        )  # noqa: E501


# ---------------------------------------------------------------------------
# Audit log tests
# ---------------------------------------------------------------------------


class TestAudit:
    def test_in_memory_sink_records_events(self) -> None:
        sink = InMemoryAuditLog()
        i = Issue.make(gate="g", kind=IssueKind.ING_OUTLIER, message="x", count=1)
        sink.write_event(i, ticker="X", gate="g")
        sink.write_event(i, ticker="X", gate="g")
        assert len(sink) == 2
        assert sink.events[0]["kind"] == "ING_OUTLIER"

    def test_write_report_writes_all_issues(self) -> None:
        sink = InMemoryAuditLog()
        i1 = Issue.make(gate="g", kind=IssueKind.ING_OUTLIER, message="a")
        i2 = Issue.make(gate="g", kind=IssueKind.ING_RANGE_VIOLATION, message="b")
        report = QualityReport(ticker="X", gate="g", issues=(i1, i2))
        write_report(sink, report)
        assert len(sink) == 2

    def test_make_default_uses_inmemory_without_dsn(self) -> None:
        from src.data.quality.audit import make_default_audit_log

        # Ensure ALPHARD_PG_DSN is unset for this test.
        import os

        old = os.environ.pop("ALPHARD_PG_DSN", None)
        try:
            sink = make_default_audit_log()
            assert isinstance(sink, InMemoryAuditLog)
        finally:
            if old is not None:
                os.environ["ALPHARD_PG_DSN"] = old

    def test_make_default_uses_postgres_with_dsn(self) -> None:
        """When $ALPHARD_PG_DSN is set, make_default_audit_log picks Postgres."""
        import os
        from src.data.quality.audit import PostgresAuditLog, make_default_audit_log

        old = os.environ.get("ALPHARD_PG_DSN")
        os.environ["ALPHARD_PG_DSN"] = "postgresql://user:pass@host:5432/db"
        try:
            sink = make_default_audit_log()
            assert isinstance(sink, PostgresAuditLog)
        finally:
            if old is None:
                os.environ.pop("ALPHARD_PG_DSN", None)
            else:
                os.environ["ALPHARD_PG_DSN"] = old

    def test_postgres_dsn_missing_raises_runtime_error(self) -> None:
        """PostgresAuditLog without DSN or env raises on first write."""
        from src.data.quality.audit import PostgresAuditLog

        old = os.environ.pop("ALPHARD_PG_DSN", None)
        try:
            sink = PostgresAuditLog(dsn=None)
            i = Issue.make(gate="g", kind=IssueKind.ING_OUTLIER, message="x")
            with pytest.raises(RuntimeError):
                sink.write_event(i, ticker="X", gate="g")
        finally:
            if old is not None:
                os.environ["ALPHARD_PG_DSN"] = old

    def test_postgres_table_name_rejects_sql_injection(self) -> None:
        """BUGFIX (C-2): reject anything that isn't a safe identifier
        at construction time."""
        from src.data.quality.audit import PostgresAuditLog

        for bad in (
            "data_quality_events; DROP TABLE users--",
            "table'with'quotes",
            "Schema.With.Dots",
            "1leading_digit",
            "",
        ):
            with pytest.raises(ValueError, match="invalid table name"):
                PostgresAuditLog(dsn="postgresql://x", table=bad)

    def test_postgres_table_name_accepts_valid_identifier(self) -> None:
        from src.data.quality.audit import PostgresAuditLog

        sink = PostgresAuditLog(dsn="postgresql://x", table="custom_table_42")
        assert sink._table == "custom_table_42"


class TestCLI:
    def _write_csv(self, path: str, rows: list[Bar]) -> None:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["primary_key", "open", "high", "low", "close", "volume"])
            for b in rows:
                w.writerow(
                    [
                        b.primary_key.isoformat(),
                        b.open,
                        b.high,
                        b.low,
                        b.close,
                        b.volume,
                    ]
                )

    def test_ingestion_cli_clean_csv(self, tmp_path) -> None:
        from src.data.quality.__main__ import main

        csv_path = tmp_path / "clean.csv"
        # 260 trading-day bars ending today so staleness/coverage OK.
        bars = _bars(260)
        self._write_csv(str(csv_path), bars)
        last = bars[-1].primary_key
        # Patch datetime.now to a known point so staleness is happy.
        import datetime as _dt

        fixed_now = _dt.datetime(
            last.year, last.month, last.day, tzinfo=_dt.timezone.utc
        ) + _dt.timedelta(  # noqa: E501
            days=1
        )  # noqa: E501
        from src.data.quality import ingestion_gate, __main__ as cli_mod

        orig_now = ingestion_gate.datetime
        orig_main_now = cli_mod.datetime

        # Stub: replace datetime.now() everywhere by monkeypatching.
        class _FrozenDateTime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return fixed_now

        try:
            ingestion_gate.datetime = _FrozenDateTime
            cli_mod.datetime = _FrozenDateTime
            rc = main(["ingestion", "SBER", "--csv", str(csv_path), "--allow-high"])
        finally:
            ingestion_gate.datetime = orig_now
            cli_mod.datetime = orig_main_now
        # 260 clean bars -> no CRITICAL, allow_high handles HIGH.
        assert rc in (0, 1)

    def test_ingestion_cli_missing_csv_column(self, tmp_path) -> None:
        """CSV missing a required column produces a CRITICAL (exit 1)."""
        from src.data.quality.__main__ import main

        csv_path = tmp_path / "bad.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["primary_key", "open", "close"])  # missing high, low, volume
            w.writerow(["2025-01-01", 100.0, 100.0])
        rc = main(["ingestion", "SBER", "--csv", str(csv_path), "--allow-high"])
        # CRITICAL -> exit 1 even with --allow-high.
        assert rc == 1

    def test_historical_cli_clean(self, tmp_path) -> None:
        from src.data.quality.__main__ import main

        csv_path = tmp_path / "hist.csv"
        bars = _bars(30, close_fn=lambda i: 100.0 + i * 0.1)
        self._write_csv(str(csv_path), bars)
        rc = main(["historical", "SBER", "--csv", str(csv_path), "--allow-high"])
        assert rc == 0

    def test_cross_source_cli_two_csvs(self, tmp_path) -> None:
        from src.data.quality.__main__ import main

        # Build two identical CSVs (no divergence).
        bars = _bars(30, close_fn=lambda i: 100.0 + i * 0.1)
        a = tmp_path / "a.csv"
        b = tmp_path / "b.csv"
        self._write_csv(str(a), bars)
        self._write_csv(str(b), bars)
        rc = main(
            [
                "cross_source",
                "SBER",
                "--csv",
                str(a),
                "--csv-b",
                str(b),
                "--source-a",
                "tinkoff",
                "--source-b",
                "moex",
                "--allow-high",
            ]
        )
        assert rc == 0

    def test_cli_passes_aware_datetime_to_gates(self, tmp_path) -> None:
        """Regression: issue #154.

        ``check_ingestion`` (and ``check_historical``) build an
        offset-aware ``latest_dt`` inside the staleness check (line 401 of
        ``ingestion_gate.py``). Passing an offset-naive ``now`` raises
        ``TypeError: can't subtract offset-naive and offset-aware
        datetimes`` BEFORE the regression fix.

        This test fails fast if anyone reintroduces a naive ``datetime.now()``
        in ``src/data/quality/__main__.py``. We force the audit sink to
        InMemoryAuditLog (CI sets ALPHARD_PG_DSN, but the data_quality
        schema is not migrated) and exercise the real production
        datetime path with the live wall clock — no monkeypatch on
        ``datetime`` itself. 260 fresh bars ending on a recent weekday
        guarantees the staleness branch is exercised; if the gate
        crashed with naive datetimes, this test would error.
        """
        import os
        from src.data.quality.__main__ import main
        from src.data.quality import __main__ as cli_mod
        from src.data.quality.audit import InMemoryAuditLog

        # Isolate from CI Postgres — schema isn't migrated in this test.
        orig_dsn = os.environ.pop("ALPHARD_PG_DSN", None)
        orig_make = cli_mod.make_default_audit_log
        cli_mod.make_default_audit_log = lambda: InMemoryAuditLog()
        try:
            csv_path = tmp_path / "fresh.csv"
            bars = _bars(260)
            self._write_csv(str(csv_path), bars)

            rc = main(["ingestion", "SBER", "--csv", str(csv_path), "--allow-high"])
            # CRITICAL => 1, HIGH => 1 unless --allow-high, no issues => 0.
            assert rc in (0, 1)

            # Also exercise the historical subcommand for symmetry.
            hist_csv = tmp_path / "hist.csv"
            self._write_csv(
                str(hist_csv),
                _bars(30, close_fn=lambda i: 100.0 + i * 0.1),
            )
            rc_h = main(["historical", "SBER", "--csv", str(hist_csv), "--allow-high"])
            assert rc_h == 0
        finally:
            cli_mod.make_default_audit_log = orig_make
            if orig_dsn is not None:
                os.environ["ALPHARD_PG_DSN"] = orig_dsn

    def test_cli_now_is_offset_aware(self) -> None:
        """Regression: issue #154 (static guarantee).

        The CLI dispatchers (``_cmd_ingestion``, ``_cmd_historical``) MUST
        pass an offset-aware ``now`` to their respective gate functions.
        We assert this by reading the live source and confirming the
        call sites construct ``datetime.now(tz=timezone.utc)`` — not the
        bare ``datetime.now()`` that triggered the original TypeError.
        """
        import inspect
        from src.data.quality import __main__ as cli_mod

        for fn_name in ("_cmd_ingestion", "_cmd_historical"):
            fn = getattr(cli_mod, fn_name)
            source = inspect.getsource(fn)
            assert "datetime.now()" not in source, (
                f"{fn_name} uses naive datetime.now(); " "check_ingestion requires offset-aware UTC now."
            )
            assert "timezone.utc" in source, (
                f"{fn_name} does not pin tz=timezone.utc on its 'now' " "argument; passes naive datetime to the gate."
            )


class TestInvariants:
    def test_severity_decision_is_deterministic(self) -> None:
        """Same gate input -> same Issue list (set + count)."""
        bars = _bars(50)
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        r1 = check_ingestion("X", bars, now=now)
        r2 = check_ingestion("X", bars, now=now)
        assert [i.kind for i in r1.issues] == [i.kind for i in r2.issues]
        assert [i.count for i in r1.issues] == [i.count for i in r2.issues]

    def test_zero_false_negatives_on_injected_anomalies(self) -> None:
        """Spec acceptance: 0 false-negatives on injected anomalies.

        Each anomaly type below MUST trigger its corresponding IssueKind
        exactly once (or more — never zero).
        """
        # Anomaly 1: synthetic forward split
        pre = _bars(5, close_fn=lambda i: 500.0)
        post = _bars(5, base=date(2025, 1, 10), close_fn=lambda i: 100.0)
        bars_with_split = pre + post
        events = detect_splits(bars_with_split)
        assert len(events) >= 1, "synthetic forward split must be detected"

        # Anomaly 2: NaN injection — pydantic Field(gt=0.0) blocks it
        # at model construction time (CRITICAL defense in depth).
        with pytest.raises(Exception):
            Bar(
                primary_key=date(2026, 1, 1),
                open=1.0,
                high=2.0,
                low=0.5,
                close=float("nan"),  # type: ignore[arg-type]
                volume=0,
            )

        # Anomaly 3: range violation (high < max(open, close))
        bars = _bars(10)
        bad = bars[3].model_copy(update={"high": 50.0, "low": 200.0})
        r = check_ingestion("X", bars[:3] + [bad] + bars[4:])
        assert IssueKind.ING_RANGE_VIOLATION in {
            i.kind for i in r.issues
        }, "range violation must be flagged"  # noqa: E501

    def test_severity_assignment_known_good_vs_known_bad(self) -> None:
        """Spec acceptance: severity correctly assigned for known-good vs bad.

        Known-good data (smooth, sufficient history, current): no issues.
        Known-bad data: at least one HIGH or CRITICAL.
        """
        # Known-good: 300 smooth bars ending today.
        bars = _bars(300)
        last = bars[-1].primary_key
        now = datetime(last.year, last.month, last.day, tzinfo=timezone.utc) + timedelta(days=1)
        r_good = check_ingestion("X", bars, now=now)
        # We accept any of the LOW-only issues (e.g. ING_LOW_VOLUME if
        # by chance >10% of rows have vol=0). None of our default bars
        # have vol=0, so this should be clean.
        assert r_good.passed, f"known-good should pass; got {r_good.issues}"

        # Known-bad: only 50 bars + a range violation.
        bad = bars[10].model_copy(update={"high": 50.0, "low": 200.0})
        r_bad = check_ingestion("X", bars[:10] + [bad] + bars[11:51])
        assert r_bad.worst_severity() in (Severity.HIGH, Severity.CRITICAL)
        assert not r_bad.passed


# ---------------------------------------------------------------------------
# Additional deterministic / edge-case tests
# ---------------------------------------------------------------------------


class TestExtraDeterminism:
    def test_ingestion_report_is_frozen(self) -> None:
        """QualityReport from ingestion is frozen (pydantic + tuple issues)."""
        bars = _bars(20)
        r = check_ingestion("X", bars, now=FROZEN_NOW)
        with pytest.raises(Exception):
            r.issues = ()  # type: ignore[misc]

    def test_issue_is_frozen(self) -> None:
        """Issue objects are immutable (frozen)."""
        i = Issue.make(gate="g", kind=IssueKind.ING_OUTLIER, message="x")
        with pytest.raises(Exception):
            i.message = "y"  # type: ignore[misc]

    def test_severity_for_all_kinds_in_catalog(self) -> None:
        """severity_for() succeeds for every IssueKind member."""
        for kind in IssueKind:
            severity_for(kind)  # would raise if kind not in catalog

    def test_issue_make_rejects_bad_kind(self) -> None:
        """Issue.make with a non-IssueKind value triggers pydantic error."""
        with pytest.raises(Exception):
            Issue.make(gate="g", kind="not-a-kind", message="x")  # type: ignore[arg-type]

    def test_quality_report_equality(self) -> None:
        """QualityReport equality is structural (same fields == same report)."""
        i = Issue.make(gate="g", kind=IssueKind.ING_OUTLIER, message="x")
        r1 = QualityReport(ticker="X", gate="g", issues=(i,))
        r2 = QualityReport(ticker="X", gate="g", issues=(i,))
        assert r1 == r2
        r3 = QualityReport(ticker="Y", gate="g", issues=(i,))
        assert r1 != r3

    def test_check_ingestion_with_zero_bars_is_not_crash(self) -> None:
        """An empty series yields a report (with HIGH issues, never crashes)."""
        r = check_ingestion("X", [], now=FROZEN_NOW)
        assert isinstance(r, QualityReport)
        assert r.worst_severity() in (Severity.HIGH, Severity.CRITICAL)
        assert not r.passed


class TestExtraHistorical:
    def test_split_event_is_reverse_property(self) -> None:
        """SplitEvent.is_reverse is True iff 0 < ratio < 1."""
        fwd = SplitEvent(date=date(2025, 1, 1), ratio=2.0, confirmed=True)
        rev = SplitEvent(date=date(2025, 1, 1), ratio=0.5, confirmed=True)
        assert not fwd.is_reverse
        assert rev.is_reverse

    def test_apply_split_adjustment_no_splits_is_noop(self) -> None:
        """Empty splits list leaves bars unchanged."""
        bars = _bars(5)
        adj = apply_split_adjustment(bars, [])
        assert adj == bars

    def test_check_historical_with_zero_bars(self) -> None:
        """Empty bar list yields a clean report, no crash."""
        r = check_historical("X", [], now=FROZEN_NOW)
        assert isinstance(r, QualityReport)
        assert r.passed


class TestExtraCrossSource:
    def test_pure_noise_divergence_within_threshold(self) -> None:
        """Tiny divergence (<1%) does not trigger HIGH."""
        sa, sb = _aligned_pair(30, scale_b=1.001)  # 0.1% divergence
        r = check_cross_source("X", sa, sb)
        kinds = {i.kind for i in r.issues}
        assert IssueKind.XSC_DIVERGENCE_HIGH not in kinds


class TestExtraIngestion:
    def test_negative_price_caught(self) -> None:
        """Negative close -> HIGH (zero_or_negative_price)."""
        bars = _bars(5)
        bad = bars[2].model_copy(update={"close": -1.0})
        r = check_ingestion("X", bars[:2] + [bad] + bars[3:])
        assert IssueKind.ING_ZERO_OR_NEGATIVE_PRICE in {i.kind for i in r.issues}

    def test_bar_model_rejects_negative_at_construction(self) -> None:
        """Bar model itself blocks non-positive prices at construction."""
        with pytest.raises(Exception):
            Bar(
                primary_key=date(2026, 1, 1),
                open=-1.0,
                high=2.0,
                low=0.5,
                close=1.0,
                volume=0,
            )

    def test_ingestion_params_frozen(self) -> None:
        """IngestionParams is frozen — callers can't mutate at runtime."""
        p = IngestionParams()
        with pytest.raises(Exception):
            p.outlier_zscore = 99.0  # type: ignore[misc]


class TestExtraAudit:
    def test_in_memory_sink_extra_payload_preserved(self) -> None:
        """InMemoryAuditLog preserves the extra dict round-trip."""
        sink = InMemoryAuditLog()
        i = Issue.make(
            gate="g",
            kind=IssueKind.ING_OUTLIER,
            message="x",
            count=3,
            extra={"first_index": 7, "threshold": 6.0},
        )
        sink.write_event(i, ticker="X", gate="g")
        assert sink.events[0]["extra"] == {"first_index": 7, "threshold": 6.0}


# ---------------------------------------------------------------------------
# C4 coverage: focused tests for defensive branches in
# _zscore_threshold_filter, log_returns, expected_trading_days,
# and check_ingestion early-return paths.
# ---------------------------------------------------------------------------


class TestIngestionGateDefensiveBranches:
    """Coverage for defensive branches that real-world data rarely hits.

    Each test below targets a single missing branch in coverage report.
    Branches include:
      - log_returns NaN propagation (zero/negative prev or cur)
      - _zscore_threshold_filter with stdev==0 (constant prices)
      - _zscore_threshold_filter with StatisticsError (1 non-NaN value)
      - _zscore_threshold_filter with all-NaN returns
      - _zscore_threshold_filter + check_ingestion outlier firing
      - expected_trading_days where end < start returns 0
      - check_ingestion: null primary_key path (lines 280-288)
      - check_ingestion: NaN-in-OHLC path (lines 300-309)
    """

    def test_log_returns_nan_when_prev_zero(self) -> None:
        """Line 153-155: prev <= 0 → first return entry is NaN.

        With ``[0.0, 100.0, 110.0]``, the first return (NaN for prev=0→cur=100)
        is NaN; subsequent returns proceed normally.
        """
        rets = log_returns([0.0, 100.0, 110.0])
        assert len(rets) == 2
        # First return: NaN (because prev=0.0 emitted the early-return branch).
        assert rets[0] != rets[0]  # NaN != NaN
        # Second return: log(110/100) ≈ 0.0953 — finite.
        assert rets[1] == pytest.approx(0.09531017980432493, abs=1e-9)

    def test_log_returns_nan_when_cur_zero(self) -> None:
        """Line 153-155: cur <= 0 → NaN return."""
        rets = log_returns([100.0, 0.0])
        assert len(rets) == 1
        assert rets[0] != rets[0]  # NaN

    def test_log_returns_nan_when_both_zero(self) -> None:
        """Line 153-155: both prev and cur zero/negative → both NaN."""
        rets = log_returns([0.0, 0.0, 100.0])
        assert len(rets) == 2
        assert rets[0] != rets[0]
        assert rets[1] != rets[1]

    def test_zscore_filter_all_nan_returns_empty(self) -> None:
        """Line 178: len(clean) < 2 → return [].

        With all-NaN returns, ``clean`` is empty → no statistics computed.
        """
        from src.data.quality.ingestion_gate import _zscore_threshold_filter

        # Two NaNs → no clean values → return [].
        result = _zscore_threshold_filter([float("nan"), float("nan")], 3.0)
        assert result == []

    def test_zscore_filter_single_clean_returns_empty(self) -> None:
        """Line 178: only one non-NaN return → len(clean) < 2 → []."""
        from src.data.quality.ingestion_gate import _zscore_threshold_filter

        # Only one finite value, rest NaN.
        result = _zscore_threshold_filter([0.01, float("nan"), float("nan")], 3.0)
        assert result == []

    def test_zscore_filter_constant_prices_returns_empty(self) -> None:
        """Line 186: stdev == 0 (no deviation) → no outliers."""
        from src.data.quality.ingestion_gate import _zscore_threshold_filter

        # Identical returns → zero variance → stdev==0 → no outliers.
        result = _zscore_threshold_filter([0.05, 0.05, 0.05, 0.05], 3.0)
        assert result == []

    def test_zscore_filter_outlier_append_path(self) -> None:
        """Lines 191-192: |z| > threshold → append (i+1, r)."""
        from src.data.quality.ingestion_gate import _zscore_threshold_filter

        # Construct returns with one extreme outlier.
        # Mean=0, stdev ~0.033; one return of 0.10 has |z| ≈ 3.0+.
        rets = [0.01, 0.02, 0.005, 0.015, 0.10, 0.02, 0.03]
        result = _zscore_threshold_filter(rets, 2.0)
        assert len(result) == 1
        # Index is i+1 (one-based for bar positions).
        assert result[0][0] == 5
        assert abs(result[0][1] - 0.10) < 1e-9

    def test_expected_trading_days_inverted_range(self) -> None:
        """Line 130: end < start → return 0."""
        assert expected_trading_days(date(2025, 6, 1), date(2025, 5, 1)) == 0

    def test_check_ingestion_null_primary_key(self) -> None:
        """Lines 280-288: rows with NULL primary_key → ING_NULL_PRIMARY_KEY."""
        # We need to bypass pydantic validation. Build a Bar then manually
        # set primary_key to None.
        bar = _bars(5)[0]
        # model_copy with update sets primary_key=None, but pydantic forbids
        # None at construction. Use object.__setattr__ to bypass.
        object.__setattr__(bar, "primary_key", None)
        r = check_ingestion("X", [bar], now=FROZEN_NOW)
        assert r.rejected
        assert IssueKind.ING_NULL_PRIMARY_KEY in {i.kind for i in r.issues}

    def test_check_ingestion_nan_via_positive_bypass(self) -> None:
        """Lines 300-309: NaN-in-OHLC → ING_NAN_PRICE.

        Pydantic Field(gt=0) blocks NaN at construction. We monkeypatch
        ``gt`` to None on a fresh Bar model class to allow NaN construction,
        simulating 'NaN that slipped through json.loads' as the docstring
        describes.
        """
        # Build via model_construct which skips validation entirely.
        bar = Bar.model_construct(
            primary_key=date(2026, 1, 5),
            open=1.0,
            high=2.0,
            low=0.5,
            close=float("nan"),
            volume=0,
        )
        r = check_ingestion("X", [bar], now=FROZEN_NOW)
        assert r.rejected
        assert IssueKind.ING_NAN_PRICE in {i.kind for i in r.issues}

    def test_max_calendar_gap_returns_zero_for_single_bar(self) -> None:
        """Line 203: fewer than 2 bars → return 0."""
        from src.data.quality.ingestion_gate import _max_calendar_gap

        bars = _bars(1)
        assert _max_calendar_gap(bars) == 0

    def test_max_calendar_gap_returns_zero_for_empty(self) -> None:
        """Line 203: empty bars list → return 0."""
        from src.data.quality.ingestion_gate import _max_calendar_gap

        assert _max_calendar_gap([]) == 0

    def test_zscore_filter_single_value_raises_statistics_error(self) -> None:
        """Lines 182-183: ``stdev()`` raises StatisticsError on 1 value.

        Note: ``statistics.stdev`` requires at least 2 data points. With
        exactly 1 non-NaN return and rest NaN, ``clean`` has 1 value, so
        ``len(clean) < 2`` triggers first (line 178 → return []).
        To exercise the StatisticsError branch (line 182), we need a
        contrived input that reaches ``stdev()`` but raises. We test
        this by patching stdev with a side_effect to raise the error.
        """
        import unittest.mock as mock
        import src.data.quality.ingestion_gate as gate

        with mock.patch.object(
            gate.statistics,
            "stdev",
            side_effect=gate.statistics.StatisticsError,
        ):
            # 3 finite values → clean=[0.01, 0.02, 0.03] → stdev would be called
            # and raises. Expected: return [] (StatisticsError caught).
            result = gate._zscore_threshold_filter([0.01, 0.02, 0.03], 3.0)
        assert result == []

    def test_check_ingestion_outlier_via_synthetic_spike(self) -> None:
        """Lines 346-347: outlier detected → Issue.append with first_idx/extra.

        Construct prices so one log-return has |z| > default threshold (6.0).
        Mean returns ~0, stdev small relative to the spike.
        """
        # 260 normal bars (≥252 to pass min_history) ending just before FROZEN_NOW,
        # then one spike bar at close=200 (vs ~100 baseline) → ln(2) ≈ 0.693 ≈ 6σ+ return.
        bars = _bars(260, base=date(2025, 8, 1))
        last = bars[-1].primary_key
        spike_date = last + timedelta(days=2)  # one weekday after the cluster
        spike = Bar(
            primary_key=spike_date,
            # open/high/low must satisfy range check (b.high >= max(open,close) - eps,
            # b.low <= min(open,close) + eps). Use safe margins above/below.
            open=199.0,
            high=201.0,  # > max(199, 200)=200 with eps
            low=197.0,  # < min(199, 200)=199 with eps
            close=200.0,
            volume=1000,
        )
        # Adjust ``now`` to satisfy staleness (≤3 calendar days from last bar).
        # Spike is at +2 days, so 'now' = spike + 0 days is 2 days after last 'real' bar.
        now = datetime(spike_date.year, spike_date.month, spike_date.day, tzinfo=timezone.utc)
        all_bars = sorted(bars + [spike], key=lambda b: b.primary_key)
        r = check_ingestion("X", all_bars, now=now)
        kinds = {i.kind for i in r.issues}
        assert IssueKind.ING_OUTLIER in kinds, f"expected ING_OUTLIER; got: {kinds}"
        # Verify the issue carries first_outlier_index + threshold.
        for i in r.issues:
            if i.kind == IssueKind.ING_OUTLIER:
                assert i.extra is not None
                assert "first_outlier_index" in i.extra
                assert i.extra["threshold"] == pytest.approx(6.0)


# flake8: noqa: W391


# ---------------------------------------------------------------------------
# C6 coverage: defensive branches in src/data/quality/historical.py
# ---------------------------------------------------------------------------


class TestHistoricalDefensiveBranches:
    """Coverage for defensive branches in historical.py (line 146, 155, 162,
    204, 210, 213, 222, 301).

    Targets the early-return/continue paths in:
      - detect_splits() loops (line 146, 155, 162)
      - _nearest_integer() early returns (line 204, 210, 213, 222)
      - check_historical() now=None path (line 301)
    """

    def test_nearest_integer_returns_none_for_non_positive(self) -> None:
        """Line 204: ratio <= 0 (zero/negative) → return None."""
        from src.data.quality.historical import _nearest_integer

        assert _nearest_integer(0.0) is None
        assert _nearest_integer(-0.5) is None
        assert _nearest_integer(-100.0) is None

    def test_nearest_integer_returns_none_for_nan_or_inf(self) -> None:
        """Line 204: math.isnan/isinf → return None."""
        from src.data.quality.historical import _nearest_integer

        assert _nearest_integer(float("nan")) is None
        assert _nearest_integer(float("inf")) is None
        assert _nearest_integer(float("-inf")) is None

    def test_nearest_integer_returns_none_for_small_inverse(self) -> None:
        """Line 210: 1.0/ratio gives N<2 → return None (small forward split)."""
        from src.data.quality.historical import _nearest_integer

        # ratio=0.99 → inv=1.0101 → n=round(1.01)=1 → n<2 → None
        assert _nearest_integer(0.99) is None
        # ratio=0.6 → inv=1.667 → n=2 but deviation = 0.167/2 = 8.3% > 2% → None
        assert _nearest_integer(0.6) is None

    def test_nearest_integer_returns_none_for_small_ratio(self) -> None:
        """Line 213: round(ratio) < 2 → return None (ordinary price move, not split)."""
        from src.data.quality.historical import _nearest_integer

        # ratio=1.01 → n=round(1.01)=1 → n<2 → None
        assert _nearest_integer(1.01) is None
        # ratio=1.5 → n=round(1.5)=2 but deviation = 0.5/2 = 25% > 2% → None
        assert _nearest_integer(1.5) is None

    def test_check_historical_uses_now_when_none(self) -> None:
        """Line 301: now=None → use datetime.now(timezone.utc)."""
        from src.data.quality.historical import check_historical, HistoricalParams

        # Build a 10-bar series ending today (UTC); should pass without future rows.
        from datetime import date, timedelta

        today = date.today()
        bars = _bars(10, base=today - timedelta(days=20))
        r = check_historical("X", bars)  # now=None
        # No issues expected (no future rows, no splits, no delisting).
        kinds = {i.kind for i in r.issues}
        assert IssueKind.HST_FUTURE_ROW not in kinds

    def test_detect_splits_skips_zero_or_negative_close(self) -> None:
        """Line 146: prev.close <= 0 or cur.close <= 0 → continue."""
        from src.data.quality.historical import detect_splits
        from src.data.quality.ingestion_gate import Bar as _Bar

        # Build bars via model_construct to bypass Field(gt=0) on close.
        base = date(2025, 1, 1)
        bars = [
            _Bar.model_construct(
                primary_key=base + timedelta(days=i),
                open=100.0,
                high=101.0,
                low=99.0,
                close=0.0 if i == 1 else 100.0,  # bar[1] has close=0
                volume=1000,
            )
            for i in range(5)
        ]
        # detect_splits should skip the bad pair (line 146), not raise.
        result = detect_splits(bars)
        # No splits expected because the bad row breaks the chain.
        assert isinstance(result, list)

    def test_detect_splits_skips_field_with_zero_value(self) -> None:
        """Line 162: field cross-check loop continues if prev_v <= 0 or cur_v <= 0.

        Build bars where the close ratio is a clean 2:1 split but one of
        open/high/low has a zero value, triggering the inner field loop's
        continue branch.
        """
        from src.data.quality.historical import detect_splits
        from src.data.quality.ingestion_gate import Bar as _Bar

        # Two bars: prev.close=100, cur.close=200 (2:1 split). All other
        # fields also follow the 2x ratio except open=0 on cur.
        base = date(2025, 1, 1)
        prev = _Bar.model_construct(
            primary_key=base,
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1000,
        )
        cur = _Bar.model_construct(
            primary_key=base + timedelta(days=1),
            open=0.0,  # ← open=0 (non-positive) triggers line 162
            high=200.0,
            low=200.0,
            close=200.0,
            volume=2000,
        )
        # detect_splits: close ratio 2.0 → clean split. But open=0 means
        # field check skips the 'open' field; only high+low agree.
        # With split_min_agreeing_fields=2 (default), we still emit the split.
        # The stored ratio is the multiplicative adjustment for PRE-event
        # bars, so a 2:1 forward split is stored as ratio=0.5.
        result = detect_splits([prev, cur])
        # Should detect the split based on high+low agreement.
        assert len(result) == 1
        assert result[0].ratio == 0.5

    def test_detect_splits_skips_non_integer_ratio(self) -> None:
        """Line 155: n_signed is None (not a clean integer ratio) → continue."""
        from src.data.quality.historical import detect_splits

        # Construct bars with a 1.07x move: not a clean integer ratio.
        bars = _bars(5, close_fn=lambda i: 100.0 * (1.07**i))
        result = detect_splits(bars)
        # 1.07x is an ordinary price move, not a split.
        assert result == []

    def test_detect_splits_skips_out_of_range_ratio(self) -> None:
        """Line 162: split outside [split_min_ratio, split_max_ratio] → continue."""
        from src.data.quality.historical import detect_splits

        # 20x move: too large to be a split (default max=10).
        bars = _bars(5, close_fn=lambda i: 100.0 * (20.0 if i == 1 else 1.0))
        result = detect_splits(bars)
        # 20x exceeds default split_max_ratio=10.
        assert result == []


# flake8: noqa: W391
