"""Pydantic models for the Macro Agent (Phase 2.3).

Why frozen?
- The fetcher builds a snapshot, the classifier consumes it, the
  persistence layer writes it. We don't want a downstream function
  silently mutating the input and producing a regime label that doesn't
  match what was fetched.
- Mirrors the project's ``RiskGate`` frozen-pydantic pattern (Phase 1.1).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

RegimeLabel = Literal["risk_off", "risk_on_reduced", "neutral"]


class MacroSnapshot(BaseModel):
    """One observation of the three macro inputs at a moment in time.

    All numeric fields are ``Decimal`` so the classifier never sees a
    ``float`` (rounding errors in the 0.1% range would push USD/RUB
    Δ from "below 5%" to "above 5%" on borderline days).

    The ``*_prev`` fields are the prior-period close (5 days back for
    USD/RUB, 60 days back for IMOEX) used to compute the change that
    drives the regime label. The classifier never has to fetch — the
    fetcher populates these once.
    """

    model_config = ConfigDict(frozen=True)

    fetched_at: datetime = Field(..., description="When the fetcher snapshot was minted (UTC).")
    cbr_key_rate: Decimal = Field(..., description="CBR key rate as a percent, e.g. 16.00.")
    usdrub_close: Decimal = Field(..., description="USD/RUB CETS latest close.")
    usdrub_5d_prev: Decimal = Field(..., description="USD/RUB close 5 trading days ago.")
    imoex_close: Decimal = Field(..., description="IMOEX index latest close.")
    imoex_60d_prev: Decimal = Field(..., description="IMOEX index close 60 trading days ago.")
    sources: dict[str, str] = Field(
        default_factory=dict,
        description="Provenance map: input_name -> source identifier (URL or 'cache').",
    )


class MacroRegime(BaseModel):
    """Classifier output: a regime label + a risk-budget multiplier.

    Multiplier is in [0.5, 1.0]. Coordinator (Phase 2.10) multiplies its
    risk budget by this number; e.g. ``risk_off`` ⇒ 0.5 means "halve
    risk exposure".
    """

    model_config = ConfigDict(frozen=True)

    regime: RegimeLabel
    multiplier: Decimal = Field(..., ge=Decimal("0.5"), le=Decimal("1.0"))
    reason: str = Field(..., description="Human-readable why-this-label line, <= 200 chars.")
    snapshot: Optional[MacroSnapshot] = Field(
        default=None,
        description="Snapshot that produced this regime. Optional so tests can build bare regimes.",
    )
