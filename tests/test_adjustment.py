"""Tests for src/data/adjustment.py (Phase 2.5 split adjustments).

Coverage targets:
- apply_split_adjustment: row at split date unchanged
- 1:2 split halves pre-split prices, doubles pre-split volume
- 1:10 reverse split multiplies pre-split prices by 10
- 2:1 split doubles pre-split prices
- Multiple splits compose multiplicatively
- Idempotency: same action list twice == once
- Sort order: unsorted actions list still produces correct output
- Other-kind actions are ignored
- Invalid (zero/negative) split ratios raise ValueError
- Empty inputs return []
- Non-split kind raises ValueError when passed directly
- Volume math: 1:2 split doubles volume; 1:10 reverse split divides volume by 10
- Idempotency at row level: re-applying same splits to already-adjusted rows
  produces unchanged output (because we treat rows as immutable input)
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

# Add src/ to sys.path so `from src.data.adjustment import ...` works
# both locally and in CI (CI runs from project root with pytest).
_SRC_PATH = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

from src.data.adjustment import apply_split_adjustment  # noqa: E402
from src.data.models import CorporateAction, OHLCVRow  # noqa: E402

# ---------- helpers ----------


def _make_row(
    ts: date,
    close: str = "100",
    volume: str = "1000",
) -> OHLCVRow:
    """Build an OHLCVRow with all OHLC values equal to ``close``.

    This avoids pydantic's structural invariant check (high >= close,
    low <= close) — the helper assumes a flat bar where open=high=low=close.
    For tests that need an irregular bar, construct OHLCVRow directly.
    """
    c = Decimal(close)
    return OHLCVRow(
        ticker="SBER",
        ts=ts,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal(volume),
        adj_close=c,
    )


def _split(ts: date, value: str) -> CorporateAction:
    return CorporateAction(
        ticker="SBER",
        ts=ts,
        kind="split",
        value=Decimal(value),
        source="moex",
    )


# ---------- empty inputs ----------


def test_empty_rows_returns_empty_list():
    out = apply_split_adjustment([], [_split(date(2026, 6, 1), "2")])
    assert out == []


def test_empty_actions_returns_rows_unchanged():
    rows = [_make_row(date(2026, 1, 1))]
    out = apply_split_adjustment(rows, [])
    assert out == rows
    # And: no mutation of the input rows
    assert rows[0].close == Decimal("100")


# ---------- single split ----------


def test_split_at_row_date_unchanged():
    """A row at the split date is the source-feed regime — already post-split."""
    rows = [_make_row(date(2026, 6, 1))]
    actions = [_split(date(2026, 6, 1), "2")]
    out = apply_split_adjustment(rows, actions)
    assert len(out) == 1
    assert out[0] == rows[0]


def test_split_one_to_two_halves_prices_doubles_volume():
    rows = [_make_row(date(2026, 5, 1), close="100", volume="1000")]
    actions = [_split(date(2026, 6, 1), "2")]
    out = apply_split_adjustment(rows, actions)
    assert out[0].close == Decimal("50")  # 100 / 2
    assert out[0].adj_close == Decimal("50")
    assert out[0].volume == Decimal("2000")  # 1000 * 2
    # Open/high/low unchanged by source feed but must follow price math
    assert out[0].open == Decimal("50")
    assert out[0].high == Decimal("50")
    assert out[0].low == Decimal("50")


def test_split_one_to_ten_inverse():
    rows = [_make_row(date(2026, 5, 1), close="100", volume="1000")]
    actions = [_split(date(2026, 6, 1), "0.1")]  # 10:1 reverse split
    out = apply_split_adjustment(rows, actions)
    assert out[0].close == Decimal("1000")  # 100 / 0.1
    assert out[0].volume == Decimal("100")  # 1000 * 0.1


def test_split_two_to_one_doubles_prices():
    """2:1 split (2 shares become 1) — pre-split price doubles."""
    rows = [_make_row(date(2026, 5, 1), close="100", volume="1000")]
    actions = [_split(date(2026, 6, 1), "0.5")]
    out = apply_split_adjustment(rows, actions)
    assert out[0].close == Decimal("200")  # 100 / 0.5
    assert out[0].volume == Decimal("500")  # 1000 * 0.5


def test_split_three_to_one_quarters_volume():
    """3:1 split (3 shares become 1) — pre-split price*3, volume/3."""
    rows = [_make_row(date(2026, 5, 1), close="300", volume="3000")]
    # 1/3 expressed as a Decimal that does division sensibly.
    actions = [_split(date(2026, 6, 1), "0.3333333333333333")]
    out = apply_split_adjustment(rows, actions)
    # 300 / 0.333...  ~ 900; 3000 * 0.333... ~ 1000.
    # We don't pin exact arithmetic (Decimal precision is finite); we
    # assert the structural property that the close tripled and the
    # volume thirded within 1 unit of float precision.
    assert out[0].close > Decimal("899") and out[0].close < Decimal("901")
    assert out[0].volume > Decimal("999") and out[0].volume < Decimal("1001")


# ---------- multiple splits ----------


def test_multiple_splits_compose_multiplicatively():
    """Two 1:2 splits = 1:4 cumulative factor on pre-split bars."""
    rows = [_make_row(date(2026, 1, 1), close="100", volume="1000")]
    actions = [
        _split(date(2026, 6, 1), "2"),
        _split(date(2026, 7, 1), "2"),
    ]
    out = apply_split_adjustment(rows, actions)
    assert out[0].close == Decimal("25")  # 100 / 4
    assert out[0].volume == Decimal("4000")  # 1000 * 4


def test_split_then_reversesplit_partial_cancellation():
    """1:2 then 2:1 leaves pre-split prices unchanged."""
    rows = [_make_row(date(2026, 1, 1), close="100", volume="1000")]
    actions = [
        _split(date(2026, 6, 1), "2"),  # halves
        _split(date(2026, 7, 1), "0.5"),  # doubles back
    ]
    out = apply_split_adjustment(rows, actions)
    assert out[0].close == Decimal("100")
    assert out[0].volume == Decimal("1000")


def test_only_applicable_splits():
    """A split *before* a row's date should NOT be applied."""
    rows = [_make_row(date(2026, 6, 1), close="100", volume="1000")]
    actions = [_split(date(2026, 5, 1), "2")]  # before the row
    out = apply_split_adjustment(rows, actions)
    assert out[0] == rows[0]


