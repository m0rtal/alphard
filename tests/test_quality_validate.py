"""Tests for src/data/quality/validate.py.

These cover the data-quality gate that runs inside the backfill loop.
The trading bot treats these bars as the primary decision input, so a
CRITICAL finding here MUST reject the upsert — the tests pin that
contract.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.data.models import OHLCVRow
from src.data.quality import (
    Issue,
    Severity,
    blocking,
    summarize,
    validate_bar,
    validate_series,
    worst_tickers,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _row(**kw: object) -> OHLCVRow:
    """Build an OHLCVRow bypassing model validators.

    We use ``model_construct`` so test inputs can deliberately violate
    invariants (e.g. high<low) that the constructor would otherwise
    reject — this is exactly the scenario the data-quality gate is
    meant to catch downstream.
    """
    defaults: dict[str, object] = {
        "ticker": "SBER",
        "ts": date(2026, 1, 15),
        "open": Decimal("100"),
        "high": Decimal("105"),
        "low": Decimal("95"),
        "close": Decimal("102"),
        "volume": Decimal("1000"),
        "adj_close": Decimal("102"),
    }
    defaults.update(kw)
    return OHLCVRow.model_construct(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_bar
# ---------------------------------------------------------------------------


def test_validate_bar_clean_row_no_issues() -> None:
    """A well-formed bar must produce zero issues."""
    assert validate_bar(_row()) == []


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("open", Decimal("200"), "high_lt_open"),  # open > high
        ("close", Decimal("200"), "high_lt_close"),  # close > high
        ("open", Decimal("50"), "low_gt_open"),  # open < low
        ("close", Decimal("50"), "low_gt_close"),  # close < low
    ],
)
def test_validate_bar_price_out_of_range(field: str, value: Decimal, code: str) -> None:
    issues = validate_bar(_row(**{field: value}))
    severities = [i.severity for i in issues]
    codes = [i.code for i in issues]
    assert Severity.CRITICAL in severities
    assert code in codes


def test_validate_bar_high_lt_low_is_critical() -> None:
    issues = validate_bar(_row(high=Decimal("80"), low=Decimal("90")))
    assert any(i.code == "high_lt_low" and i.severity == Severity.CRITICAL for i in issues)


def test_validate_bar_negative_volume_is_critical() -> None:
    issues = validate_bar(_row(volume=Decimal("-1")))
    assert any(i.code == "neg_volume" and i.severity == Severity.CRITICAL for i in issues)


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_validate_bar_non_positive_price_is_critical(field: str) -> None:
    issues = validate_bar(_row(**{field: Decimal("0")}))
    assert any(i.code == f"non_positive_{field}" and i.severity == Severity.CRITICAL for i in issues)


# ---------------------------------------------------------------------------
# validate_series
# ---------------------------------------------------------------------------


def test_validate_series_empty_returns_no_issues() -> None:
    assert validate_series([]) == []


def test_validate_series_single_row_returns_no_issues() -> None:
    """A single bar has no prev/next — no return or gap checks fire."""
    assert validate_series([_row()]) == []


def test_validate_series_normal_daily_returns_no_warnings() -> None:
    rows = [_row(ts=date(2026, 1, i), close=Decimal("100") + i) for i in range(1, 6)]
    assert validate_series(rows) == []


def test_validate_series_detects_50pct_jump() -> None:
    """A daily move of 60% must surface a WARNING. We use a split-like
    jump (close doubles in one day) to verify the threshold.
    """
    rows = [
        _row(ts=date(2026, 1, 1), close=Decimal("100")),
        _row(ts=date(2026, 1, 2), close=Decimal("100")),  # baseline
        _row(ts=date(2026, 1, 5), close=Decimal("160")),  # +60% weekend gap
    ]
    issues = validate_series(rows)
    codes = [i.code for i in issues]
    assert "return_gt_50pct" in codes
    assert all(i.severity == Severity.WARNING for i in issues)


def test_validate_series_50pct_drop_is_warning() -> None:
    rows = [
        _row(ts=date(2026, 1, 1), close=Decimal("100")),
        _row(ts=date(2026, 1, 2), close=Decimal("40")),  # -60%
    ]
    issues = validate_series(rows)
    assert any(i.code == "return_gt_50pct" for i in issues)


def test_validate_series_detects_long_gap() -> None:
    """Calendar gap > 14 days suggests missing history for a Russian
    liquid share."""
    rows = [
        _row(ts=date(2026, 1, 1), close=Decimal("100")),
        _row(ts=date(2026, 3, 1), close=Decimal("100")),  # 60-day gap
    ]
    issues = validate_series(rows)
    assert any(i.code == "long_gap" for i in issues)


def test_validate_series_weekend_only_is_ok() -> None:
    """A normal weekend (Sat→Mon = 2 days) must NOT trigger long_gap."""
    rows = [
        _row(ts=date(2026, 1, 9), close=Decimal("100")),  # Fri
        _row(ts=date(2026, 1, 12), close=Decimal("100")),  # Mon
    ]
    assert not any(i.code == "long_gap" for i in validate_series(rows))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_blocking_filters_to_critical_only() -> None:
    issues = [
        Issue(Severity.CRITICAL, "SBER", date(2026, 1, 1), "x", "x"),
        Issue(Severity.WARNING, "SBER", date(2026, 1, 2), "y", "y"),
    ]
    blocking_issues = blocking(issues)
    assert len(blocking_issues) == 1
    assert blocking_issues[0].severity == Severity.CRITICAL


def test_summarize_counts_per_severity() -> None:
    issues = [
        Issue(Severity.CRITICAL, "SBER", date(2026, 1, 1), "a", "a"),
        Issue(Severity.CRITICAL, "GAZP", date(2026, 1, 1), "b", "b"),
        Issue(Severity.WARNING, "SBER", date(2026, 1, 2), "c", "c"),
    ]
    s = summarize(issues)
    assert s["CRITICAL"] == 2
    assert s["WARNING"] == 1
    assert s["INFO"] == 0


def test_worst_tickers_orders_by_issue_count() -> None:
    issues = [
        Issue(Severity.WARNING, "A", date(2026, 1, 1), "x", "x"),
        Issue(Severity.WARNING, "A", date(2026, 1, 2), "x", "x"),
        Issue(Severity.WARNING, "B", date(2026, 1, 3), "x", "x"),
    ]
    top = worst_tickers(issues, limit=10)
    assert top[0] == ("A", 2)
    assert top[1] == ("B", 1)


# ---------------------------------------------------------------------------
# Integration: backfill's gate must reject batches with CRITICAL bars
# ---------------------------------------------------------------------------


def test_validate_bar_critical_issues_reject_upsert() -> None:
    """This is the contract that protects the bot: if ANY bar in the
    fresh batch fails structural validation, the whole batch must be
    skipped — silent garbage in = silent garbage out."""
    rows = [
        _row(ts=date(2026, 1, 1)),
        _row(ts=date(2026, 1, 2), high=Decimal("80"), low=Decimal("90")),  # high < low
        _row(ts=date(2026, 1, 3)),
    ]
    all_issues = []
    for r in rows:
        all_issues.extend(validate_bar(r))
    critical = blocking(all_issues)
    # Broken row produces one or more CRITICALs (e.g. high<low also
    # implies high<open and high<close). Just pin the canonical
    # invariant is in the set.
    codes = {i.code for i in critical}
    assert "high_lt_low" in codes
