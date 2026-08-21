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

``apply_dividend_adjustment`` takes a list of OHLCVRow + a list of
CorporateAction of kind="dividend" and returns new rows where:

  - Only ``adj_close`` is modified (raw open/high/low/close/volume
    are left untouched — dividends are a return-of-capital event, not
    a price-multiplier event).
  - Bars strictly before the dividend ex-date have ``adj_close``
    reduced by the dividend amount per share.
  - Bars at or after the dividend ex-date are returned unchanged (the
    dividend has already been paid; the investor who held through
    the ex-date already received the cash).

``apply_adjustment`` is the unified entry point: it composes the
split and dividend stages in that order (splits first, because
dividends are quoted in the post-split-share currency) and is the
single function the orchestrator pipeline should call.

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


def _dividend_amount(action: CorporateAction) -> Decimal:
    """Validate and return a dividend action's cash-per-share value.

    The amount is ``action.value`` (RUB per share for the listing
    currency). The caller subtracts the cumulative dividend sum from
    ``adj_close`` on every bar with ``ts < ex_date``.

    Raises ValueError on kind != 'dividend' or amount < 0.
    """
    if action.kind != "dividend":
        raise ValueError(
            f"CorporateAction kind {action.kind!r} is not 'dividend'; "
            "apply_dividend_adjustment only handles dividends. Splits "
            "need a separate adjustment path (apply_split_adjustment)."
        )
    amount = action.value
    if amount < Decimal("0"):
        raise ValueError(
            f"dividend amount must be non-negative, got {amount} " f"(ticker={action.ticker}, ts={action.ts})"
        )
    return amount


def apply_dividend_adjustment(
    rows: Sequence[OHLCVRow],
    actions: Iterable[CorporateAction],
) -> list[OHLCVRow]:
    """Apply dividend adjustments to ``adj_close`` on historical OHLCV bars.

    Why this exists
    ---------------
    Phase 1.1 stores both ``close`` (raw exchange close) and
    ``adj_close`` (split-adjusted close) on every bar. The split-only
    pipeline (PR #45 + #74) makes ``adj_close`` comparable across
    splits but still ignores dividends. A bar before an ex-dividend
    date is *not* directly comparable to a bar after: the cash dividend
    caused the price to drop on the ex-date, so the raw close series
    has a discontinuity. To build a continuous "total return" series,
    we subtract every dividend whose ex-date lands AFTER the bar's
    date from that bar's ``adj_close``.

    What gets adjusted
    ------------------
    Only ``adj_close``. Raw ``close``/``open``/``high``/``low`` and
    ``volume`` are NEVER modified by dividends — dividends are a
    return-of-capital event, not a price-multiplier event. This
    matches the convention used by Tinkoff/MOEX ``adj_close`` series
    and standard total-return-index math (the price-level series stays
    raw so the operator can always audit "what the market actually
    showed on day X").

    Convention
    ----------
    For a bar at ``ts = T`` and a dividend with ``ts = D`` (the ex-date,
    first trading day on which the dividend is no longer attached):

      - If ``D > T`` (dividend's ex-date is in the future relative to
        the bar): subtract the dividend amount from ``adj_close``.
        Rationale: any investor who owned the share at close T and
        held it through D would have received the dividend; to make
        T's price directly comparable to a post-D price we must
        deduct the dividend so the cash is "moved" out of the price.
      - If ``D <= T`` (bar is on or after the ex-date): no change.
        The investor who held through the ex-date already received
        the cash; subsequent prices are post-dividend by construction.

    Multiple dividends compose by summation: a row is discounted by
    the sum of every dividend whose ex-date is strictly greater than
    the row's date.

    Parameters
    ----------
    rows : sequence of OHLCVRow
        OHLCV bars for a single ticker, sorted by ts ascending.
    actions : iterable of CorporateAction
        Corporate actions for the same ticker. Only entries with
        ``kind == "dividend"`` are processed; other kinds are ignored.
        Action ``ts`` is the ex-dividend date.

    Returns
    -------
    list of OHLCVRow
        Adjusted bars, in the same order as the input. Rows not
        affected by any dividend are passed through unchanged (no copy).

    Notes
    -----
    * Idempotency: applying the same action list twice == applying
      once. Idempotency at the row level is achieved because we treat
      ``rows`` as immutable — we never mutate, only model_copy or
      pass-through.
    * Sort order: the output preserves the input row order.
    * Pydantic ``OHLCVRow.adj_close`` is constrained ``>= 0`` — if a
      row's pre-discount adj_close is smaller than the cumulative
      dividend, the result would be negative, which is invalid. We
      raise ``ValueError`` instead of silently clipping, so a corrupt
      dividend payload (e.g. dividend larger than the bar's price)
      surfaces immediately during a dry run.
    """
    dividends = sorted(
        (a for a in actions if a.kind == "dividend"),
        key=lambda a: a.ts,
    )
    if not dividends:
        return list(rows)

    out: list[OHLCVRow] = []
    for row in rows:
        # Sum every dividend whose ex-date is strictly after the row's
        # date. We iterate the sorted list once per row — at 3000+
        # tickers x ~250 bars/year x few dividends this is trivially
        # fast (~100k ops) and avoids building an interval tree.
        cumulative_dividend = Decimal("0")
        for action in dividends:
            if action.ts <= row.ts:
                continue
            cumulative_dividend += _dividend_amount(action)

        if cumulative_dividend == Decimal("0"):
            out.append(row)
        else:
            new_adj_close = row.adj_close - cumulative_dividend
            if new_adj_close < Decimal("0"):
                raise ValueError(
                    f"dividend adjustment would produce negative adj_close: "
                    f"ticker={row.ticker} ts={row.ts} "
                    f"adj_close={row.adj_close} cumulative_dividend={cumulative_dividend}"
                )
            out.append(
                row.model_copy(
                    update={
                        "adj_close": new_adj_close,
                    }
                )
            )
    return out


def apply_adjustment(
    rows: Sequence[OHLCVRow],
    actions: Iterable[CorporateAction],
) -> list[OHLCVRow]:
    """Apply both split and dividend adjustments to OHLCV bars.

    This is the unified entry point for the orchestrator pipeline
    (Phase 2.5 step 2b → step 2c): pass in a mixed list of splits and
    dividends, get a fully adjusted bar list back.

    Composition order
    -----------------
    Splits run first, then dividends. Reasoning:

      1. Splits change the price LEVEL (a 1:2 split halves the price).
         Working in the post-split price space keeps dividend
         arithmetic consistent: dividends are quoted in the
         post-split-share currency, so adjusting adj_close for
         dividends after the split scaling is the only correct order.
      2. The two stages both treat ``rows`` as immutable, so the
         dividend stage sees the same input whether or not a prior
         stage ran. Idempotency is preserved.

    Either stage is a no-op when its action list is empty.

    Other ``kind`` values (``change`` — ticker rename) are ignored at
    every stage. The caller is responsible for splitting a renamed
    ticker into separate per-ticker histories before calling this
    function; ``CorporateAction.kind == 'change'`` is a pure metadata
    signal, not an arithmetic input.
    """
    # Materialise once: apply_split_adjustment iterates ``actions`` at
    # most once, and apply_dividend_adjustment iterates again. Calling
    # the iterator twice would yield an empty second pass.
    actions_list = list(actions)
    after_splits = apply_split_adjustment(rows, actions_list)
    return apply_dividend_adjustment(after_splits, actions_list)


__all__ = [
    "apply_adjustment",
    "apply_dividend_adjustment",
    "apply_split_adjustment",
]