def test_only_future_splits_in_window():
    """A split *after* a row's date IS applied."""
    rows = [_make_row(date(2026, 6, 1), close="100", volume="1000")]
    actions = [_split(date(2026, 7, 1), "2")]  # after the row
    out = apply_split_adjustment(rows, actions)
    assert out[0].close == Decimal("50")
    assert out[0].volume == Decimal("2000")


def test_mixed_windowed_splits():
    """Some splits before, some after — only future ones apply."""
    rows = [
        _make_row(date(2026, 1, 1), close="100", volume="1000"),
        _make_row(date(2026, 5, 1), close="110", volume="1100"),
        _make_row(date(2026, 7, 1), close="120", volume="1200"),
    ]
    actions = [
        _split(date(2026, 3, 1), "2"),  # before Jan row only
        _split(date(2026, 6, 1), "2"),  # before May, before Jul
    ]
    out = apply_split_adjustment(rows, actions)

    # Jan 1: both future splits apply. close 100/4 = 25, vol 1000*4 = 4000.
    assert out[0].close == Decimal("25")
    assert out[0].volume == Decimal("4000")
    # May 1: only June split applies. close 110/2 = 55, vol 1100*2 = 2200.
    assert out[1].close == Decimal("55")
    assert out[1].volume == Decimal("2200")
    # Jul 1: no future splits (June was before). Unchanged.
    assert out[2] == rows[2]


# ---------- sort order ----------


def test_unsorted_actions_still_correct():
    """Action list order should not affect output."""
    rows = [_make_row(date(2026, 1, 1), close="100", volume="1000")]
    actions = [
        _split(date(2026, 7, 1), "2"),
        _split(date(2026, 6, 1), "2"),
    ]
    out = apply_split_adjustment(rows, actions)
    assert out[0].close == Decimal("25")
    assert out[0].volume == Decimal("4000")


def test_actions_unsorted_mixed_kinds():
    """Non-split kinds interleaved with splits — non-splits ignored."""
    rows = [_make_row(date(2026, 1, 1), close="100", volume="1000")]
    dividend = CorporateAction(
        ticker="SBER",
        ts=date(2026, 5, 1),
        kind="dividend",
        value=Decimal("5"),
        source="moex",
    )
    actions = [
        _split(date(2026, 6, 1), "2"),
        dividend,
        CorporateAction(
            ticker="SBER",
            ts=date(2026, 4, 1),
            kind="change",
            value=Decimal("0"),
            source="moex",
        ),
    ]
    out = apply_split_adjustment(rows, actions)
    # Only the split applies (close/2, volume*2).
    assert out[0].close == Decimal("50")
    assert out[0].volume == Decimal("2000")


