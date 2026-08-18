"""Data-freshness check — Phase 1.6 H-9 (observability).

Run from cron every 5 minutes. If the latest bar in ``ohlcv_daily``
is more than ``--stale-days`` (default 1) days behind NOW(), alert.

Why this exists
---------------
Backfill silently no-op'ing (e.g. due to auth drift) does not surface
in container logs unless the operator runs ``tail -f`` on the right
file at the right moment. The auth probe catches "writes failing"
but not "writes succeeding but the loader has nothing new to write
because the upstream feed is down". The freshness check catches
both: stuck on a stale bar means *something* went wrong, regardless
of why.

Exit codes
----------
0   data is fresh (latest bar within threshold)
1   data is STALE — daily_sync or backfill has not written in N days
2   DB unreachable — auth broken (run check_db_health.py for detail)

Phase 1.6 — paired with check_db_health.py.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from data.pg_store import PostgresDataStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stale-days",
        type=int,
        default=1,
        help="Alert if MAX(ts) is older than this many days (default: 1).",
    )
    args = parser.parse_args()

    try:
        store = PostgresDataStore()
    except Exception as exc:
        print(f"[freshness] cannot connect: {type(exc).__name__}: {exc}")
        return 2

    latest = store.latest_ts_overall()
    if latest is None:
        # Empty DB is a legitimate state (pre-launch or just after wipe).
        # Don't alert on that.
        print("[freshness] OK: ohlcv_daily is empty (legitimate pre-launch state)")
        return 0

    threshold = date.today() - timedelta(days=args.stale_days)
    stale = latest < threshold
    days_old = (date.today() - latest).days
    print(
        f"[freshness] latest bar = {latest.isoformat()} " f"({days_old} days old; threshold = {args.stale_days} day(s))"
    )
    if stale:
        print("[freshness] STALE: latest bar older than threshold")
        return 1
    print("[freshness] OK: data is fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
