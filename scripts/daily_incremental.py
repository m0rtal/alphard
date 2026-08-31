#!/usr/bin/env python3
"""Daily incremental refresh: closed bars for complete tickers.

Contract (2026-08-29, issue #331, m0rtal):
    For each ticker with ``backfill_complete = TRUE`` (i.e. its full
    history pull succeeded at least once), pull the delta from
    ``latest_db_ts + 1`` through ``today - 1`` and upsert. We never
    insert today's bar (it is still forming during the session) and
    we never re-fetch history that is already in the DB.

Differences from ``scripts/daily_sync.py``:
    - daily_sync.py operates on a curated ``LIQUID_UNIVERSE`` of 20
      MOEX blue-chips with ``--days`` lookback. It is the cross-
      sectional ML feature pipeline; it does NOT track per-ticker
      ``latest_ts`` and it can drop incomplete bars for tickers not
      on the top-20 list.
    - daily_incremental.py is the post-backfill maintenance
      refresher: it covers the full universe of complete tickers
      (3265 as of 2026-08-29) and only inserts the closed-bar
      delta so we do not churn the row for today's still-forming
      bar.

Source priority: broker gRPC (Tinkoff) first, MOEX ISS fallback.
Same chain as ``FallbackDataLoader`` (issue #331): the chain order
is ``tinkoff_grpc, moex_iss``. The fetch helper routes through
``FallbackDataLoader.iter_ohlcv`` so it automatically inherits
the per-source lookback-aware chunking from PR #348 (closes #350).
A successful broker fetch never touches the MOEX loader.

Used by cron once per day after market close. Idempotent:
``upsert_ohlcv`` is upsert on (ticker, ts) PK.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta

# Make alphard.src importable when run from /app
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.data.pg_store import PostgresDataStore  # noqa: E402
from src.data.tinkoff_loader import TinkoffInvestDataLoader  # noqa: E402
from src.data.moex_loader import MOEXDataLoader  # noqa: E402
from src.data.fallback_loader import FallbackDataLoader  # noqa: E402

logger = logging.getLogger("alphard.daily_incremental")


def _closed_bar_window(latest_db_ts: date | None) -> tuple[date, date]:
    """Return (start, end) of the closed-bar window to fetch.

    ``end = today - 1`` (yesterday). The current day's bar is still
    forming during the live session so we never insert ``ts = today``.

    If ``latest_db_ts is None`` (ticker has no bars yet — should be
    impossible if backfill_complete=True, but defensive), we fall
    back to ``today - 1`` only and let the supervisor catch the
    anomaly via _is_complete.

    Otherwise: ``start = latest_db_ts + 1`` so we only fetch the
    strictly-new delta. If ``start > end`` (today is the only
    missing day and it is also today, which we skip) we return
    ``start > end`` and the caller skips the ticker.
    """
    end = date.today() - timedelta(days=1)
    if latest_db_ts is None:
        return end, end
    start = latest_db_ts + timedelta(days=1)
    return start, end


def _fetch_with_fallback(ticker: str, start: date, end: date) -> list:
    """Fetch bars for [start, end] via the broker-first fallback chain.

    Routes through ``FallbackDataLoader.iter_ohlcv`` so the chain's
    per-source lookback-aware chunking (PR #348) applies. Before
    issue #350 was fixed, this helper inlined the chain with a direct
    ``MOEXDataLoader().iter_ohlcv(ticker, start, end)`` call on the
    fallback path — MOEX enforces a 1825-day cap and any longer
    window raised ``LoaderError: range ... exceeds upstream max
    lookback 1825d``, silently losing the incremental update for
    delisted tickers (or any ticker with stale ``latest_db_ts``) on
    days when broker gRPC happened to fail.

    Broker construction is wrapped in ``try/except``: when no Tinkoff
    token is set (``TinkoffInvestDataLoader.__init__`` raises
    ``LoaderAuthError`` in the documented ``ALLOW_NO_BROKER=true``
    Phase 0 stub mode and in out-of-container dev runs without
    ``.env``), the broker is passed as ``None`` to the chain. The
    chain's ``_resolve`` already skips ``None`` sources, so the
    request silently degrades to MOEX. Issue #354 regression guard.
    """
    broker: object | None = None
    try:
        broker = TinkoffInvestDataLoader()
    except Exception as exc:  # noqa: BLE001 — LoaderAuthError or any constructor-time failure
        logger.warning(f"{ticker}: broker gRPC unavailable ({type(exc).__name__}: {exc}); " "falling back to MOEX ISS")
    fl = FallbackDataLoader(
        tinkoff_grpc=broker,
        moex_iss=MOEXDataLoader(),
    )
    return list(fl.iter_ohlcv(ticker, start, end))


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily incremental refresh of closed OHLCV bars.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and log the per-ticker window without fetching or writing.",
    )
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=0,
        help="Limit number of tickers (0 = all complete tickers).",
    )
    parser.add_argument(
        "--max-bars-per-ticker",
        type=int,
        default=0,
        help="Safety cap: skip the fetch if the would-be window exceeds N days (0 = no cap).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("ALPHARD_PG_DSN")
    if not dsn:
        logger.error("ALPHARD_PG_DSN not set")
        return 1
    os.environ["ALPHARD_PG_DSN"] = dsn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    store = PostgresDataStore()
    try:
        # Full list of complete tickers from ticker_universe. Same query
        # shape as the supervisor's _universe_metrics_loop.
        all_meta = store.list_complete_universe()
    finally:
        store.close()

    if args.max_tickers > 0:
        all_meta = all_meta[: args.max_tickers]

    today = date.today()
    end = today - timedelta(days=1)
    logger.info(f"=== Daily incremental refresh: end={end} (yesterday), " f"{len(all_meta)} complete tickers ===")

    total_inserted = 0
    total_skipped = 0
    errors = []
    store = PostgresDataStore()
    try:
        for i, meta in enumerate(all_meta, start=1):
            ticker = meta.ticker
            latest = store.latest_ts(ticker)
            start, end = _closed_bar_window(latest)

            if start > end:
                logger.debug(f"{i}/{len(all_meta)} {ticker}: no new closed bar (start={start} > end={end})")
                total_skipped += 1
                continue

            window_days = (end - start).days + 1
            if args.max_bars_per_ticker and window_days > args.max_bars_per_ticker:
                logger.warning(f"{i}/{len(all_meta)} {ticker}: skipping, window {window_days}d > cap")
                total_skipped += 1
                continue

            if args.dry_run:
                logger.info(
                    f"{i}/{len(all_meta)} {ticker}: would fetch {start}..{end} "
                    f"({window_days}d, latest_in_db={latest})"
                )
                continue

            try:
                bars = _fetch_with_fallback(ticker, start, end)
                if not bars:
                    logger.debug(f"{i}/{len(all_meta)} {ticker}: no bars in {start}..{end}")
                    total_skipped += 1
                    continue
                # Final safety filter: never insert today or future
                # bars. Tinkoff sometimes returns the current forming
                # bar depending on server-side state, so we filter
                # defensively here even though we asked for end=today-1.
                bars = [b for b in bars if b.ts <= end]
                if not bars:
                    total_skipped += 1
                    continue
                written = store.upsert_ohlcv(bars)
                logger.info(
                    f"{i}/{len(all_meta)} {ticker}: +{written} bars " f"({start}..{end}, latest_in_db_was={latest})"
                )
                total_inserted += written
            except Exception as exc:  # noqa: BLE001
                logger.error(f"{i}/{len(all_meta)} {ticker}: {type(exc).__name__}: {exc}")
                errors.append((ticker, str(exc)))
    finally:
        store.close()

    logger.info(
        f"=== DONE: +{total_inserted} bars inserted, {total_skipped} skipped, " f"{len(errors)} errors, end={end} ==="
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
