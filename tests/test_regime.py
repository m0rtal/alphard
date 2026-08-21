"""Tests for src.macro.regime (Phase 2.3 Macro Agent).

The classifier is PURE — no IO, no datetime.now(), no random — so every
test is a deterministic input → output assertion. We exercise:

* The 4 branches (CBR high, IMOEX drawdown, USD/RUB Δ, neutral).
* The 4 edge cases (zero prior denominators, negative CBR, None input).
* The worst-case-wins ordering (risk_off > risk_on_reduced > neutral).
* Threshold boundary behaviour (>= vs > at 15.00% CBR, 5% USD/RUB, 20% IMOEX).
* Reason-string contents — operators grep on these in production logs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.macro.regime import (
    MULTIPLIER_NEUTRAL,
    MULTIPLIER_OFF,
    MULTIPLIER_REDUCED,
    PERCENT_SCALE,
    THRESHOLD_CBR_HIGH,
    THRESHOLD_IMOEX_DRAWDOWN,
    THRESHOLD_USDRUB_DELTA,
    _safe_delta,
    _safe_delta_percent,
    classify,
)
from src.macro.models import MacroSnapshot


def _snap(
    *,
    cbr: str = "10.00",
    usd: str = "90.0000",
    usd_5d: str = "88.0000",
    imoex: str = "3000.00",
    imoex_60d: str = "3000.00",
) -> MacroSnapshot:
    return MacroSnapshot(
        fetched_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        cbr_key_rate=Decimal(cbr),
        usdrub_close=Decimal(usd),
        usdrub_5d_prev=Decimal(usd_5d),
        imoex_close=Decimal(imoex),
        imoex_60d_prev=Decimal(imoex_60d),
        sources={"cbr": "test", "usdrub": "test", "imoex": "test"},
    )


# ---------------------------------------------------------------------------
# The 4 branches
# ---------------------------------------------------------------------------


def test_branch_cbr_above_threshold_triggers_risk_off() -> None:
    """CBR 16.00% > 15.00% → risk_off, multiplier 0.50."""
    reg = classify(_snap(cbr="16.00"))
    assert reg.regime == "risk_off"
    assert reg.multiplier == MULTIPLIER_OFF
    assert "CBR key rate 16.00% > 15%" in reg.reason


def test_branch_imoex_drawdown_triggers_risk_off() -> None:
    """IMOEX fell ~26.67% over 60d → risk_off."""
    reg = classify(_snap(imoex="2200.00", imoex_60d="3000.00"))
    assert reg.regime == "risk_off"
    assert reg.multiplier == MULTIPLIER_OFF
    assert "IMOEX 60d drawdown 26.67% > 20%" in reg.reason


def test_branch_usdrub_delta_triggers_risk_on_reduced() -> None:
    """USD/RUB moved from 90 to 95 (Δ = 5.55% > 5%) → risk_on_reduced."""
    reg = classify(_snap(usd="95.0000", usd_5d="90.0000"))
    assert reg.regime == "risk_on_reduced"
    assert reg.multiplier == MULTIPLIER_REDUCED
    assert "USD/RUB 5d Δ 5.56% > 5%" in reg.reason


def test_branch_neutral_when_no_triggers() -> None:
    """All inputs calm → neutral, multiplier 1.00."""
    reg = classify(_snap(cbr="8.00", usd="90.0000", usd_5d="89.0000", imoex="3000.00", imoex_60d="2950.00"))
    assert reg.regime == "neutral"
    assert reg.multiplier == MULTIPLIER_NEUTRAL
    assert reg.reason == "no triggers fired"


# ---------------------------------------------------------------------------
# Worst-case wins
# ---------------------------------------------------------------------------


def test_risk_off_wins_over_risk_on_reduced_when_both_trigger() -> None:
    """CBR high AND USD/RUB Δ both fire → risk_off dominates."""
    reg = classify(_snap(cbr="20.00", usd="95.0000", usd_5d="90.0000"))
    assert reg.regime == "risk_off"
    assert reg.multiplier == MULTIPLIER_OFF
    assert "CBR key rate" in reg.reason
    assert "USD/RUB 5d" in reg.reason


# ---------------------------------------------------------------------------
# Threshold boundaries (>, not >=; locked in regime.py)
# ---------------------------------------------------------------------------


def test_cbr_exactly_15_percent_is_not_risk_off() -> None:
    """Boundary: 15.00% CBR is NOT risk_off (rule is strict >)."""
    reg = classify(_snap(cbr="15.00"))
    # Could be neutral OR risk_on_reduced if USD/RUB also moves. Use calm USD:
    assert reg.regime != "risk_off"


def test_cbr_just_above_15_is_risk_off() -> None:
    reg = classify(_snap(cbr="15.01"))
    assert reg.regime == "risk_off"


def test_usdrub_exactly_5_percent_delta_is_not_reduced() -> None:
    """Boundary: USD/RUB Δ = exactly 5.00% is NOT risk_on_reduced."""
    reg = classify(_snap(usd="94.5000", usd_5d="90.0000"))  # Δ = 5.0%
    assert reg.regime != "risk_on_reduced"


def test_imoex_exactly_20_percent_drawdown_is_not_risk_off() -> None:
    """Boundary: IMOEX dd = exactly 20.0% is NOT risk_off."""
    reg = classify(_snap(imoex="2400.00", imoex_60d="3000.00"))  # dd = 20.0%
    assert reg.regime != "risk_off"


# ---------------------------------------------------------------------------
# The 4 edge cases
# ---------------------------------------------------------------------------


def test_edge_e1_usdrub_5d_prev_zero_treated_as_zero_delta() -> None:
    """E1: usdrub_5d_prev == 0 → Δ is treated as 0 (no divide-by-zero)."""
    reg = classify(_snap(usd="95.0000", usd_5d="0.0000"))
    # Δ = 0 → no USD/RUB trigger. Should NOT raise.
    # The diagnostic IS surfaced, but no trigger fires.
    assert reg.regime == "neutral"
    assert "USD/RUB 5d prior is zero" in reg.reason
    # Crucially, the trigger string "USD/RUB 5d Δ" must NOT be present.
    assert "USD/RUB 5d Δ" not in reg.reason


def test_edge_e2_imoex_60d_prev_zero_treated_as_zero_drawdown() -> None:
    """E2: imoex_60d_prev == 0 → drawdown treated as 0."""
    reg = classify(_snap(imoex="2400.00", imoex_60d="0.00"))
    # The reason surfaces the data gap but does NOT trigger risk_off.
    assert "IMOEX 60d prior is zero" in reg.reason
    assert reg.regime == "neutral"
    # Crucially, the trigger string must NOT be present.
    assert "IMOEX 60d drawdown" not in reg.reason


def test_edge_e3_negative_cbr_does_not_silently_trigger_risk_off() -> None:
    """E3: CBR -5% should not crash AND should not silently trigger."""
    reg = classify(_snap(cbr="-5.00"))
    # The rule is `cbr > 0.15`, so -5 < 0.15 → no trigger.
    assert reg.regime == "neutral"
    # Crucially, the trigger string "CBR key rate XX% > 15%" must NOT be present.
    assert "> 15%" not in reg.reason
    # The data-error diagnostic IS surfaced.
    assert "data error" in reg.reason


def test_edge_e4_none_snapshot_raises() -> None:
    """E4: classify(None) MUST raise — caller is responsible for the snapshot."""
    with pytest.raises(ValueError, match="requires a MacroSnapshot"):
        classify(None)


# ---------------------------------------------------------------------------
# Reason-string stability (operators grep these in production)
# ---------------------------------------------------------------------------


def test_reason_contains_all_fired_triggers() -> None:
    """When two triggers fire, the reason lists BOTH."""
    reg = classify(_snap(cbr="20.00", imoex="2200.00", imoex_60d="3000.00"))
    assert "CBR key rate" in reg.reason
    assert "IMOEX 60d drawdown" in reg.reason


def test_snapshot_is_attached_to_regime() -> None:
    """The regime carries the snapshot so downstream can audit."""
    snap = _snap(cbr="16.00")
    reg = classify(snap)
    assert reg.snapshot is snap


def test_regime_is_immutable() -> None:
    """MacroRegime is frozen; mutating post-construction is rejected."""
    from pydantic import ValidationError

    from src.macro.models import MacroRegime

    reg = MacroRegime(regime="neutral", multiplier=Decimal("1.0"), reason="x")
    with pytest.raises(ValidationError):
        reg.regime = "risk_off"  # type: ignore[misc]


def test_classifier_is_deterministic() -> None:
    """Same input → same output, twice in a row."""
    snap = _snap(cbr="20.00", usd="95.0000", usd_5d="90.0000", imoex="2400.00", imoex_60d="3000.00")
    a = classify(snap)
    b = classify(snap)
    assert a.regime == b.regime
    assert a.multiplier == b.multiplier
    assert a.reason == b.reason


# ---------------------------------------------------------------------------
# _safe_delta helper
# ---------------------------------------------------------------------------


def test_safe_delta_returns_zero_when_prev_is_zero() -> None:
    assert _safe_delta(Decimal("100"), Decimal("0")) == Decimal("0")


def test_safe_delta_computes_normal_delta() -> None:
    assert _safe_delta(Decimal("105"), Decimal("100")) == Decimal("0.05")


# ---------------------------------------------------------------------------
# _safe_delta_percent helper (issue #88 — percent canonicalization)
# ---------------------------------------------------------------------------


def test_safe_delta_percent_returns_zero_when_prev_is_zero() -> None:
    """Same zero-divisor behaviour as ``_safe_delta``, but in percent."""
    assert _safe_delta_percent(Decimal("100"), Decimal("0")) == Decimal("0")


def test_safe_delta_percent_computes_percent_delta() -> None:
    """5/100 = 5.0% (not 0.05)."""
    assert _safe_delta_percent(Decimal("105"), Decimal("100")) == Decimal("5.00")


def test_percent_scale_is_100() -> None:
    """Sanity guard: the percent constant is exactly 100.

    A future refactor that swaps PERCENT_SCALE for ``0.01`` (or anything
    else) would silently flip the regime semantics. This test makes the
    intent explicit.
    """
    assert PERCENT_SCALE == Decimal("100")


def test_thresholds_are_in_percent_not_fraction() -> None:
    """Issue #88: all three thresholds are now percent values.

    - ``THRESHOLD_CBR_HIGH``          must be 15.00  (not 0.15)
    - ``THRESHOLD_IMOEX_DRAWDOWN``    must be 20.00  (not 0.20)
    - ``THRESHOLD_USDRUB_DELTA``      must be 5.00   (not 0.05)

    If a refactor reintroduces the fraction representation, the
    classifier will silently mis-trigger (or never trigger).
    """
    assert THRESHOLD_CBR_HIGH == Decimal("15.00")
    assert THRESHOLD_IMOEX_DRAWDOWN == Decimal("20.00")
    assert THRESHOLD_USDRUB_DELTA == Decimal("5.00")


def test_cbr_threshold_is_strict_greater_than_at_exactly_15() -> None:
    """Issue #88: boundary check at exactly 15.00 is NOT risk_off.

    The rule is ``cbr > 15.00`` (strict). Exactly 15.00 is a no-op.
    Tests both the just-below and exactly-at cases through the reason
    string so a regression in the comparator surfaces immediately.
    """
    # Exactly 15.00: no trigger.
    reg = classify(_snap(cbr="15.00"))
    assert reg.regime == "neutral"
    assert "> 15%" not in reg.reason

    # Just below 15.00: no trigger.
    reg = classify(_snap(cbr="14.99"))
    assert reg.regime == "neutral"
    assert "> 15%" not in reg.reason


def test_imoex_drawdown_uses_percent_comparator() -> None:
    """Sanity: 20.0% drawdown = exactly at threshold → NOT risk_off.

    After the issue #88 refactor, the comparator is ``> 20.00`` (percent),
    not ``> 0.20`` (fraction). Both produce the same arithmetic outcome
    on valid inputs, but the unit documentation now matches the input.
    """
    # 20.0% drawdown exactly: no trigger.
    reg = classify(_snap(imoex="2400.00", imoex_60d="3000.00"))
    assert reg.regime == "neutral"

    # 20.01% drawdown: risk_off.
    reg = classify(_snap(imoex="2399.40", imoex_60d="3000.00"))
    assert reg.regime == "risk_off"
