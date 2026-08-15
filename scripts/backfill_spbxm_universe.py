"""One-shot: restore SPBXM universe (1516 US tickers) via Tinkoff + backfill OHLCV.

After manually deleting SPBXM (foreign) tickers from ticker_universe, this
script re-inserts them via Tinkoff gRPC and optionally backfills OHLCV.

The full set is needed for ML training (anonymized ticker, no class_code
leakage features — but the *universe* itself must be diverse to provide
varied regime/volatility training signal).

Run once:
    python scripts/backfill_spbxm_universe.py [--days=1825] [--batch=20]

Backfill 1516 tickers × 5y takes ~21h at 100 req/min (1 chunk per ticker).
Add to cron afterwards if you want incremental daily updates.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from src.data.tinkoff_loader import TinkoffInvestDataLoader
from src.data.pg_store import PostgresDataStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1825, help="OHLCV lookback days (default 1825 = 5y)")
    parser.add_argument("--batch", type=int, default=20, help="Tinkoff rate limit pause every N tickers")
    parser.add_argument("--no-backfill", action="store_true", help="Insert universe rows only, skip OHLCV")
    args = parser.parse_args()

    loader = TinkoffInvestDataLoader()
    store = PostgresDataStore()

    # 1. Pull SPBXM-class tickers from Tinkoff
    print("Pulling SPBXM universe from Tinkoff gRPC...")
    spbxm = loader.list_shares_all(class_code="SPBXM")
    print(f"Found {len(spbxm)} SPBXM-class tickers")

    inserted = 0
    skipped = 0
    for meta in spbxm:
        try:
            store.upsert_ticker(meta)
            inserted += 1
        except Exception as exc:
            print(f"  upsert {meta.ticker}: {exc}")
            skipped += 1
    print(f"Inserted {inserted} / skipped {skipped}")

    if args.no_backfill:
        return 0

    # 2. Backfill OHLCV for each (skip if already covered)
    end = date.today()
    start = end - timedelta(days=args.days)
    print(f"Backfilling OHLCV for {start} → {end}")
    backed = 0
    covered = 0
    for i, meta in enumerate(spbxm):
        try:
            existing = store.count_ohlcv(meta.ticker)
            if existing > 0:
                covered += 1
                continue
            rows = loader.fetch_ohlcv(meta.ticker, start, end)
            if rows:
                store.upsert_ohlcv(rows)
                backed += len(rows)
        except Exception as exc:
            print(f"  {meta.ticker}: {exc}")
        if (i + 1) % args.batch == 0:
            print(f"  [{i + 1}/{len(spbxm)}] backed={backed} covered={covered}")
    print(f"Done. backed={backed} bars, covered={covered} tickers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
