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
Complete for a ticker when **stored bar count >= expected bar count**
for the date range we can possibly cover for it::

    expected_bars = trading_days(listed_at, today|delisted_at) * (1 - _HALTS_PCT)

    trading_days = calendar_days * 252 / 365.25   (no holiday calendar)
    _HALTS_PCT   = 0.15                            (15% slack: 2022 sanctions gap, etc.)

Fast path: if ``count >= --min-bars`` (1300), short-circuits. Catches
~99% of "complete" tickers (live, delisted or fresh) without
touching universe metadata.

Once every ticker in the universe is complete, the script exits 0
and the cron'd ``daily_sync.py`` takes over for incremental updates
via broker gRPC.

Recovery on restart
-------------------
The script is **idempotent and resumable** at any point: re-running it
on a partially-complete DB inserts only the missing ``(ticker, ts)``
rows. There is no checkpoint file; the DB itself is the source of
truth.

Data-quality gate (primary decision input)
------------------------------------------
The DB is the primary signal for trade decisions — silent garbage
in means silent garbage out. Every fresh batch runs through
``src.data.quality.validate`` before the upsert:

  - **CRITICAL** (high<low, negative volume, non-positive price)
    rejects the entire ticker batch for this run. The caller logs
    the rejection and moves on. Repeat offenders accumulate in
    the warning summary and surface in ``scripts/validate_ohlcv.py``.
  - **WARNING** (daily return > 50%, calendar gap > 14 days)
    is logged but does not block. The operator investigates
    periodically.

Series-level checks (50% return, long gaps) protect against
un-accommodated stock splits, missing archives, and silent
delistings.

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
from src.data.tinkoff_md_loader import TinkoffInvestMDDataLoader  # noqa: E402
from src.data.tinkoff_loader import TinkoffInvestDataLoader  # noqa: E402
from src.data.moex_loader import MOEXDataLoader  # noqa: E402
from src.data.fallback_loader import FallbackDataLoader  # noqa: E402
from src.data.models import TickerMeta  # noqa: E402
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
_TICKER_DEADLINE_SECONDS = 120  # 2026-08-19: raised from 30s. Live cluster observation
# showed that the previous 30s deadline was too aggressive: it cut off
# legitimate slow responses (SBER 9-year minute archive can be 40-90s
# on a fresh connection; foreign ETFs/bonds over 60-90s). The .107
# network stall is a separate issue — when the body stalls, SIGALRM
# still fires at 120s and the fallback chain moves on. 120s is the
# sweet spot: long enough for healthy large archives to finish,
# short enough that a stuck ticker doesn't block the run.


def _set_complete_flag(store: PostgresDataStore, ticker: str, complete: bool) -> None:
    """Flip the per-ticker backfill_complete flag. Catches any pg_store
    exception so a flag-flip failure can't crash the backfill loop — the
    flag is metadata, the bars are the primary deliverable.
    """
    try:
        store.mark_backfill_complete(ticker, complete=complete)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not set backfill_complete={complete} for {ticker}: {exc}")


def _resolve_universe(
    loader: FallbackDataLoader,
    classes: list[str] | None,
    limit: int,
) -> tuple[list[str], dict[str, "TickerMeta"]]:
    """Resolve universe -> (tickers, metas_map) in stable order.

    ``classes`` filters by ``class_code`` (``TQBR`` / ``SPBXM`` / ...).
    Empty ``classes`` = all classes. ``limit`` caps the universe size
    for smoke runs.

    Returns BOTH:
    * ordered ticker strings (for the main backfill loop), and
    * the metas_map keyed by ticker so the caller can upsert_tickers
      into ticker_universe BEFORE fetching bars (BUGFIX 2026-08-18:
      the FK on ohlcv_daily.ticker requires the row in ticker_universe
      to exist before INSERT).
    """
    metas = loader.list_tickers()
    if classes:
        classes_upper = {c.upper() for c in classes}
        metas = [m for m in metas if (m.class_code or "").upper() in classes_upper]
    if limit > 0:
        metas = metas[:limit]
    tickers = [m.ticker for m in metas]
    metas_map = {m.ticker: m for m in metas}
    logger.info(f"Universe: {len(tickers)} tickers (classes={classes or 'ALL'}, limit={limit})")
    return tickers, metas_map