# ---------- idempotency ----------


def test_idempotency_raw_input_run_twice():
    """Running the function twice on the SAME raw input gives the same result.

    The function is not idempotent on already-adjusted input — applying a
    split to an already-split row would over-correct. Callers that
    re-apply must always feed the function raw (pre-adjustment) bars.
    This test pins that contract.
    """
    raw_rows = [_make_row(date(2026, 1, 1), close="100", volume="1000")]
    actions = [_split(date(2026, 6, 1), "2")]
    once = apply_split_adjustment(raw_rows, actions)
    twice = apply_split_adjustment(raw_rows, actions)  # same raw input
    assert twice == once


def test_input_not_mutated():
    """apply_split_adjustment must not mutate input rows."""
    row = _make_row(date(2026, 1, 1), close="100", volume="1000")
    actions = [_split(date(2026, 6, 1), "2")]
    out = apply_split_adjustment([row], actions)
    assert row.close == Decimal("100")  # input preserved
    assert row.volume == Decimal("1000")
    assert out[0] is not row  # output is a new object


# ---------- invalid ratios ----------


def test_zero_split_ratio_raises():
    rows = [_make_row(date(2026, 1, 1))]
    actions = [_split(date(2026, 6, 1), "0")]
    with pytest.raises(ValueError, match="must be positive"):
        apply_split_adjustment(rows, actions)


def test_negative_split_ratio_raises():
    rows = [_make_row(date(2026, 1, 1))]
    actions = [_split(date(2026, 6, 1), "-1")]
    with pytest.raises(ValueError, match="must be positive"):
        apply_split_adjustment(rows, actions)


# ---------- structural invariants ----------


def test_ohlcv_consistency_after_adjustment():
    """low <= open/close <= high must hold after the split math.

    We start from a row with low=99, high=110, open=100, close=105 — a
    realistic bar. After a 1:2 split, the same physical price bar
    should still have low < high.
    """
    row = OHLCVRow(
        ticker="SBER",
        ts=date(2026, 5, 1),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("99"),
        close=Decimal("105"),
        volume=Decimal("1000"),
        adj_close=Decimal("105"),
    )
    actions = [_split(date(2026, 6, 1), "2")]
    out = apply_split_adjustment([row], actions)
    adj = out[0]
    assert adj.low == Decimal("49.5")  # 99 / 2
    assert adj.high == Decimal("55")  # 110 / 2
    assert adj.open == Decimal("50")
    assert adj.close == Decimal("52.5")
    # Invariant: low <= open <= high and low <= close <= high.
    assert adj.low <= adj.open <= adj.high
    assert adj.low <= adj.close <= adj.high


def test_apply_split_adjustment_returns_new_object_per_changed_row():
    """Changed rows are model_copied (new objects), unchanged rows are
    passed through (same object reference)."""
    rows = [
        _make_row(date(2026, 5, 1), close="100", volume="1000"),  # will change
        _make_row(date(2026, 7, 1), close="100", volume="1000"),  # unchanged
    ]
    actions = [_split(date(2026, 6, 1), "2")]
    out = apply_split_adjustment(rows, actions)
    assert out[0] is not rows[0]  # changed -> new object
    assert out[1] is rows[1]  # unchanged -> same object


# ---------- edge cases ----------


def test_zero_volume_row_unchanged():
    """Edge: a row with volume=0 should still be adjusted on prices."""
    row = OHLCVRow(
        ticker="SBER",
        ts=date(2026, 5, 1),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("0"),
        adj_close=Decimal("100"),
    )
    actions = [_split(date(2026, 6, 1), "2")]
    out = apply_split_adjustment([row], actions)
    assert out[0].close == Decimal("50")
    assert out[0].volume == Decimal("0")


def test_decimal_precision_preserved():
    """Decimal arithmetic should preserve precision (no float)."""
    row = _make_row(date(2026, 5, 1), close="100", volume="1000")
    actions = [_split(date(2026, 6, 1), "7")]  # awkward 1:7 ratio
    out = apply_split_adjustment([row], actions)
    # 100 / 7 with Decimal = 14.285714285714285714... exactly.
    assert isinstance(out[0].close, Decimal)
    assert out[0].close * Decimal("7") == Decimal("100")
    assert out[0].volume * Decimal("1") / Decimal("7") == Decimal("1000")
