"""One-shot: restore SPBXM universe (1516 US tickers) via Tinkoff + full OHLCV backfill.

After manually deleting SPBXM (foreign) tickers from ticker_universe, this
script re-inserts them via Tinkoff gRPC and backfills OHLCV for ALL of them.

The full set is needed for ML training (anonymized ticker, no class_code
leakage features — but the *universe* itself must be diverse to provide
varied regime/volatility training signal).

Default behaviour (no flags):
  * Pulls ALL SPBXM-class tickers from Tinkoff (~1516)
  * Inserts them all into ticker_universe
  * Backfills 5y of OHLCV for each (skip if already covered)

Estimated runtime: ~75-90 min (100 req/min × 5 chunks/ticker × 1516 tickers).

Idempotent: ON CONFLICT (ticker) DO UPDATE for ticker_universe; pg_store.upsert_ohlcv
preserves first-source-wins but allows fresh dates.

Run:
    python scripts/backfill_spbxm_universe.py
    python scripts/backfill_spbxm_universe.py --no-backfill  # universe only
    python scripts/backfill_spbxm_universe.py --days=365    # 1y instead of 5y
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from urllib.parse import quote

# Ensure /app is on sys.path so src.* imports work inside the container
sys.path.insert(0, "/app")

# Ensure DSN is set for PostgresDataStore (token-gate).
# BUGFIX (H-9): URL-escape the password (and user) so special characters
# like '@', ':', '/', '?' don't break the DSN. Without this, a password
# containing '@' would silently route the connection to the wrong host.
if not os.environ.get("ALPHARD_PG_DSN"):
    pg_user = quote(os.environ.get("POSTGRES_USER", "alphard"), safe="")
    pg_pwd = quote(os.environ.get("POSTGRES_PASSWORD", ""), safe="")
    pg_host = os.environ.get("POSTGRES_HOST", "alphard-postgres")
    pg_db = os.environ.get("POSTGRES_DB", "alphard")
    os.environ["ALPHARD_PG_DSN"] = f"postgresql://{pg_user}:{pg_pwd}@{pg_host}:5432/{pg_db}"

from src.data.pg_store import PostgresDataStore  # noqa: E402
from src.data.tinkoff_loader import TinkoffInvestDataLoader  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1825, help="OHLCV lookback days (default 1825 = 5y)")
    parser.add_argument(
        "--batch",
        type=int,
        default=20,
        help="Tinkoff rate-log print every N tickers (default 20)",
    )
    parser.add_argument(
        "--no-backfill",
        action="store_true",
        help="Insert universe rows only, skip OHLCV backfill",
    )
    parser.add_argument(
        "--classes",
        type=str,
        default="SPBXM",
        help="Comma-separated Tinkoff class_codes to backfill (default: SPBXM). "
        "Other classes: TQBR, TQOB, TQCB, TQTE.",
    )
    args = parser.parse_args()

    loader = TinkoffInvestDataLoader()
    store = PostgresDataStore()

    total_universe = 0
    total_inserted = 0
    total_backed = 0
    total_covered = 0

    for class_code in [c.strip() for c in args.classes.split(",") if c.strip()]:
        print(f"\n=== {class_code} ===")
        print(f"Pulling {class_code} universe from Tinkoff gRPC...")
        try:
            ticker_metas = loader.list_shares_all(class_code=class_code)
        except Exception as exc:
            print(f"  list_shares_all({class_code}) failed: {exc}")
            continue
        print(f"Found {len(ticker_metas)} tickers in {class_code}")
        total_universe += len(ticker_metas)

        # 1. Insert into ticker_universe (idempotent ON CONFLICT)
        inserted = 0
        skipped = 0
        for meta in ticker_metas:
            try:
                store.upsert_ticker(meta)
                inserted += 1
            except Exception as exc:
                print(f"  upsert {meta.ticker}: {exc}")
                skipped += 1
        total_inserted += inserted
        print(f"Inserted {inserted} / skipped {skipped}")

        if args.no_backfill:
            continue

        # 2. Backfill OHLCV for every ticker (skip if already covered)
        end = date.today()
        start = end - timedelta(days=args.days)
        print(f"Backfilling OHLCV for {start} → {end} ({args.days} days)")
        backed = 0
        covered = 0
        for i, meta in enumerate(ticker_metas):
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
                print(f"  [{i + 1}/{len(ticker_metas)}] " f"backed={backed} covered={covered} " f"(class={class_code})")
        total_backed += backed
        total_covered += covered
        print(f"Class {class_code} done. backed={backed} bars, covered={covered} tickers")

    print(
        f"\n=== Summary ===\n"
        f"universe: {total_universe} tickers processed\n"
        f"inserted: {total_inserted}\n"
        f"backed:   {total_backed} bars\n"
        f"covered:  {total_covered} tickers (already had OHLCV)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