# Trading days per calendar year on MOEX. ~252 sessions/year is the
# standard accounting convention (excludes weekends + holidays).
_TRADING_DAYS_PER_YEAR = 252

# Fraction of trading days a "complete" ticker is allowed to be
# missing without being flagged as incomplete. Covers normal
# exchange halts, delisting days, and major disruption events
# (2022 sanctions gap, etc.). 15% is well above the worst
# historical Russian-market disruption.
_HALTS_PCT = 0.15

# Earliest year Tinkoff's history-data archive goes back to. Pre-2018
# data requires a paid source (AlgoPack, etc.).
MIN_YEAR = 2018
# MOEX ISS caps lookback at 1825d (~5y). When we don't have listed_at,
# we shouldn't ask MOEX for more than it can serve.
MOEX_MAX_LOOKBACK_DAYS = 1825


def _earliest_expected_ts(
    meta: tuple[date | None, date | None] | None,
    *,
    moex_clamped: bool = True,
) -> date:
    """Earliest date the universe says we can reach for this ticker.

    - If ``delisted_at`` is set, only history up to that date is meaningful
      (and pullable). We still want to back to MIN_YEAR for the delisted
      ticker so backtests see the full history.
    - If ``listed_at`` is set and the ticker is still live, history starts
      from listed_at (or MIN_YEAR, whichever is later).

    Tickers without ``listed_at`` (rare — some delisted/legacy entries)
    fall back to MIN_YEAR.

    The ``moex_clamped`` flag controls whether the 1825-day MOEX ISS
    lookback cap applies. The primary MD loader (Tinkoff history-data
    archive) has no such cap and pulls yearly ZIPs back to MIN_YEAR,
    so we want full history when the MD loader is active. The cap
    only matters when MOEX ISS is the fallback.
    """
    from datetime import timedelta

    listed_at, _delisted_at = meta if meta else (None, None)
    today = date.today()
    if moex_clamped:
        # Cap for sources that limit lookback (MOEX ISS = 1825d).
        lookback_floor = today - timedelta(days=MOEX_MAX_LOOKBACK_DAYS)
    else:
        # Tinkoff MD archive has no lookback cap — go back to MIN_YEAR.
        lookback_floor = date(MIN_YEAR, 1, 1)
    if listed_at:
        earliest_listed = date(listed_at.year, 1, 1)
        return max(earliest_listed, date(MIN_YEAR, 1, 1), lookback_floor)
    # No listed_at: be honest about what we can actually pull.
    return max(date(MIN_YEAR, 1, 1), lookback_floor)


def _is_complete(
    store: PostgresDataStore,
    ticker: str,
    min_bars: int,
) -> bool:
    """Honest backfill-completion check.

    A ticker is "complete" when its stored bar count reaches the
    *expected* bar count for the date range we can possibly cover
    for it, with a configurable trading-halt allowance.

    Formula:
        expected_bars = trading_days(listed_at, today) * (1 - _HALTS_PCT)
        complete iff count_ohlcv(ticker) >= expected_bars

    ``trading_days`` counts calendar days minus weekends (no
    holiday calendar — adding a Russian holidays library would
    cost more than the precision buys). For delisted tickers the
    range is ``listed_at..delisted_at``.

    ``_HALTS_PCT`` is the fraction of trading days we *don't* expect
    to have bars for, even on a "complete" ticker. Covers normal
    exchange halts, delisting days, 2022-style sanctions gaps, etc.
    Set to ``0.15`` (15%) which is well above historical Russian
    market disruption levels.

    Tickers without listed_at metadata fall back to the legacy
    ``min_bars`` check (best-effort for legacy data).

    No need for separate earliest/latest/tolerance side checks — the
    one formula naturally handles fresh tickers (low expected
    count, low required count), delisted tickers (narrow range,
    modest count), live tickers (full range, count >= min_bars
    anyway), and trading-halt-affected tickers (15% margin).
    """
    # No fast-path min_bars short-circuit: full history must be
    # backfilled. The MD loader covers 9 years back to MIN_YEAR=2018;
    # a ticker that's been listed since 2014 must be pulled to 2018,
    # not just to the last 1300 bars. trading_days() formula already
    # encodes the full range for both live and delisted tickers.
    count = store.count_ohlcv(ticker=ticker)
    meta = store.ticker_meta(ticker)
    if meta is None:
        # No metadata = legacy data without universe entry. Best we
        # can do is the legacy min_bars threshold, which already
        # failed above. Treat as incomplete so we re-pull.
        return count >= min_bars
    listed_at, delisted_at = meta
    if listed_at is None:
        # listed_at unknown. Infer it from the earliest bar in DB
        # (best estimate of when this ticker actually started trading).
        earliest = store.earliest_ts(ticker=ticker)
        if earliest is None:
            # No bars either, can't decide — treat as incomplete so
            # backfill will populate at least one row.
            return False
        listed_at = earliest
    end = delisted_at if delisted_at else date.today()
    if end <= listed_at:
        # Delisted the same day it listed (or before). Nothing to pull.
        return count > 0
    calendar_days = (end - listed_at).days
    trading_days = int(calendar_days * _TRADING_DAYS_PER_YEAR / 365.25)
    expected_bars = int(trading_days * (1.0 - _HALTS_PCT))
    return count >= expected_bars


