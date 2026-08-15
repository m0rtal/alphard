#!/usr/bin/env python3
"""Daily sync: pull last N days for top MOEX tickers into Postgres.

Tinkoff Invest gRPC is the PRIMARY source (broker-authoritative).
MOEX ISS REST is reserved for backfill pre-2010 (when Tinkoff API
may not have data) and as a cross-source validation fallback.

Used by cron. Idempotent: upsert on (ticker, ts, source) PK.

Universe (Phase 1.1): top 20 liquid MOEX TQBR shares — bootstrap for
the cross-sectional ML pipeline. Phase 3 will expand to full MOEX.
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

from src.data.tinkoff_loader import TinkoffInvestDataLoader
from src.data.pg_store import PostgresDataStore

logger = logging.getLogger("alphard.daily_sync")

# Top 20 MOEX TQBR by ADV (June 2026 estimate). Phase 1.1 bootstrap.
LIQUID_UNIVERSE = [
    "SBER", "GAZP", "LKOH", "GMKN", "NVTK",
    "ROSN", "TATN", "MGNT", "MOEX", "ALRS",
    "MTSS", "SNGS", "NLMK", "CHMF", "YDEX",
    "OZON", "VKCO", "SBERP", "BANE", "BSPB",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5,
                        help="Pull last N days (default 5, includes weekends)")
    parser.add_argument("--backfill", type=int, default=0,
                        help="If > 0, pull N days for the full universe (one-time backfill)")
    parser.add_argument("--universe", nargs="*", default=None,
                        help="Ticker list (default: top 20 MOEX liquid)")
    parser.add_argument("--dsn", default=os.environ.get("ALPHARD_PG_DSN"),
                        help="Postgres DSN (falls back to $ALPHARD_PG_DSN)")
    parser.add_argument("--source", default="tkf",
                        choices=["tkf", "moex"],
                        help="Primary source: tkf (Tinkoff, default) or moex (MOEX ISS)")
    args = parser.parse_args()

    if not args.dsn:
        logger.error("ALPHARD_PG_DSN not set")
        return 1

    os.environ["ALPHARD_PG_DSN"] = args.dsn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if args.backfill > 0:
        end = date.today()
        start = end - timedelta(days=args.backfill)
        symbols = args.universe or LIQUID_UNIVERSE
    else:
        end = date.today()
        start = end - timedelta(days=args.days)
        symbols = args.universe or LIQUID_UNIVERSE

    logger.info(f"=== Sync: {args.source} {start} → {end} ({len(symbols)} tickers) ===")

    # Loader selection
    if args.source == "tkf":
        try:
            loader = TinkoffInvestDataLoader()
        except Exception as e:
            logger.error(f"Failed to init Tinkoff loader: {e}")
            return 2
    else:
        from src.data.moex_loader import MOEXDataLoader
        loader = MOEXDataLoader()

    store = PostgresDataStore()

    # Resolve TickerMeta once
    try:
        meta_cache = {t.ticker: t for t in loader.list_tickers()}
    except Exception as e:
        logger.error(f"Failed to list tickers: {e}")
        store.close()
        return 2

    total_bars = 0
    errors = []

    try:
        for symbol in symbols:
            meta = meta_cache.get(symbol.upper())
            if meta is None:
                logger.warning(f"{symbol}: not in universe, skipping")
                continue

            try:
                store.upsert_ticker(meta)
                if args.source == "tkf":
                    bars = loader.fetch_ohlcv(symbol, start, end)
                else:
                    bars = list(loader.iter_ohlcv(symbol, start, end))  # type: ignore[attr-defined]

                if bars:
                    written = store.upsert_ohlcv(bars)
                    logger.info(f"{symbol}: {written} bars")
                    total_bars += written
            except Exception as exc:
                logger.error(f"{symbol}: {exc}")
                errors.append((symbol, str(exc)))
    finally:
        store.close()

    logger.info(f"=== DONE: {total_bars} bars written, {len(errors)} errors ===")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
