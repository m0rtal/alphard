#!/usr/bin/env python3
"""Macro sync: pull CBR + USD/RUB + IMOEX, classify regime, upsert to Postgres.

Phase 2.3 Macro Agent. Idempotent: re-running within the cache TTL window
is a no-op for the network call but always re-classifies and upserts.

The script mirrors ``daily_sync.py`` and ``apply_corporate_actions.py``:
- argparse for CLI ergonomics (so the daemon can pass flags if needed);
- ``--dsn`` (or ``$ALPHARD_PG_DSN``) for the Postgres connection;
- ``--state-dir`` for the fetcher cache (defaults to /var/lib/alphard/macro
  in the container, $TMPDIR elsewhere);
- ``--force`` to skip the "skip if last fetch < 1h" gate.

Side effects on success:
    * INSERT/UPDATE one row in ``macro_regime_log`` (keyed by fetched_at).
    * Write per-fetcher caches in ``<state_dir>/macro/{cbr,usdrub,imoex}.json``
      via .tmp+rename (atomic).

Exit codes:
    0  snapshot built + regime classified + row upserted
    1  any fetcher failed AND cache was empty (network outage on first run)
    2  classifier raised (defensive — should never happen for a valid snapshot)
    3  Postgres upsert failed (connection issue, etc.)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make alphard.src importable when run from /app (matches other scripts).
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.data.macro_fetcher import build_snapshot  # noqa: E402
from src.data.pg_store import PostgresDataStore  # noqa: E402
from src.macro import persistence as macro_persistence  # noqa: E402
from src.macro.models import MacroRegime  # noqa: E402
from src.macro.regime import classify  # noqa: E402

logger = logging.getLogger("alphard.macro_sync")

# Skip gate: re-running the macro sync within this window is a no-op.
DEFAULT_SKIP_WINDOW_SECONDS = 3600  # 1 hour

# Where the fetcher keeps its per-source JSON caches.
DEFAULT_STATE_DIR = Path(os.environ.get("ALPHARD_STATE_DIR", "/var/lib/alphard"))


def _latest_in_db(store: PostgresDataStore) -> Optional[MacroRegime]:
    """Return the latest ``MacroRegime`` row from ``macro_regime_log``,
    or None if the table is empty."""
    try:
        return macro_persistence.latest_regime(store._conn)
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        logger.warning(f"latest_regime() lookup failed: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Macro sync: fetch + classify + upsert.")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("ALPHARD_PG_DSN"),
        help="Postgres DSN (falls back to $ALPHARD_PG_DSN)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Where to keep per-fetcher caches (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the 'last fetch < 1h' no-op gate.",
    )
    parser.add_argument(
        "--skip-window-seconds",
        type=int,
        default=DEFAULT_SKIP_WINDOW_SECONDS,
        help=f"Skip if the most-recent row is fresher than N seconds (default {DEFAULT_SKIP_WINDOW_SECONDS}).",
    )
    args = parser.parse_args()

    if not args.dsn:
        logger.error("ALPHARD_PG_DSN not set; pass --dsn or export the env var")
        return 1

    os.environ["ALPHARD_PG_DSN"] = args.dsn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    store = PostgresDataStore()
    try:
        # Issue #164: PostgresDataStore uses LAZY connection
        # (``self._conn = None`` at __init__, ``_connect()`` opens it).
        # The previous code accessed ``store._conn`` at two sites below
        # WITHOUT ever calling ``store._connect()`` first, so on a fresh
        # store ``store._conn is None`` and ``_is_postgres(None)`` returns
        # True (duck-types as psycopg2). The persistence helpers then
        # called ``None.cursor()`` and raised AttributeError. Net effect:
        # the skip-gate was silently disabled (every run re-fetched) and
        # the upsert always failed with rc=3, so macro_regime_log was
        # never written.
        #
        # Force a single explicit connect here, mirroring the pattern in
        # scripts/validate_ohlcv.py:89. After this line ``store._conn``
        # is a live psycopg2 connection (or a StoreError has been raised
        # by ``_connect()``, which we propagate as rc=3 below).
        try:
            store._connect()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"macro_sync store connect failed: {exc}")
            return 3

        # Skip gate: if the most-recent row is younger than the window, exit 0.
        if not args.force:
            latest = _latest_in_db(store)
            if latest is not None and latest.snapshot is not None:
                age = (datetime.now(tz=timezone.utc) - latest.snapshot.fetched_at).total_seconds()
                if age < args.skip_window_seconds:
                    logger.info(f"macro_sync skipped: latest fetch {age:.0f}s ago < window {args.skip_window_seconds}s")
                    return 0

        # Build the snapshot. Returns None on total failure.
        snapshot = build_snapshot(state_dir=args.state_dir)
        if snapshot is None:
            logger.error("macro_sync failed: snapshot builder returned None")
            return 1

        # Classify (pure).
        try:
            regime = classify(snapshot)
        except ValueError as exc:
            logger.error(f"macro_sync classifier raised: {exc}")
            return 2

        logger.info(f"macro_sync regime={regime.regime} multiplier={regime.multiplier} " f"reason={regime.reason!r}")

        # Persist.
        try:
            macro_persistence.upsert_regime(store._conn, regime)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"macro_sync upsert failed: {exc}")
            return 3

        logger.info("macro_sync OK: row upserted into macro_regime_log")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