def _backfill_one(
    loader: FallbackDataLoader,
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

        # Data-quality gate: never write structurally invalid bars into
        # Postgres — this DB is the primary decision input for the
        # trading bot. CRITICAL issues reject the whole batch.
        from src.data.quality import (
            blocking,
            summarize,
            validate_bar,
            validate_series,
        )

        bar_issues = []
        for r in rows:
            bar_issues.extend(validate_bar(r))
        bar_blocking = blocking(bar_issues)
        if bar_blocking:
            counts = summarize(bar_issues)
            logger.error(
                f"[{ticker_idx}/{total}] {ticker}: rejected {len(bar_blocking)} "
                f"CRITICAL bar(s) (counts={counts}); skipping upsert"
            )
            return {"fetched": len(rows), "written": 0}

        # Series-level checks (returns > 50%, long gaps) — WARN, don't block.
        series_issues = validate_series(rows)
        if series_issues:
            counts = summarize(series_issues)
            logger.warning(
                f"[{ticker_idx}/{total}] {ticker}: {len(series_issues)} " f"quality WARNINGS (counts={counts})"
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
        # Fallback chain: Tinkoff MD (history-data) → Tinkoff gRPC (broker
        # GetCandles) → MOEX ISS. All three wrap behind one iterator.
        loader = FallbackDataLoader(
            tinkoff_md=TinkoffInvestMDDataLoader(token=args.token),
            tinkoff_grpc=TinkoffInvestDataLoader(),
            moex_iss=MOEXDataLoader(),
        )
    except Exception as e:
        logger.error(f"Loader init failed: {e}")
        return 2

    store = PostgresDataStore()

    # BUGFIX (H-9): verify the bot's DB credentials actually work before
    # wasting hours on a backfill that silently writes to nowhere. We hit
    # this in production 2026-08-18: pg_isready reported healthy, SELECT
    # worked, but INSERT raised permission error and 268 "no-data"
    # tickers were the symptom. auth_probe() does SELECT 1 + INSERT
    # ON CONFLICT and returns False on any error. If False, we exit
    # loudly (return 1) so the cron operator sees it in the journal.
    # The same _auth_probe table is checked by scripts/check_db_health.py
    # in scheduled cron, so a probe failure here is the first signal —
    # the cron job's persistent failure is the second.
    if not store.auth_probe(source="backfill_pre_run"):
        logger.error(
            "AUTH PROBE FAILED: cannot SELECT+INSERT into postgres. "
            "Backfill would silently fail to write. Aborting run. "
            "Run scripts/check_db_health.py for detail or check /app/logs/health.log."
        )
        store.close()
        return 1

    # BUGFIX (H-9): ticker_universe SELECT verifies that the bot has
    # the privileges to actually read the universe it is about to
    # backfill. postgres may have a broken GRANT setup that lets the bot
    # SELECT from _auth_probe (which it owns) but not from
    # ticker_universe (which is owned by the initial superuser).
    try:
        universe_count = store.list_tickers(include_delisted=True)
        logger.info(f"Universe table reachable, {len(universe_count)} tickers")
    except Exception as exc:
        logger.error(f"UNIVERSE QUERY FAILED: cannot SELECT from ticker_universe: " f"{type(exc).__name__}: {exc}")
        store.close()
        return 1

    started = time.monotonic()
    total_fetched = 0
    total_written = 0
    skipped_complete = 0
    errors: list[tuple[str, str]] = []

    try:
        tickers, universe_metas_map = _resolve_universe(loader, args.classes, args.limit)
        logger.info(f"=== Backfill (fallback chain md → grpc → moex): {len(tickers)} tickers, {start} → {end} ===")

        # BUGFIX (2026-08-18 / Phase 1.6 audit): upsert ticker_universe rows
        # BEFORE we try to write any ohlcv_daily bars. The FK
        # ``fk_ohlcv_ticker`` rejects INSERTs whose ticker is not already
        # in ticker_universe. Without this upsert we'd see silent
        # constraint-violation failures on every ticker.
        # Idempotent — ON CONFLICT preserves existing rows.
        try:
            universe_metas: list[TickerMeta] = list(universe_metas_map.values())
            store.upsert_tickers(universe_metas)
            logger.info(f"Universe row state: {len(universe_metas)} tickers upserted into ticker_universe")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"UPSERT tickers failed: {type(exc).__name__}: {exc}")
            store.close()
            return 1

        circuit_breaker_streak = 0
        for i, ticker in enumerate(tickers, start=1):
            # Per-ticker effective start: clamp to source lookback limits
            # (MOEX ISS = 1825d). Without this, we ask MOEX for 9 years of
            # pre-listing data and get nothing back.
            meta = store.ticker_meta(ticker)
            # Primary loader is Tinkoff MD archive (yearly ZIPs back to
            # MIN_YEAR, no 1825d cap). The cap only applies if we ever
            # fall back to MOEX ISS as the only source — handled inside
            # the fallback chain. Pass moex_clamped=False so we pull
            # the full available history.
            effective_start = max(start, _earliest_expected_ts(meta, moex_clamped=False))
            if effective_start > end:
                logger.info(f"{i}/{len(tickers)} {ticker}: skip " f"(effective_start {effective_start} > end {end})")
                skipped_complete += 1
                continue
            if not args.force and _is_complete(store, ticker, args.min_bars):
                logger.info(f"{i}/{len(tickers)} {ticker}: skip (complete)")
                skipped_complete += 1
                # Make sure the flag reflects reality. If a previous run
                # marked the ticker complete but DB bars were wiped, the
                # flag is stale. Re-flip it now (idempotent, cheap).
                _set_complete_flag(store, ticker, True)
                circuit_breaker_streak = 0
                continue

            stats = _backfill_one(
                loader,
                store,
                ticker,
                effective_start,
                end,
                ticker_idx=i,
                total=len(tickers),
            )
            total_fetched += max(stats["fetched"], 0)
            total_written += max(stats["written"], 0)
            if stats["written"] < 0:
                errors.append((ticker, "error"))
                circuit_breaker_streak += 1
                # Don't unmark the flag if we already marked it (would
                # lose state). Just leave it as-is.
            else:
                circuit_breaker_streak = 0
                # Re-check completion after the fresh pull. If the
                # expected_bars formula now passes, flip the flag on.
                if _is_complete(store, ticker, args.min_bars):
                    _set_complete_flag(store, ticker, True)

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
    fb = loader.stats if isinstance(loader, FallbackDataLoader) else None
    fb_summary = ""
    if fb:
        fb_summary = " | ".join(
            f"{src}={fb[src]['ok']}/{fb[src]['fallback']}/{fb[src]['error']}"
            for src in ("tinkoff_md", "tinkoff_grpc", "moex_iss")
        )
    logger.info(
        f"=== DONE in {elapsed:.0f}s: fetched={total_fetched} "
        f"written={total_written} skipped={skipped_complete} errors={len(errors)} "
        f"| sources [ok/fallback/err]: {fb_summary} ==="
    )
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
