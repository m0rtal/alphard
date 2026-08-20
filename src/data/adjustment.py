"""Split and dividend adjustments for OHLCV bars.

Why this module?
---------------- Phase 1.1 stores both ``close`` (raw exchange close) and
``adj_close`` (split-adjusted close) on every OHLCV bar. Phase 1.1 ships
the schema but ``adj_close = close`` is a placeholder — there is no
corporate-action processing yet. Phase 2.5 wires real adjustments.

What this module does
---------------------
``apply_split_adjustment`` takes a list of OHLCVRow + a list of
CorporateAction of kind="split" and returns new rows where:

  - Bars at or after the split date are unchanged (they already reflect
    the post-split regime from the source feed).
  - Bars strictly before the split date have open/high/low/close/adj_close
    scaled by 1/R where R is the split ratio (see "ratio convention").
  - Bars strictly before the split date have volume scaled by R (a 1:2
    split doubles the share count traded, so pre-split volume
    multiplied by 2 is comparable to post-split volume).

Why a pure function?
-------------------- Pure = no IO, no network, no DB. The same logic can be invoked
from:

  * A backfill-time script that recomputes adjusted bars after fetching
    corporate actions.
  * A live daemon that adjusts new bars as corporate actions arrive.
  * A backtester that needs the historical adjustment factors
    on demand.

Each call site owns its IO; the math is portable.

Ratio convention
----------------
We follow the same convention as Tinkoff's `dividends` feed and the
``CorporateAction`` model: ``value`` for a split is the ratio
``new_shares_outstanding / old_shares_outstanding``.

Examples
~~~~~~~~
* 1:2 split (1 share becomes 2): value=2.0 -> pre-split close halved.
* 1:10 reverse split (10 shares become 1): value=0.1 -> pre-split close
  multiplied by 10.
* 2:1 split (2 shares become 1): value=0.5 -> pre-split close doubled.

Negative or zero ratios are invalid and raise ``ValueError`` — split
arithmetic is undefined there.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from src.data.models import CorporateAction, OHLCVRow


def _split_factor(action: CorporateAction) -> Decimal:
    """Validate and return a split action's ratio.

    The factor equals ``action.value`` (new/old share ratio). The
    caller uses the factor to scale pre-split bars:

      - Prices / adj_close: multiply by (1 / factor).
      - Volume: multiply by factor.

    Raises ValueError on kind != 'split' or factor <= 0.
    """
    if action.kind != "split":
        raise ValueError(
            f"CorporateAction kind {action.kind!r} is not 'split'; "
            "apply_split_adjustment only handles splits. Dividends "
            "need a separate adjustment path (Phase 2.5 step 2)."
        )
    factor = action.value
    if factor <= Decimal("0"):
        raise ValueError(f"split ratio must be positive, got {factor} " f"(ticker={action.ticker}, ts={action.ts})")
    return factor


def _apply_one(row: OHLCVRow, factor: Decimal) -> OHLCVRow:
    """Return a new OHLCVRow with prices divided by factor and volume multiplied.

    Uses model_copy so the frozen OHLCVRow semantics are preserved.
    """
    inv_factor = Decimal("1") / factor
    return row.model_copy(
        update={
            "open": row.open * inv_factor,
            "high": row.high * inv_factor,
            "low": row.low * inv_factor,
            "close": row.close * inv_factor,
            "adj_close": row.adj_close * inv_factor,
            "volume": row.volume * factor,
        }
    )


def apply_split_adjustment(
    rows: Sequence[OHLCVRow],
    actions: Iterable[CorporateAction],
) -> list[OHLCVRow]:
    """Apply split adjustments to historical OHLCV bars.

    Parameters
    ----------
    rows : sequence of OHLCVRow
        OHLCV bars for a single ticker, sorted by ts ascending. Bars at
        or after the split date are returned unchanged (the source feed
        already factors in the post-split regime).
    actions : iterable of CorporateAction
        Corporate actions for the same ticker. Only entries with
        ``kind == "split"`` are processed; other kinds are ignored.
        Action ``ts`` is the effective split date (the first trading day
        on which the post-split price is observed).

    Returns
    -------
    list of OHLCVRow
        Adjusted bars, in the same order as the input. Rows not
        affected by any split are passed through unchanged (no copy).

    Notes
    -----
    * Multiple splits compose multiplicatively. A row at ts=T is
      adjusted by the product of (1/R_i) for every split i with
      ts_i > T. Volume gets the inverse product (R_i).
    * Idempotency: applying the same action list twice == applying
      once. Idempotency at the row level is achieved because we treat
      ``rows`` as immutable — we never mutate, only model_copy or
      pass-through.
    * Sort order: the output preserves the input row order.
    """
    splits = sorted(
        (a for a in actions if a.kind == "split"),
        key=lambda a: a.ts,
    )
    if not splits:
        return list(rows)

    out: list[OHLCVRow] = []
    for row in rows:
        # Compute the cumulative factor across every split that
        # landed AFTER this row's date (so the row is pre-split and
        # must be adjusted).
        cumulative_price_factor = Decimal("1")  # multiplies prices
        cumulative_volume_factor = Decimal("1")  # multiplies volume
        for action in splits:
            if action.ts <= row.ts:
                continue
            factor = _split_factor(action)
            # Prices are divided by R; volume is multiplied by R.
            cumulative_price_factor /= factor
            cumulative_volume_factor *= factor

        if cumulative_price_factor == Decimal("1"):
            out.append(row)
        else:
            # One model_copy with both adjustments.
            out.append(
                row.model_copy(
                    update={
                        "open": row.open * cumulative_price_factor,
                        "high": row.high * cumulative_price_factor,
                        "low": row.low * cumulative_price_factor,
                        "close": row.close * cumulative_price_factor,
                        "adj_close": row.adj_close * cumulative_price_factor,
                        "volume": row.volume * cumulative_volume_factor,
                    }
                )
            )
    return out


__all__ = ["apply_split_adjustment"]
