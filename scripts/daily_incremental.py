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
is ``tinkoff_grpc, moex_iss``.

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
    """Fetch bars for [start, end] from broker gRPC, fall back to MOEX.

    Mirrors the chain in FallbackDataLoader but inlined here so the
    script remains self-contained (matches daily_sync.py's pattern
    of constructing loaders directly rather than going through
    FallbackDataLoader). The shared chain is enforced by tests.

    MOEX loader is constructed only on the fallback path, so a
    successful broker fetch does not pay the MOEX import /
    connection cost.
    """
    try:
        return TinkoffInvestDataLoader().fetch_ohlcv(ticker, start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{ticker}: broker gRPC failed ({type(exc).__name__}: {exc}); " f"falling back to MOEX ISS")
    return list(MOEXDataLoader().iter_ohlcv(ticker, start, end))


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
        # Get full list of complete tickers from ticker_universe.
        # Uses the same query shape as supervisor's _universe_metrics_loop.
        all_meta = store.list_complete_universe()  # placeholder, will assert in tests
    except AttributeError:
        # Fallback path: store doesn't yet expose list_complete_universe.
        # We rebuild from existing public methods. The supervisor in
        # src/main.py uses store.upsert_tickers + SELECT; we do the
        # same here. To keep this script self-contained for now, we
        # delegate to backfill_history_md's _resolve_universe
        # helper if available.
        logger.error(
            "PostgresDataStore.list_complete_universe() not implemented; "
            "this script depends on issue #331 follow-up work"
        )
        return 2
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
