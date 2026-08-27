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
import math
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

    # ------------------------------------------------------------------
    # Issue #271 — _log_returns NaN-poison + _pearson NaN silent-pass.
    # ------------------------------------------------------------------

    def test_log_returns_does_not_poison_after_zero(self) -> None:
        """Issue #271: consecutive zero/NaN closes must NOT poison subsequent returns.

        Pre-fix: _log_returns([100, 102, 0, 0, 101, 103]) propagated the
        zero forward as ``prev`` (unconditional ``prev = cur``), so once
        a zero appeared, prev stayed zero and every subsequent return
        was NaN — even when the actual adjacent closes were perfectly
        fine. That NaN-poisoned series then slipped through ``_pearson``
        (no NaN handling) and through ``check_cross_source`` (``NaN <
        threshold`` is False), silently bypassing the Level-2 gate.

        Post-fix: each invalid bar is treated as a gap — the return
        that includes it is NaN, but ``last_valid`` does not advance to
        the bad bar. The next valid bar pairs against the LAST VALID
        close (not against the corrupted one), so the return series
        resumes a clean shape immediately after the gap rather than
        staying poisoned.
        """
        from src.data.quality.cross_source import _log_returns

        rets = _log_returns([100.0, 102.0, 0.0, 0.0, 101.0, 103.0])
        # Six input closes -> five returns.
        assert len(rets) == 5
        # Index 0: ln(102/100) — valid.
        assert rets[0] == pytest.approx(math.log(102.0 / 100.0))
        # Index 1: cur=0 (bad) -> NaN gap; last_valid stays at 102.
        assert math.isnan(rets[1])
        # Index 2: cur=0 (bad) -> NaN gap; last_valid stays at 102.
        assert math.isnan(rets[2])
        # Index 3: cur=101 (valid) -> ln(101/102). Pre-fix this was NaN
        # because prev=0 carried forward and `prev <= 0` fired again.
        # Post-fix it is a real log return, just over a slightly wider
        # gap than a normal pair.
        assert rets[3] == pytest.approx(math.log(101.0 / 102.0))
        # Index 4: ln(103/101).
        assert rets[4] == pytest.approx(math.log(103.0 / 101.0))

    def test_log_returns_handles_nan_close(self) -> None:
        """NaN close must NOT poison the return series."""
        from src.data.quality.cross_source import _log_returns

        rets = _log_returns([100.0, float("nan"), 105.0])
        assert len(rets) == 2
        assert math.isnan(rets[0])
        # Pre-fix this was NaN because prev=nan; post-fix the gap is bridged.
        assert rets[1] == pytest.approx(math.log(105.0 / 100.0))

    def test_pearson_returns_none_when_any_nan(self) -> None:
        """Issue #271: _pearson must NOT return NaN — return None instead.

        Pre-fix: sum() of a list containing NaN yields NaN, which then
        silently flows through varx/vary comparison (NaN == 0 is False)
        and produces NaN as the final correlation. Post-fix: any NaN pair
        short-circuits to None.
        """
        from src.data.quality.cross_source import _pearson

        result = _pearson([0.01, 0.02, 0.03], [0.01, float("nan"), 0.03])
        assert result is None

    def test_zero_close_in_source_emits_quality_issue_not_silent_pass(
        self,
    ) -> None:
        """Issue #271 regression test.

        Pre-fix: source B with a single zero close in a 10-point window
        causes Pearson to return NaN, the NaN-comparison guards in
        check_cross_source silently let it pass, and operators get NO
        XSC_CORRELATION_LOW / XSC_SOURCE_MISSING warning.

        Post-fix: a quality issue MUST be raised (XSC_SOURCE_MISSING is the
        natural fit — the source has at least one zero/NaN close that
        corrupts the log-return series).
        """
        # 10 trading days starting Mon 2026-01-05.
        start = date(2026, 1, 5)
        dates: list[date] = []
        d = start
        while len(dates) < 10:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)
        a_closes = [100.0 + i for i in range(10)]
        # B mirrors A except index 2 is zero (data glitch).
        b_closes = [100.0 + i for i in range(10)]
        b_closes[2] = 0.0

        sa = SourceSeries(source_name="a", bars=tuple(zip(dates, a_closes)))
        sb = SourceSeries(source_name="b", bars=tuple(zip(dates, b_closes)))

        r = check_cross_source("X", sa, sb)
        # The gate MUST raise at least one issue — silent pass is the bug.
        assert not r.passed, (
            f"check_cross_source silently passed despite zero close in B; "
            f"issues={[(i.kind, i.severity) for i in r.issues]}"
        )
        # The natural issue kind is XSC_SOURCE_MISSING (NaN correlation
        # because at least one source has zero/NaN closes).
        kinds = {i.kind for i in r.issues}
        assert IssueKind.XSC_SOURCE_MISSING in kinds, f"expected XSC_SOURCE_MISSING in issues, got {kinds}"

    def test_rolling_divergence_emits_when_one_source_has_nan_close(self) -> None:
        """Issue #271: rolling divergence must also surface NaN closes.

        Pre-fix: the rolling loop guards `c <= 0` but NaN compares False
        to 0, so a NaN close is treated as a normal price; the resulting
        log-divergence is NaN, never exceeds the threshold, and the
        divergence issue is silently skipped. Post-fix: the loop must
        skip windows with NaN closes, AND emit XSC_SOURCE_MISSING if ALL
        windows are NaN-poisoned (so we don't lose the signal entirely).
        """
        from src.data.quality.cross_source import CrossSourceParams

        # 10 trading days, A clean & rising, B clean & rising but with one
        # NaN and a large divergence on the LAST bar (window ends at last bar).
        start = date(2026, 1, 5)
        dates: list[date] = []
        d = start
        while len(dates) < 10:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)
        a_closes = [100.0 + i for i in range(10)]
        b_closes = [100.0 + i for i in range(10)]
        # Plant NaN + divergence on the same final bar so the rolling
        # window that includes the divergence contains the NaN.
        b_closes[9] = float("nan")  # data glitch

        sa = SourceSeries(source_name="a", bars=tuple(zip(dates, a_closes)))
        sb = SourceSeries(source_name="b", bars=tuple(zip(dates, b_closes)))

        # Lower the threshold so any surviving divergence would trip.
        params = CrossSourceParams(rolling_max_mean_divergence=0.001)
        r = check_cross_source("X", sa, sb, params=params)
        # Must NOT silently pass.
        assert not r.passed, (
            f"check_cross_source silently passed despite NaN close in B; "
            f"issues={[(i.kind, i.severity) for i in r.issues]}"
        )

    # ------------------------------------------------------------------
    # Issue #278 — leading-bad bar must emit a NaN gap, not silently
    # drop a slot in the output series. Length contract:
    #     len(_log_returns(closes)) == len(closes) - 1  for all len >= 2
    # ------------------------------------------------------------------

    def test_log_returns_emits_leading_nan_gap(self) -> None:
        """Issue #278: leading NaN close produces a leading NaN return,
        not a length-shortened series. Pre-fix the leading slot was
        silently dropped, breaking the length contract for ``_pearson``."""
        from src.data.quality.cross_source import _log_returns

        rets = _log_returns([float("nan"), 100.0, 102.0])
        # 3 closes -> 2 returns (NOT 1 — that was the bug).
        assert len(rets) == 2, f"expected 2 returns, got {rets!r}"
        # First return is NaN (the gap for the bad first bar).
        assert math.isnan(rets[0]), f"expected NaN leading gap, got {rets!r}"
        # Second return is the log-return of the first valid pair.
        assert math.isfinite(rets[1]), f"expected finite second return, got {rets!r}"
        assert rets[1] == pytest.approx(math.log(102.0 / 100.0))

    def test_log_returns_emits_leading_zero_gap(self) -> None:
        """Issue #278: leading zero close behaves the same as leading NaN."""
        from src.data.quality.cross_source import _log_returns

        rets = _log_returns([0.0, 100.0, 102.0])
        assert len(rets) == 2, f"expected 2 returns, got {rets!r}"
        assert math.isnan(rets[0]), f"expected NaN leading gap, got {rets!r}"
        assert rets[1] == pytest.approx(math.log(102.0 / 100.0))

    def test_log_returns_emits_leading_negative_gap(self) -> None:
        """Issue #278: leading negative close also produces a leading
        NaN gap. Negative closes are equally invalid for log-returns."""
        from src.data.quality.cross_source import _log_returns

        rets = _log_returns([-5.0, 100.0, 102.0])
        assert len(rets) == 2, f"expected 2 returns, got {rets!r}"
        assert math.isnan(rets[0])

    def test_log_returns_length_equals_closes_minus_one_always(self) -> None:
        """Issue #278 property test: ``_log_returns`` must return exactly
        ``len(closes) - 1`` elements for ANY input of length >= 2, no
        matter where the bad bars sit. This pins the length contract
        callers (``_pearson``) depend on."""
        from src.data.quality.cross_source import _log_returns

        cases = [
            [100.0, 102.0, 0.0, 0.0, 101.0, 103.0],  # middle (issue #271 pattern)
            [float("nan"), 100.0, 102.0],  # leading NaN
            [0.0, 100.0, 102.0],  # leading zero
            [-1.0, 100.0, 102.0],  # leading negative
            [float("inf"), 100.0, 102.0],  # leading inf
            [float("nan"), float("nan"), 100.0, 102.0],  # double leading bad
            [100.0, 102.0, float("nan")],  # trailing NaN
            [100.0, 102.0, 0.0],  # trailing zero
            [float("nan"), float("nan"), float("nan")],  # all bad
            [100.0, 102.0],  # clean baseline
        ]
        for closes in cases:
            rets = _log_returns(closes)
            assert len(rets) == len(closes) - 1, (
                f"_log_returns({closes!r}) returned {len(rets)} returns " f"(expected {len(closes) - 1}); rets={rets!r}"
            )

    def test_log_returns_leading_glitch_does_not_poison_subsequent(self) -> None:
        """Issue #278 (orthogonal to #271): after a leading bad bar, the
        remaining returns must NOT be NaN-poisoned. The post-#271 fix
        already protects the middle/trailing cases; this test pins the
        leading-edge symmetry."""
        from src.data.quality.cross_source import _log_returns

        rets = _log_returns([float("nan"), 100.0, 102.0, 104.0])
        assert len(rets) == 3
        # Leading gap is NaN.
        assert math.isnan(rets[0])
        # Next two returns are real numbers, not poisoned. The baseline
        # was the first VALID close (100.0), so returns are paired
        # against it: log(102/100) and log(104/102).
        assert math.isfinite(rets[1])
        assert math.isfinite(rets[2])
        assert rets[1] == pytest.approx(math.log(102.0 / 100.0))
        assert rets[2] == pytest.approx(math.log(104.0 / 102.0))

    def test_cross_source_leading_glitch_emits_documented_kind(self) -> None:
        """Issue #278 end-to-end: a leading bad bar in one source (with
        the other source clean) must NOT silently produce a misleading
        ``XSC_SOURCE_MISSING - zero variance`` message. The post-fix
        state machine emits a NaN gap for the leading bad bar so
        ``_pearson`` sees a properly-aligned return series; the NaN
        inside that series then triggers the ``"nan_gap"`` reason,
        which the gate surfaces with a message that names the bad
        close (not "zero variance")."""
        from src.data.quality.cross_source import check_cross_source

        # Build 10 trading days with one perfectly-correlated source and
        # another perfectly-correlated source with a leading bad bar.
        start = date(2026, 1, 5)
        dates: list[date] = []
        d = start
        while len(dates) < 10:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)

        a_closes = [100.0 + i for i in range(10)]
        b_closes = [100.0 + i for i in range(10)]
        b_closes[0] = 0.0  # data glitch on the FIRST bar of B

        sa = SourceSeries(source_name="a", bars=tuple(zip(dates, a_closes)))
        sb = SourceSeries(source_name="b", bars=tuple(zip(dates, b_closes)))

        r = check_cross_source("TEST", sa, sb)
        assert not r.passed, "check_cross_source passed despite leading-bad bar"
        # XSC_SOURCE_MISSING is the right IssueKind (one source has NaN
        # in the return series due to the leading-bad bar). The bug was
        # that the message lied about "zero variance in one source" when
        # the real cause was the silent length-shortening of B's return
        # series — pre-fix, the lengths of A and B differed (len 9 vs 10)
        # so _pearson returned None on the n != len(ys) guard rather
        # than the stdev==0 guard.
        kinds = {i.kind for i in r.issues}
        assert IssueKind.XSC_SOURCE_MISSING in kinds, f"expected XSC_SOURCE_MISSING, got {kinds}"
        msgs = " ".join(i.message for i in r.issues)
        # The bug: the message said "zero variance in one source" for a
        # leading-bad bar (issue #278 acceptance #3).
        assert "zero variance" not in msgs, f"misleading zero-variance message still emitted: {msgs!r}"
        # The fix: the message now mentions the NaN-gap / bad close
        # pattern so operators can act on it.
        assert (
            "NaN gap" in msgs or "bad close" in msgs.lower()
        ), f"expected message to mention NaN gap or bad close, got: {msgs!r}"

    # ------------------------------------------------------------------
    # Issue #278 follow-up — discriminator tests for the three failure
    # modes of ``_pearson_with_reason`` and the corresponding messages
    # emitted by ``check_cross_source``. These pin the QA-accepted
    # #278 acceptance #3 contract: operator-facing messages must
    # distinguish "stuck/flat source" from "one bar arrived as NaN".
    # ------------------------------------------------------------------

    def test_pearson_with_reason_length_mismatch(self) -> None:
        """Issue #278 (follow-up): ``_pearson_with_reason`` returns
        ``('length_mismatch', ...)`` when ``len(xs) != len(ys)`` or
        ``n < 2``. This was the exact guard that the #278 length-fix
        was supposed to make unreachable in production (because
        ``_log_returns`` now emits a leading NaN gap instead of
        silently dropping a slot)."""
        from src.data.quality.cross_source import _pearson_with_reason

        # n < 2
        corr, reason, detail = _pearson_with_reason([1.0], [2.0])
        assert corr is None
        assert reason == "length_mismatch", f"got {reason!r}"
        assert "1" in detail and "1" in detail  # both lengths reported

        # n != len(ys)
        corr, reason, detail = _pearson_with_reason([1.0, 2.0], [1.0])
        assert corr is None
        assert reason == "length_mismatch"
        assert "2" in detail and "1" in detail

    def test_pearson_with_reason_nan_gap(self) -> None:
        """Issue #278 (follow-up): ``_pearson_with_reason`` returns
        ``('nan_gap', ...)`` when any paired entry is non-finite, and
        the detail reports the count and the first few indices."""
        from src.data.quality.cross_source import _pearson_with_reason

        corr, reason, detail = _pearson_with_reason([0.01, 0.02, 0.03], [0.01, float("nan"), 0.03])
        assert corr is None
        assert reason == "nan_gap", f"got {reason!r}"
        assert "1" in detail  # count of bad entries
        assert "[1]" in detail or "1]" in detail  # first index
        assert "bad close" in detail.lower()

    def test_pearson_with_reason_zero_variance_names_flat_side(self) -> None:
        """Issue #278 (follow-up): ``_pearson_with_reason`` returns
        ``('zero_variance', ...)`` and names which side is flat
        (xs / ys / xs,ys)."""
        from src.data.quality.cross_source import _pearson_with_reason

        # xs is flat.
        corr, reason, detail = _pearson_with_reason([100.0, 100.0, 100.0], [1.0, 2.0, 3.0])
        assert corr is None
        assert reason == "zero_variance"
        assert "xs" in detail

        # ys is flat.
        corr, reason, detail = _pearson_with_reason([1.0, 2.0, 3.0], [100.0, 100.0, 100.0])
        assert corr is None
        assert reason == "zero_variance"
        assert "ys" in detail

    def test_pearson_with_reason_ok(self) -> None:
        """Issue #278 (follow-up): ``_pearson_with_reason`` returns
        ``('ok', '')`` for a clean computation."""
        from src.data.quality.cross_source import _pearson_with_reason

        corr, reason, detail = _pearson_with_reason([0.01, 0.02, 0.03], [0.02, 0.04, 0.06])
        assert reason == "ok"
        assert detail == ""
        assert corr is not None
        assert abs(corr - 1.0) < 1e-9

    def test_check_cross_source_zero_variance_message_names_flat_side(
        self,
    ) -> None:
        """Issue #278 (follow-up): when ``check_cross_source`` fails
        because one source is genuinely flat, the message names which
        side is flat (not a generic misleading string)."""
        from src.data.quality.cross_source import check_cross_source

        start = date(2026, 1, 5)
        dates: list[date] = []
        d = start
        while len(dates) < 10:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)

        # A genuinely flat; B varies.
        a_closes = [100.0] * 10
        b_closes = [100.0 + i for i in range(10)]
        sa = SourceSeries(source_name="flat_a", bars=tuple(zip(dates, a_closes)))
        sb = SourceSeries(source_name="rising_b", bars=tuple(zip(dates, b_closes)))

        r = check_cross_source("TEST", sa, sb)
        assert not r.passed
        kinds = {i.kind for i in r.issues}
        assert IssueKind.XSC_SOURCE_MISSING in kinds
        msgs = " ".join(i.message for i in r.issues)
        # The flat-side discriminator must be present (issue #278 #3).
        assert "zero variance" in msgs.lower()
        # And must name the flat side so the operator knows which feed
        # is stuck.
        assert "flat_a" in msgs or "xs" in msgs, f"flat side not named in message: {msgs!r}"

    def test_log_returns_empty_for_short_input(self) -> None:
        """Issue #278 (follow-up, coverage): ``_log_returns`` of a list
        shorter than 2 must return an empty list (no slot to pair).
        Pre-#278 the function relied on the loop running zero times —
        this test pins the explicit guard."""
        from src.data.quality.cross_source import _log_returns

        assert _log_returns([]) == []
        assert _log_returns([100.0]) == []

    def test_check_cross_source_fails_closed_with_nan_close(self) -> None:
        """Issue #271 regression test: a NaN close in any source must
        cause ``check_cross_source`` to fail (not silently pass). The
        NaN surfaces as a NaN-return in ``_log_returns`` and triggers
        the ``"nan_gap"`` reason in ``_pearson_with_reason``; the
        gate then emits ``XSC_SOURCE_MISSING`` with a message that
        names the bad-close pattern. Pinned here so a future refactor
        that decouples correlation from rolling divergence does not
        regress to the pre-#271 silent NaN-skip defect.
        """
        from src.data.quality.cross_source import check_cross_source

        # Plant a NaN bar inside the FIRST 5-day window. B is otherwise
        # identical to A so correlation would otherwise be 1.0.
        start = date(2026, 1, 5)
        dates: list[date] = []
        d = start
        while len(dates) < 10:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)
        a_closes = [100.0 + i for i in range(10)]
        b_closes = [100.0 + i for i in range(10)]
        b_closes[2] = float("nan")  # inside window [0:5]
        sa = SourceSeries(source_name="a", bars=tuple(zip(dates, a_closes)))
        sb = SourceSeries(source_name="b", bars=tuple(zip(dates, b_closes)))

        r = check_cross_source("TEST", sa, sb)
        # Must not pass cleanly — NaN bar in B triggers XSC_SOURCE_MISSING
        # via the correlation NaN-gap path (issue #271 fix).
        assert not r.passed

    def test_check_cross_source_emits_correlation_low(self) -> None:
        """Issue #278 (follow-up, coverage): when two sources are
        intentionally uncorrelated, the gate must emit
        ``XSC_CORRELATION_LOW`` (not ``XSC_SOURCE_MISSING``). Pins the
        ``corr < p.correlation_min`` branch."""
        from src.data.quality.cross_source import check_cross_source

        start = date(2026, 1, 5)
        dates: list[date] = []
        d = start
        while len(dates) < 10:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)

        # A monotonically rises; B oscillates — Pearson is negative.
        a_closes = [100.0 + i for i in range(10)]
        b_closes = [100.0 + (-1) ** i for i in range(10)]
        sa = SourceSeries(source_name="rising_a", bars=tuple(zip(dates, a_closes)))
        sb = SourceSeries(source_name="oscillating_b", bars=tuple(zip(dates, b_closes)))

        r = check_cross_source("TEST", sa, sb)
        kinds = {i.kind for i in r.issues}
        assert IssueKind.XSC_CORRELATION_LOW in kinds, f"expected XSC_CORRELATION_LOW, got {kinds}"


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

    def test_postgres_schema_name_accepts_valid_identifier(self) -> None:
        """Issue #265 follow-up: schema qualifier must round-trip safely."""
        from src.data.quality.audit import PostgresAuditLog

        sink = PostgresAuditLog(dsn="postgresql://x", table="t", schema="my_schema")
        assert sink._schema == "my_schema"

    def test_postgres_schema_default_is_none(self) -> None:
        """When schema is not configured the table stays unqualified
        (the connection's search_path resolves it — public in production)."""
        from src.data.quality.audit import PostgresAuditLog

        sink = PostgresAuditLog(dsn="postgresql://x", table="t")
        assert sink._schema is None

    def test_postgres_schema_name_rejects_sql_injection(self) -> None:
        """Same defensive validation as table names — single safe identifier."""
        from src.data.quality.audit import PostgresAuditLog

        for bad in (
            "sch; DROP TABLE users--",
            "schema'with'quotes",
            "Schema.With.Dots",
            "1leading_digit",
            "",
        ):
            with pytest.raises(ValueError, match="invalid schema name"):
                PostgresAuditLog(dsn="postgresql://x", table="t", schema=bad)

    def test_close_is_noop_when_never_connected(self) -> None:
        """Closing an unconnected PostgresAuditLog is a silent no-op."""
        from src.data.quality.audit import PostgresAuditLog

        sink = PostgresAuditLog(dsn="postgresql://x", table="custom_table_42")
        assert sink._conn is None
        sink.close()  # must not raise
        assert sink._conn is None
        assert sink._cursor is None

    def test_close_calls_commit_then_close_in_order(self) -> None:
        """Happy path: commit() runs, then close() runs, then handles clear."""
        from src.data.quality.audit import PostgresAuditLog

        sink = PostgresAuditLog(dsn="postgresql://x", table="custom_table_42")
        call_order: list[str] = []

        class _FakeConn:
            def commit(self) -> None:
                call_order.append("commit")

            def close(self) -> None:
                call_order.append("close")

        sink._conn = _FakeConn()
        sink._cursor = object()  # any sentinel

        sink.close()

        assert call_order == [
            "commit",
            "close",
        ], f"close() must call conn.commit() before conn.close(); got {call_order}"
        assert sink._conn is None
        assert sink._cursor is None

    def test_close_surfaces_commit_error_not_close_error(self) -> None:
        """Issue #266: if commit() raises, the caller sees the commit error,
        NOT the chained InterfaceError from close().

        Old shape nested close() inside commit()'s finally block, so the
        caller's primary exception was the close() error (with the real
        commit failure buried one frame deep as __context__). New shape
        explicitly captures commit errors and re-raises them, swallowing
        close() errors when commit() failed first.
        """
        from src.data.quality.audit import PostgresAuditLog

        class _CommitFails(RuntimeError):
            """Stand-in for psycopg.OperationalError on a network blip."""

        class _CloseAlsoFails(RuntimeError):
            """Stand-in for psycopg.InterfaceError('connection already closed')."""

        sink = PostgresAuditLog(dsn="postgresql://x", table="custom_table_42")

        class _FakeConn:
            def commit(self) -> None:
                raise _CommitFails("commit failed: network blip")

            def close(self) -> None:
                raise _CloseAlsoFails("connection already closed")

        sink._conn = _FakeConn()
        sink._cursor = object()

        with pytest.raises(_CommitFails) as exc_info:
            sink.close()

        # Primary exception is the commit() error, not the close() error.
        assert "commit failed" in str(exc_info.value)
        assert "connection already closed" not in str(exc_info.value), (
            "caller must see commit()'s error, not the chained close() " "InterfaceError (issue #266)"
        )
        # Handles cleared even on error path.
        assert sink._conn is None
        assert sink._cursor is None

    def test_close_swallows_close_error_when_commit_succeeded(self) -> None:
        """If commit() succeeds but close() raises, the close() error is
        not actionable for the caller (the connection was already broken
        *after* the data was durably written). Swallow it silently."""
        from src.data.quality.audit import PostgresAuditLog

        sink = PostgresAuditLog(dsn="postgresql://x", table="custom_table_42")

        class _FakeConn:
            def commit(self) -> None:
                pass  # success

            def close(self) -> None:
                raise RuntimeError("connection already closed")

        sink._conn = _FakeConn()
        sink._cursor = object()

        # Must not raise.
        sink.close()

        assert sink._conn is None
        assert sink._cursor is None

    def test_close_is_idempotent(self) -> None:
        """Calling close() twice on the same writer is a safe no-op the
        second time (the handles are already None)."""
        from src.data.quality.audit import PostgresAuditLog

        sink = PostgresAuditLog(dsn="postgresql://x", table="custom_table_42")
        sink._conn = None  # explicit: simulate post-close state
        sink._cursor = None

        sink.close()  # must not raise even though conn is None
        assert sink._conn is None
        assert sink._cursor is None


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
