"""Deterministic macro regime classifier (Phase 2.3).

PURE function: ``classify(snapshot) -> MacroRegime``. No IO, no DB, no
``datetime.now()``, no random — every input produces the same output on
every run. This is the unit-testable core of the Macro Agent.

Rules (locked 2026-08-19, see issue #70):

    risk_off         if CBR > 15%      OR IMOEX drawdown > 20% over 60d
    risk_on_reduced  if USD/RUB Δ > 5% over 5d   (worst-case wins)
    neutral          otherwise

Multipliers:
    risk_off            -> 0.50
    risk_on_reduced     -> 0.75
    neutral             -> 1.00

"Drawdown" for IMOEX is a POSITIVE number: 0.20 means the index fell
20% from its 60d-prior close. We measure it as
``(imoex_60d_prev - imoex_close) / imoex_60d_prev``.

"Δ for USD/RUB" is also POSITIVE: 0.05 means the ruble weakened by 5%
versus the dollar over 5 trading days. We measure it as
``(usdrub_close - usdrub_5d_prev) / usdrub_5d_prev``.

The 4 branches:

    1. cbr > 0.15             -> risk_off            (0.50)
    2. imoex_dd > 0.20        -> risk_off            (0.50)
    3. usdrub_delta > 0.05    -> risk_on_reduced     (0.75)
    4. else                   -> neutral             (1.00)

When multiple triggers fire simultaneously, ``risk_off`` wins over
``risk_on_reduced`` (the wording in issue #70: "worst-case wins").

Edge cases:

    E1. ``usdrub_5d_prev == 0`` -> we treat Δ as 0 (cannot divide).
    E2. ``imoex_60d_prev == 0`` -> we treat drawdown as 0 (cannot divide).
    E3. ``cbr_key_rate < 0``    -> still risk_off? No — the rule is
        ``cbr > 0.15``. Negative CBR is not a thing in reality and would
        be a data bug; we log it via the ``reason`` field rather than
        silently triggering.
    E4. ``snapshot is None``    -> ArgumentError, NOT a silent neutral.
        The caller is responsible for not calling us with no data.

The 4 edge cases are covered in ``tests/test_regime.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from .models import MacroRegime, MacroSnapshot, RegimeLabel

# Thresholds — locked constants. Pulled out so tests can reference them
# by name rather than re-typing magic numbers.
THRESHOLD_CBR_HIGH: Final[Decimal] = Decimal("0.15")  # 15%
THRESHOLD_USDRUB_DELTA: Final[Decimal] = Decimal("0.05")  # 5% over 5 days
THRESHOLD_IMOEX_DRAWDOWN: Final[Decimal] = Decimal("0.20")  # 20% over 60 days

MULTIPLIER_NEUTRAL: Final[Decimal] = Decimal("1.00")
MULTIPLIER_REDUCED: Final[Decimal] = Decimal("0.75")
MULTIPLIER_OFF: Final[Decimal] = Decimal("0.50")


def _safe_delta(curr: Decimal, prev: Decimal) -> Decimal:
    """Return (curr - prev) / prev, or ``Decimal(0)`` if prev is 0.

    Kept as a public helper for callers who want both directions
    (drawdown vs appreciation). The classifier itself uses inline math
    in branches 2/3 so the E1/E2 zero-divisor paths are explicit.
    """
    if prev == 0:
        return Decimal("0")
    return (curr - prev) / prev


def classify(snapshot: MacroSnapshot | None) -> MacroRegime:
    """Map a ``MacroSnapshot`` to a ``MacroRegime``.

    Args:
        snapshot: the three inputs + their 5d/60d priors, OR None.

    Returns:
        ``MacroRegime`` with regime label + multiplier + reason string.

    Raises:
        ValueError: ``snapshot is None``. The fetcher builds an empty
            snapshot on total failure — it doesn't pass ``None`` in. If
            a caller does, refuse explicitly instead of silently
            returning neutral.
    """
    if snapshot is None:  # E4
        raise ValueError("classify() requires a MacroSnapshot; got None")

    triggers: list[str] = []
    diagnostics: list[str] = []

    # Branch 1: CBR > 15% ⇒ risk_off.
    if snapshot.cbr_key_rate > THRESHOLD_CBR_HIGH * Decimal("100"):
        triggers.append(f"CBR key rate {snapshot.cbr_key_rate}% > 15%")

    # Branch 2: IMOEX drawdown > 20% over 60d ⇒ risk_off.
    # Drawdown is positive when the index fell: (prior - curr) / prior.
    if snapshot.imoex_60d_prev == 0:
        diagnostics.append("IMOEX 60d prior is zero — dd treated as 0")
        imoex_dd = Decimal("0")
    else:
        imoex_dd = (snapshot.imoex_60d_prev - snapshot.imoex_close) / snapshot.imoex_60d_prev
        if imoex_dd > THRESHOLD_IMOEX_DRAWDOWN:
            triggers.append(f"IMOEX 60d drawdown {imoex_dd:.2%} > 20%")

    # Branch 3: USD/RUB Δ > 5% over 5d ⇒ risk_on_reduced.
    # Δ is positive when the ruble weakened: (curr - prior) / prior.
    if snapshot.usdrub_5d_prev == 0:
        diagnostics.append("USD/RUB 5d prior is zero — Δ treated as 0")
        usdrub_delta = Decimal("0")
    else:
        usdrub_delta = (snapshot.usdrub_close - snapshot.usdrub_5d_prev) / snapshot.usdrub_5d_prev
        if usdrub_delta > THRESHOLD_USDRUB_DELTA:
            triggers.append(f"USD/RUB 5d Δ {usdrub_delta:.2%} > 5%")

    # E3: negative CBR is not a "risk_off" — surface as diagnostic.
    if snapshot.cbr_key_rate < 0:
        diagnostics.append(f"CBR key rate {snapshot.cbr_key_rate}% < 0 (data error)")

    # Worst-case wins: risk_off > risk_on_reduced > neutral.
    has_off = any(t.startswith("CBR key rate") or t.startswith("IMOEX 60d drawdown") for t in triggers)
    has_reduced = any(t.startswith("USD/RUB 5d") for t in triggers)

    if has_off:
        label: RegimeLabel = "risk_off"
        multiplier = MULTIPLIER_OFF
    elif has_reduced:
        label = "risk_on_reduced"
        multiplier = MULTIPLIER_REDUCED
    else:
        label = "neutral"
        multiplier = MULTIPLIER_NEUTRAL

    parts = triggers + diagnostics
    reason = "; ".join(parts) if parts else "no triggers fired"
    return MacroRegime(
        regime=label,
        multiplier=multiplier,
        reason=reason,
        snapshot=snapshot,
    )


__all__ = [
    "classify",
    "THRESHOLD_CBR_HIGH",
    "THRESHOLD_USDRUB_DELTA",
    "THRESHOLD_IMOEX_DRAWDOWN",
    "MULTIPLIER_NEUTRAL",
    "MULTIPLIER_REDUCED",
    "MULTIPLIER_OFF",
]
