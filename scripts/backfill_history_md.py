#!/usr/bin/env python3
"""Tinkoff MD archive backfill — primary full-universe daily-candle bootstrap.

Workflow
--------
1. List every instrument Tinkoff has in ``shares()`` (≈1927 TQBR + 1516
   SPBXM). Filter to those with a non-empty ``figi``.
2. For each ticker, download yearly archives from 2018 (or
   ``MIN_YEAR``) through the current year. Each archive is one ZIP of
   daily CSVs of minute bars.
3. Aggregate minute bars to daily OHLCV inside the loader.
4. ``upsert_ohlcv`` into Postgres. Idempotent on ``(ticker, ts)`` PK.

When backfill is "complete"
--------------------------
Complete = the DB has >= ``--min-bars`` (default 1300) daily bars for
every ticker in the resolved universe. Once complete the script exits
0 — the cron'd ``daily_sync.py`` takes over for incremental updates.

Recovery on restart
-------------------
The script is **idempotent and resumable** at any point: re-running it
on a partially-complete DB inserts only the missing ``(ticker, ts)``
rows. There is no checkpoint file; the DB itself is the source of
truth.

Run as (production)
-------------------

This script IS the primary backfill — runs inside the deployed stack
(``alphard-bot``) and bootstraps the OHLCV universe end-to-end.

::

    # Primary: pull everything Tinkoff exposes (no figi.txt — universe
    # comes from list_shares_all / list_bonds / list_etfs gRPC).
    # Resume-safe: re-running picks up where the DB left off.
    python scripts/backfill_history_md.py

    # Smoke run, first 50 tickers only:
    python scripts/backfill_history_md.py --limit 50

    # If you ever need a single class (rare — production uses no filter):
    python scripts/backfill_history_md.py --classes SPBXM

When the DB has >= ``--min-bars`` daily bars for every ticker in the
universe, the script exits 0. Cron then runs ``daily_sync.py`` to
keep the last few days fresh via the broker gRPC (not the archive
endpoint).

ENV
---
- ``$TINKOFF_SANDBOX_TOKEN`` or ``$TINKOFF_REAL_TOKEN`` (required)
- ``$ALPHARD_PG_DSN`` (required)
- ``$HTTP_PROXY`` (optional — Tinkoff public API is reachable directly)

Why this is primary over figi.txt
---------------------------------
``investAPI/src/marketdata/figi.txt`` ships with a hardcoded list of
~2817 FIGI captured at repo time. That list is **stale** — delisted
tickers stay in it forever and new listings only appear at the next
release. This script pulls the live universe from Tinkoff's gRPC
``InstrumentsService`` every run, so the universe is always current.
The history-data endpoint (``invest-public-api.tinkoff.ru``) and the
aggregation to daily bars are otherwise identical to the upstream
``download_md.sh`` reference script.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import threading
from datetime import date

# Make alphard.src importable when run from /app in container.
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.data.pg_store import PostgresDataStore  # noqa: E402
from src.data.tinkoff_md_loader import (  # noqa: E402
    TinkoffInvestMDDataLoader,
    aggregate_minutes_to_daily,
)
from typing import Any  # noqa: E402

logger = logging.getLogger("alphard.backfill_history_md")


class _LoaderTimeout(Exception):
    """Raised when the per-ticker deadline is exceeded. The
    ``_alarm_handler`` at module level raises this from SIGALRM so
    the main thread exits ``iter_ohlcv`` cleanly without relying on
    ctypes thread injection (which only works when the watchdog can
    capture the main thread id at function entry)."""


def _alarm_handler(signum: int, frame: object) -> None:
    """Convert SIGALRM into a clean ``_LoaderTimeout`` so the
    ``except _LoaderTimeout`` branch in ``_backfill_one`` catches it
    and the main loop records the failure cleanly."""
    raise _LoaderTimeout()


# Circuit breaker: how many consecutive ticker failures before we abort
# the whole run. Catches systematic issues (rate-limit ban, MD endpoint
# outage, parse bug) without burning hours on a doomed loop.
_CIRCUIT_BREAKER_THRESHOLD = 5

# Hard per-ticker deadline. If _backfill_one() doesn't return within this
# many seconds, the heartbeating watchdog inside it raises _LoaderTimeout
# and the run moves on to the next ticker. SBER + 9 years of minute bars
# fits in ~30s; foreign ETFs over the IB bridge can take 90s on a bad
# day. 180s gives enough headroom without letting a single stuck ticker
# starve the whole run.
_TICKER_DEADLINE_SECONDS = 180


def _resolve_universe(
    loader: TinkoffInvestMDDataLoader,
    classes: list[str] | None,
    limit: int,
) -> list[str]:
    """Resolve universe -> list of tickers in stable order.

    ``classes`` filters by ``class_code`` (``TQBR`` / ``SPBXM`` / ...).
    Empty ``classes`` = all classes. ``limit`` caps the universe size
    for smoke runs.
    """
    metas = loader.list_tickers_with_figi()
    if classes:
        classes_upper = {c.upper() for c in classes}
        metas = [m for m in metas if (m.class_code or "").upper() in classes_upper]
    if limit > 0:
        metas = metas[:limit]
    tickers = [m.ticker for m in metas]
    logger.info(f"Universe: {len(tickers)} tickers (classes={classes or 'ALL'}, limit={limit})")
    return tickers


def _is_complete(
    store: PostgresDataStore,
    ticker: str,
    min_bars: int,
) -> bool:
    """Backfill complete if ticker has >= ``min_bars`` daily bars already."""
    return store.count_ohlcv(ticker=ticker) >= min_bars


def _backfill_one(
    loader: TinkoffInvestMDDataLoader,
    store: PostgresDataStore,
    ticker: str,
    start: date,
    end: date,
    ticker_idx: int = 0,
    total: int = 0,
) -> dict[str, int]:
    """Download + aggregate - upsert one ticker. Returns stats.

    Two deadline mechanisms work together to bound per-ticker work:

    1. ``signal.alarm(_TICKER_DEADLINE_SECONDS)`` schedules a SIGALRM
       on the main thread (POSIX). The handler raises ``_LoaderTimeout``
       which propagates up to the ``except _LoaderTimeout`` clause.
       Caveat: SIGALRM only fires at Python bytecode boundaries, so
       it does NOT interrupt long-running C code such as
       ``zipfile.ZipFile.read()`` or ``pandas`` resample. The alarm
       fires as soon as control returns to Python.
    2. A background watchdog thread fires ``PyThreadState_SetAsyncExc``
       against the main thread id captured at function entry. This
       works only when the main thread is in Python code; same
       C-extension caveat applies.

    For tickers whose zip is unusually large (multi-100MB minute
    archives for SPBXM ETFs), neither mechanism can interrupt the
    inflate. The watchdog thread still logs the deadline exceedance
    and the parent process gets a clear "deadline exceeded" line in
    the log so the operator can intervene manually if needed.

    A heartbeat line is emitted every 30s inside ``iter_ohlcv`` so
    the operator can see where the time is going when the ticker
    is in pure Python code (HTTP read, generator yield).
    """
    deadline = time.monotonic() + _TICKER_DEADLINE_SECONDS
    timed_out = threading.Event()
    # Capture the main thread id at function entry — PyThreadState_SetAsyncExc
    # takes a target thread id, and the watchdog thread's own id is NOT the
    # main thread. If we captured in the watchdog closure, we'd inject an
    # exception into the watchdog itself (which would be cleared by
    # timed_out.set() and produce silent failure).
    main_thread_id = threading.get_ident()

    # Belt-and-suspenders: signal.alarm runs in the main thread on
    # POSIX, fires SIGALRM after ``_TICKER_DEADLINE_SECONDS`` and is
    # caught by _alarm_handler. The ctypes thread injection below is
    # the primary mechanism; this is the safety net.
    _sigalrm_supported = hasattr(signal, "SIGALRM")
    _prev_alarm = signal.alarm(0) if _sigalrm_supported else 0
    if _sigalrm_supported:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(_TICKER_DEADLINE_SECONDS)

    def _deadline_watchdog() -> None:
        while not timed_out.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    f"[{ticker_idx}/{total}] {ticker}: deadline " f"{_TICKER_DEADLINE_SECONDS}s exceeded, aborting"
                )
                # Inject exception into the MAIN thread via ctypes.
                # CPython GIL + PyThreadState_SetAsyncExc is the standard
                # pattern for thread-safe timeout enforcement.
                import ctypes

                code = ctypes.py_object(_LoaderTimeout)
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(main_thread_id), code)
                timed_out.set()
                return
            time.sleep(min(5.0, max(1.0, remaining / 4)))

    watchdog = threading.Thread(target=_deadline_watchdog, daemon=True, name=f"wd-{ticker}")
    watchdog.start()
    ticker_started = time.monotonic()
    last_heartbeat = ticker_started
    try:
        from src.data.models import OHLCVRow as _OHLCV

        rows: list[Any] = []
        for b in loader.iter_ohlcv(ticker, start, end):
            now = time.monotonic()
            if now - last_heartbeat >= 30.0:
                logger.info(f"[{ticker_idx}/{total}] {ticker} STREAM elapsed={now - ticker_started:.0f}s")
                last_heartbeat = now
            rows.append(b)
        if not rows:
            return {"fetched": 0, "written": 0}
        if last_heartbeat == ticker_started:
            logger.info(
                f"[{ticker_idx}/{total}] {ticker} fetched={len(rows)} in {time.monotonic() - ticker_started:.0f}s"
            )
        written = store.upsert_ohlcv(rows)
        return {"fetched": len(rows), "written": written}
    except _LoaderTimeout:
        return {"fetched": 0, "written": -1}
    except Exception as exc:
        logger.error(f"{ticker}: {exc}")
        return {"fetched": 0, "written": -1}
    finally:
        timed_out.set()  # tell watchdog to exit
        if _sigalrm_supported:
            signal.alarm(0)  # cancel any pending SIGALRM
            if _prev_alarm:
                signal.alarm(_prev_alarm)  # restore prior alarm


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--start-year",
        type=int,
        default=2018,
        help="Earliest year to backfill (default 2018). Below 2018 returns 404.",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=date.today().year,
        help="Latest year to backfill (default = current year).",
    )
    parser.add_argument("--classes", nargs="*", default=None, help="Filter universe by class_code (e.g. SPBXM TQBR).")
    parser.add_argument("--limit", type=int, default=0, help="Cap universe size for smoke runs (0 = no cap).")
    parser.add_argument(
        "--min-bars", type=int, default=1300, help="Min daily bars per ticker to consider backfill complete."
    )
    parser.add_argument("--dsn", default=os.environ.get("ALPHARD_PG_DSN"))
    parser.add_argument("--token", default=None, help="Override $TINKOFF_SANDBOX_TOKEN/$TINKOFF_REAL_TOKEN.")
    parser.add_argument("--batch-sleep", type=float, default=0.0, help="Sleep between tickers (rate-limit cushion).")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if ticker already has min-bars.")
    args = parser.parse_args()

    if not args.dsn:
        logger.error("ALPHARD_PG_DSN not set")
        return 1
    os.environ["ALPHARD_PG_DSN"] = args.dsn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    start = date(args.start_year, 1, 1)
    end = date(args.end_year, 12, 31)

    try:
        loader = TinkoffInvestMDDataLoader(token=args.token)
    except Exception as e:
        logger.error(f"Loader init failed: {e}")
        return 2

    store = PostgresDataStore()
    started = time.monotonic()
    total_fetched = 0
    total_written = 0
    skipped_complete = 0
    errors: list[tuple[str, str]] = []

    try:
        tickers = _resolve_universe(loader, args.classes, args.limit)
        logger.info(f"=== Tinkoff MD backfill: {len(tickers)} tickers, {start} → {end} ===")

        circuit_breaker_streak = 0
        for i, ticker in enumerate(tickers, start=1):
            if not args.force and _is_complete(store, ticker, args.min_bars):
                logger.info(f"{i}/{len(tickers)} {ticker}: skip (complete)")
                skipped_complete += 1
                circuit_breaker_streak = 0
                continue

            stats = _backfill_one(
                loader,
                store,
                ticker,
                start,
                end,
                ticker_idx=i,
                total=len(tickers),
            )
            total_fetched += max(stats["fetched"], 0)
            total_written += max(stats["written"], 0)
            if stats["written"] < 0:
                errors.append((ticker, "error"))
                circuit_breaker_streak += 1
            else:
                circuit_breaker_streak = 0

            if stats["fetched"]:
                logger.info(f"{i}/{len(tickers)} {ticker}: " f"fetched={stats['fetched']} written={stats['written']}")
            else:
                logger.info(f"{i}/{len(tickers)} {ticker}: no data in window")

            if args.batch_sleep > 0:
                time.sleep(args.batch_sleep)

            # Circuit breaker: if N consecutive tickers fail, abort the
            # run. This catches systematic issues (rate-limit ban, MD
            # endpoint outage, parse bug) without burning hours on a
            # doomed loop.
            if circuit_breaker_streak >= _CIRCUIT_BREAKER_THRESHOLD:
                logger.error(
                    f"Circuit breaker tripped: {circuit_breaker_streak} "
                    f"consecutive ticker failures. Aborting run. "
                    f"Investigate upstream MD endpoint."
                )
                return 3
    finally:
        store.close()

    elapsed = time.monotonic() - started
    logger.info(
        f"=== DONE in {elapsed:.0f}s: fetched={total_fetched} "
        f"written={total_written} skipped={skipped_complete} errors={len(errors)} ==="
    )
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
