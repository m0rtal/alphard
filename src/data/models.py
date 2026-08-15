"""Shared pydantic models for the Data Agent.

Why central models?
-------------------
Both ``DataLoader`` (network side) and ``DataStore`` (DB side) speak the
same wire types. Putting them in one module prevents drift between the
two contracts — if we ever evolve the schema, this file is the single
point of edit.

Why pydantic, not dataclass?
----------------------------
Validation is part of the loader's job. A malformed API response must
fail loudly at the boundary, not silently propagate NaN-valued prices
into the DB. Phase 0 risk gate already established pydantic-everywhere
as the house style.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Tinkoff / MOEX tickers are short (e.g. "SBER", "GAZP", "YDEX"), but
# allow up to 12 chars for class codes and prefix-suffixed instruments
# (e.g. "SBERP", "GAZPR", "SU26238RMFS0"). Phase 1.3 may add qualifier
# tickers (e.g. "SBER@SPB") — keep the cap generous.
TICKER_REGEX = re.compile(r"^[A-Z0-9@._-]{1,12}$")

# Source tag for audit trail. Two letters keeps the column compact and
# matches Phase 1.1 schema spec.
SourceType = Literal["tkf", "moex", "manual"]


class OHLCVRow(BaseModel):
    """One daily OHLCV bar.

    Notes
    -----
    - ``adj_close`` is the split-adjusted close (dividends NOT adjusted
      out — total-return index is Phase 2). For a 1:2 split, the
      un-adjusted close is unchanged in the source feed; ``adj_close`` is
      halved so backtests on a single share remain comparable.
    - ``volume`` is shares traded (not lots). Tinkoff reports shares
      directly; MOEX ISS reports lots and we multiply by lot size.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(..., description="Ticker symbol, e.g. 'SBER'")
    ts: date = Field(..., description="Trading date (UTC calendar)")
    open: Decimal = Field(..., ge=Decimal("0"))
    high: Decimal = Field(..., ge=Decimal("0"))
    low: Decimal = Field(..., ge=Decimal("0"))
    close: Decimal = Field(..., ge=Decimal("0"))
    volume: Decimal = Field(..., ge=Decimal("0"))
    adj_close: Decimal = Field(..., ge=Decimal("0"))

    @field_validator("ticker")
    @classmethod
    def _v_ticker(cls, v: str) -> str:
        v = v.upper().strip()
        if not TICKER_REGEX.match(v):
            raise ValueError(f"invalid ticker {v!r}: must match {TICKER_REGEX.pattern}")
        return v

    @model_validator(mode="after")
    def _v_ohlc_consistency(self) -> "OHLCVRow":
        # Order in the source schema is: high >= max(open, close) >= min(open, close) >= low.
        # We check the structural invariants (low <= open/close <= high and low <= high).
        if self.high < self.low:
            raise ValueError(f"high {self.high} < low {self.low}")
        if self.high < self.open or self.high < self.close:
            raise ValueError(f"high {self.high} below open/close")
        if self.low > self.open or self.low > self.close:
            raise ValueError(f"low {self.low} above open/close")
        return self


class CorporateAction(BaseModel):
    """Split / dividend / ticker-change event.

    Phase 1.1 only uses SPLIT (split / reverse-split). DIVIDEND arrives in
    Phase 2 when total-return index is built. CHANGE handles ticker
    rename / FIGI reuse — important for survivorship-aware backtests.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    ts: date
    kind: Literal["split", "dividend", "change"]
    # For splits: ratio numerator (e.g. 2 for 1:2 split producing 2x shares).
    # For dividends: cash per share in listing currency.
    # For change: new ticker symbol.
    value: Decimal = Field(..., description="See kind for unit semantics")
    source: SourceType

    @field_validator("ticker")
    @classmethod
    def _v_ticker(cls, v: str) -> str:
        v = v.upper().strip()
        if not TICKER_REGEX.match(v):
            raise ValueError(f"invalid ticker {v!r}")
        return v


class TickerMeta(BaseModel):
    """Ticker universe entry — name, FIGI, lot, status, listing date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    figi: str | None = Field(default=None, description="Tinkoff FIGI (12 chars)")
    name: str = Field(..., min_length=1)
    lot: int = Field(..., gt=0, description="Trade lot size (shares per lot)")
    isin: str | None = Field(default=None, description="ISIN, e.g. 'RU0009029542'")
    currency: str = Field(default="RUB", min_length=3, max_length=3)
    # MOEX class code: TQBR (shares), TQOB (OFZ), TQCB (corp/muni), TQTE (ETFs), CETS (currencies)
    class_code: str | None = Field(default=None, description="MOEX class code (TQBR/TQOB/TQCB/TQTE/CETS)")  # noqa: E501
    delisted: bool = Field(default=False)
    delisted_at: date | None = None
    listed_at: date | None = None
    source: SourceType

    @field_validator("ticker")
    @classmethod
    def _v_ticker(cls, v: str) -> str:
        v = v.upper().strip()
        if not TICKER_REGEX.match(v):
            raise ValueError(f"invalid ticker {v!r}")
        return v
