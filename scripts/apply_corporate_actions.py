"""Phase 2.5 step 2b/2c: apply split AND dividend adjustments to OHLCV bars.

Why this script?
----------------
PHASE1-AUDIT flagged "Adjusted prices — adj_close = close placeholder".
Phase 2.5 ships the pipeline in three pieces:

  - Step 1 (PR #45): pure adjustment math in ``src.data.adjustment``
    (splits only at that point).
  - Step 2a (PR #54): standalone fetcher ``scripts/fetch_moex_corporate_actions.py``
    that pulls MOEX ISS splits + dividends into JSON.
  - Step 2b (PR #74): orchestrator that combines splits with raw
    OHLCV bars, persists split-adjusted bars into ``ohlcv_daily_adj``.
  - Step 2c (this commit, branch feat/issue-dividend-apply): extend the
    orchestrator so the same pipeline applies BOTH splits and
    dividends. The pure-math stage was extended in
    ``src.data.adjustment.apply_dividend_adjustment`` (subtract
    cumulative dividends from ``adj_close`` for bars before the
    dividend ex-date) and ``apply_adjustment`` is now the unified
    entry point that runs splits first, then dividends. PHASE2-ROADMAP
    explicitly defers dividend handling past step 2b — step 2c lands it.

This script is invoked as a subprocess by ``src.main._corp_actions_apply_loop``
on a weekly cadence. It can also be invoked manually::

    python3 scripts/apply_corporate_actions.py                    # full universe
    python3 scripts/apply_corporate_actions.py --tickers SBER,GAZP # one-off smoke
    python3 scripts/apply_corporate_actions.py --dry-run          # log only, no DB writes
    python3 scripts/apply_corporate_actions.py --force            # bypass 7d idempotency

Storage
-------
Split- and dividend-adjusted bars land in ``ohlcv_daily_adj`` (a
parallel table introduced alongside this script in PR #74). The raw
``ohlcv_daily`` is NEVER touched. When Phase 2.6 step 2 lands the
``source`` column on ``ohlcv_daily``, the parallel table is migrated
via ``INSERT INTO ohlcv_daily ... SELECT FROM ohlcv_daily_adj``.

Idempotency
-----------
A JSON cache at ``--cache-path`` (default ``/var/lib/alphard/cache/corp_actions_applied.json``)
records ``{ticker: last_applied_iso8601}``. Tickers whose last apply is
within ``--skip-older-than-days`` (default 7) are skipped on subsequent
runs unless ``--force`` is set. The cache itself is tolerant to
corruption — a JSON decode error deletes the cache and treats every
ticker as fresh (defensive: a corrupt cache must never silently lose
adjustment work).

Per-ticker error handling
-------------------------
If ``apply_adjustment`` or the DB write raises for one ticker, the
exception is logged with the ticker name + traceback and the loop
continues with the next ticker. The non-fatal path is the default
because a single bad ticker must not abort the weekly run for 3000+
others; only a fatal error (store init, schema, network fetch) exits
non-zero.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

# scripts/ is on sys.path via pyproject.toml's [tool.pytest.ini_options]
# pythonpath = ["scripts"]. For direct invocation we rely on the
# /app layout (src/ as a sibling package); import via sys.path here so
# the script also works from a checkout tree without `pip install -e .`.
_SRC_PATH = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

import fetch_moex_corporate_actions as mca  # noqa: E402

from src.data.adjustment import apply_adjustment  # noqa: E402
from src.data.models import CorporateAction, OHLCVRow, TickerMeta  # noqa: E402
from src.data.pg_store import PostgresDataStore  # noqa: E402
from src.data.store import DataStore, StoreError  # noqa: E402

logger = logging.getLogger("alphard.corp_actions_apply")

DEFAULT_CACHE_PATH = Path("/var/lib/alphard/cache/corp_actions_applied.json")
DEFAULT_FETCH_TIMEOUT = 60  # matches fetch_moex_corporate_actions.REQUEST_TIMEOUT
DEFAULT_SKIP_OLDER_THAN_DAYS = 7
PROGRESS_HEARTBEAT_EVERY = 50  # one log line per N tickers processed

EXIT_OK = 0
EXIT_FATAL = 1  # store init, schema, network — whole run aborted
EXIT_USAGE = 2  # arg parsing failure

# MOEX ISS endpoint is the source of truth — same URL as
# scripts/fetch_moex_corporate_actions.py. We hit it directly via the
# shared fetcher module (which already has retry+backoff per PR #54)
# so the orchestrator inherits the same behaviour as the standalone
# snapshot script.
_FETCHER_MOD = mca


def _build_store(args: argparse.Namespace) -> DataStore:
    """Construct the production Postgres store.

    Reads ``$ALPHARD_PG_DSN`` when ``--pg-dsn`` is omitted. Tests
    bypass this function entirely by passing ``store=`` directly to
    ``main()``; see the ``main()`` docstring.
    """
    dsn = args.pg_dsn or None
    return PostgresDataStore(dsn=dsn)


def _list_tickers(store: DataStore, *, only: Iterable[str] | None = None) -> list[TickerMeta]:
    """All listed tickers (delisted included — backfill may need them).

    The ``listed_at IS NOT NULL`` filter matches the task spec. Delisted
    tickers with a known delisted_at are still included because their
    pre-delisting history may carry splits that we want adjusted.
    """
    all_metas = store.list_tickers(include_delisted=True)
    listed = [m for m in all_metas if m.listed_at is not None]
    if only is None:
        return listed
    only_set = {t.strip().upper() for t in only}
    return [m for m in listed if m.ticker in only_set]


# ---- MOEX ISS per-run cache (issue #137, fix #140) -------------------------
# Both ``fetch_splits`` and ``fetch_dividends`` return the entire MOEX
# market's data in one HTTP round-trip, but the per-ticker helpers
# were called inside the main loop, so a 3000-ticker universe issued
# 6000 redundant HTTP requests per weekly run.
#
# ``_MOEX_CACHE`` collapses them into one fetch per endpoint per run:
# it is reset at the start of every ``main()`` invocation, so each
# subprocess run is guaranteed exactly one HTTP round-trip per
# endpoint regardless of universe size. Module-level state is
# acceptable because the script is a short-lived subprocess (started
# by ``_corp_actions_apply_loop`` or by an operator from the shell) —
# there is no long-running interpreter state to leak across runs.
#
# Historical note (issue #140): the first cut keyed the cache on
# ``id(session)`` and leaked across tests — CPython recycles
# ``id()`` of freed objects, so a new ``requests.Session`` built by
# the next ``main()`` call would inherit the previous test's cached
# fetcher payload. ``main()``-level reset is the simpler and more
# defensible contract: every orchestrator run starts with a clean
# cache, regardless of how many sessions are GC'd and reused in
# between. Tests no longer need to call ``_reset_moex_cache_for_tests``
# between assertions, but the helper is kept for the rare test that
# wants to pre-seed the cache.
_MOEX_CACHE: dict[str, list[dict]] = {}


def _reset_moex_cache_for_tests() -> None:
    """Test-only: clear the MOEX per-session cache between assertions.

    Not called from production code. ``main()`` calls
    ``_reset_moex_cache_for_tests`` itself at entry so a fresh
    orchestrator run always starts with an empty cache.
    """
    _MOEX_CACHE.clear()


def _all_splits_for_session(session: requests.Session, timeout: int) -> list[dict]:
    """Return the full MOEX splits list, fetching it at most once per session.

    See module-level ``_MOEX_CACHE`` docstring for the rationale.
    """
    if "splits" not in _MOEX_CACHE:
        _MOEX_CACHE["splits"] = _FETCHER_MOD.fetch_splits(session, timeout=timeout)
    return _MOEX_CACHE["splits"]


def _all_dividends_for_session(session: requests.Session, timeout: int) -> list[dict]:
    """Return the full MOEX dividends list, fetching it at most once per session."""
    if "dividends" not in _MOEX_CACHE:
        _MOEX_CACHE["dividends"] = _FETCHER_MOD.fetch_dividends(session, timeout=timeout)
    return _MOEX_CACHE["dividends"]


def _fetch_splits_for_ticker(
    ticker: str,
    session: requests.Session,
    timeout: int,
) -> list[CorporateAction]:
    """Per-ticker CorporateAction list (only kind='split').

    Kept as a thin wrapper around ``_FETCHER_MOD.fetch_splits`` for
    backwards-compat with the existing test surface (``aca._fetch_splits_for_ticker``
    is patched by tests in ``test_apply_corporate_actions.py``). New code
    should call ``_fetch_actions_for_ticker`` instead, which returns
    both splits and dividends.

    Step 2a's fetcher returns ALL splits for ALL tickers in one call.
    We invoke it once and filter in-process; calling it per ticker would
    hit MOEX ISS 3000 times for nothing. Filter by ticker AND kind.

    Issue #137: the underlying ``fetch_splits`` call is cached per
    orchestrator run (via module-level ``_MOEX_CACHE``, reset at the
    start of ``main()``) so a weekly run with N tickers makes exactly
    ONE HTTP round-trip to ``/splits.json``, not N. Issue #140: the
    cache is keyed by endpoint, not by ``id(session)`` — CPython
    recycles ``id()`` of freed ``Session`` objects and that leaked
    stale payloads across tests.
    """
    raw = _all_splits_for_session(session, timeout)
    actions: list[CorporateAction] = []
    for entry in raw:
        if entry.get("ticker") != ticker:
            continue
        # MOEX returns ISO date strings ("2014-06-16").
        ts_raw = entry.get("ts")
        ratio_raw = entry.get("ratio")
        if not ts_raw or ratio_raw is None:
            continue
        try:
            ts = date.fromisoformat(ts_raw)
            ratio = Decimal(str(ratio_raw))
        except (TypeError, ValueError):
            # Defensive: MOEX ISS has historical entries in non-standard
            # formats. step 2a's fetcher drops malformed ratios; we just
            # skip anything that survives in an unexpected shape.
            continue
        if ratio <= Decimal("0"):
            continue
        actions.append(
            CorporateAction(
                ticker=ticker,
                ts=ts,
                kind="split",
                value=ratio,
                source="moex",
            )
        )
    return actions


def _parse_dividend_entry(entry: dict, ticker: str) -> CorporateAction | None:
    """Convert a MOEX-style dividend dict into a CorporateAction(kind='dividend').

    Returns None on malformed input (missing ticker, unparseable date
    or amount, negative amount). Negative-amount rows are dropped
    defensively — they are not a real product of MOEX ISS but we don't
    want a corrupt feed to silently propagate into apply_adjustment.
    """
    if entry.get("ticker") != ticker:
        return None
    ts_raw = entry.get("ts")
    amount_raw = entry.get("amount_rub_per_share")
    if not ts_raw or amount_raw is None:
        return None
    try:
        ts = date.fromisoformat(ts_raw)
        # Decimal() raises decimal.InvalidOperation (not ValueError) on
        # unparseable strings — catch the broader InvalidOperation parent
        # class plus ValueError so a corrupt feed can never crash the
        # orchestrator mid-loop.
        amount = Decimal(str(amount_raw))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if amount < Decimal("0"):
        return None
    return CorporateAction(
        ticker=ticker,
        ts=ts,
        kind="dividend",
        value=amount,
        source="moex",
    )


def _fetch_dividends_for_ticker(
    ticker: str,
    session: requests.Session,
    timeout: int,
) -> list[CorporateAction]:
    """Per-ticker dividend CorporateAction list.

    Sister to ``_fetch_splits_for_ticker``. Issue #137: shares the
    same per-run cache (``_all_dividends_for_session``) so a
    full-universe run makes exactly ONE HTTP round-trip to
    ``/dividends.json``, not N. Issue #140: the cache is keyed by
    endpoint (not by ``id(session)``) and is reset by ``main()`` at
    entry so successive ``main()`` calls can't inherit each other's
    payloads via ``id()`` recycling. Returns an empty list if MOEX
    has no dividends for the ticker or the call fails (the caller
    treats an empty list as "nothing to apply" rather than as an
    error).
    """
    raw = _all_dividends_for_session(session, timeout)
    actions: list[CorporateAction] = []
    for entry in raw:
        action = _parse_dividend_entry(entry, ticker)
        if action is not None:
            actions.append(action)
    return actions


def _fetch_actions_for_ticker(
    ticker: str,
    session: requests.Session,
    timeout: int,
) -> list[CorporateAction]:
    """Fetch splits + dividends for ``ticker`` in one MOEX ISS round-trip per kind.

    The fetcher returns ALL splits and ALL dividends across the entire
    share market in one call; we filter by ticker in-process to avoid
    3000 round-trips. Splits and dividends are concatenated into a
    single list — apply_adjustment() dispatches by kind.

    Phase 2.5 step 2c (this commit) wires dividends into the
    orchestrator: every ticker now receives both kinds of corporate
    action. Previously the orchestrator only fetched splits and
    dividends sat in ``corporate_actions`` ignored. See PHASE2-ROADMAP
    for the deferred-work context.
    """
    splits = _fetch_splits_for_ticker(ticker, session, timeout)
    dividends = _fetch_dividends_for_ticker(ticker, session, timeout)
    return splits + dividends


def _load_cache(cache_path: Path) -> dict[str, str]:
    """Read the per-ticker last-applied cache. Returns {} on any failure.

    A corrupt cache is treated as "no cache": the next apply will rewrite
    it cleanly. This is intentionally permissive — a corrupted JSON file
    must never block a weekly run.
    """
    try:
        return json.loads(cache_path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "corp_actions cache at %s is unreadable (%s); resetting to empty",
            cache_path,
            exc,
        )
        return {}


def _save_cache(cache_path: Path, cache: dict[str, str]) -> None:
    """Atomically write the per-ticker cache.

    We do NOT want a crash mid-write to corrupt the cache and silently
    lose idempotency state. The pattern is: write to ``cache_path.tmp``,
    then rename. The rename is atomic on POSIX filesystems, so the
    previous cache stays intact if the run crashes mid-write.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(cache, indent=2, sort_keys=True))
    tmp_path.replace(cache_path)


def _is_fresh(cache_entry: str | None, skip_older_than_days: int) -> bool:
    """True if the ticker was applied within the skip window."""
    if not cache_entry:
        return False
    try:
        last = datetime.fromisoformat(cache_entry)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last
    return age < timedelta(days=skip_older_than_days)


def _apply_for_ticker(
    store: DataStore,
    ticker: str,
    actions: list[CorporateAction],
    dry_run: bool,
) -> int:
    """Apply ``actions`` to raw OHLCV for ``ticker``; return rows upserted.

    Returns the count of adjusted bars written (or that *would* be
    written under ``dry_run=True``). On no-op (empty actions / empty raw
    feed) returns 0.

    Idempotency at the row level is achieved by passing the raw rows
    (not already-adjusted rows) through ``apply_split_adjustment`` and
    letting the upsert overwrite. Re-running produces the same adjusted
    output because the input is unchanged.
    """
    if not actions:
        return 0

    # Pull the full history. The adjusted-output range should match the
    # raw-input range (1:1 row count) so we don't impose a date window
    # here — every pre-split bar must be scaled.
    #
    # source='tkf' is mandatory post-Phase 2.6 step 2 (issue #136):
    # ``ohlcv_daily_adj`` is keyed on (ticker, ts) only, so reading both
    # sources and feeding them through ``apply_adjustment`` would run
    # ``upsert_ohlcv_adj`` twice per date — the second write silently
    # overwrites the first via ``ON CONFLICT (ticker, ts) DO UPDATE``,
    # dropping half the adjusted output without any error. 'tkf' is the
    # primary source ('tkf' is the default in OHLCVRow.source) and
    # matches the orchestrator's pre-Phase-2.6 behavior.
    raw_rows = store.query_ohlcv(
        ticker=ticker,
        start=date(1990, 1, 1),  # far past — covers the whole Russian market
        end=date(2100, 1, 1),  # far future — covers any stored bar
        source="tkf",
    )
    if not raw_rows:
        logger.info("ticker=%s no raw OHLCV rows; nothing to adjust", ticker)
        return 0

    adjusted = apply_adjustment(raw_rows, actions)
    if dry_run:
        logger.info(
            "ticker=%s dry-run: would upsert %d adjusted bars (from %d raw)",
            ticker,
            len(adjusted),
            len(raw_rows),
        )
        return len(adjusted)

    written = store.upsert_ohlcv_adj(adjusted)
    logger.info(
        "ticker=%s applied %d actions, upserted %d/%d adjusted bars (source=tkf)",
        ticker,
        len(actions),
        written,
        len(raw_rows),
    )
    return written


def _parse_args() -> argparse.Namespace:
    """argparse wrapper: parses ``sys.argv[1:]`` (production)."""
    return _parse_args_from(sys.argv[1:])


def _parse_args_from(argv: list[str]) -> argparse.Namespace:
    """argparse wrapper: parses an explicit argv list (testability)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tickers",
        type=str,
        default=None,
        help=(
            "Comma-separated whitelist of tickers to apply (e.g. 'SBER,GAZP,VTBR'). "
            "Default: full universe from ticker_universe where listed_at IS NOT NULL."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be done without writing to Postgres.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass 7d idempotency cache — reapply every ticker.",
    )
    parser.add_argument(
        "--skip-older-than-days",
        type=int,
        default=DEFAULT_SKIP_OLDER_THAN_DAYS,
        help=(
            "Skip tickers whose last successful apply is younger than this many days. "
            f"Default {DEFAULT_SKIP_OLDER_THAN_DAYS}. Set to 0 to disable (equivalent to --force)."
        ),
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=("Path to the per-ticker last-applied JSON cache. " f"Default {DEFAULT_CACHE_PATH}."),
    )
    parser.add_argument(
        "--pg-dsn",
        type=str,
        default=None,
        help=(
            "Postgres DSN. Defaults to $ALPHARD_PG_DSN. "
            "Used only by the production path; tests inject a store via the "
            "``store=`` kwarg of main()."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_FETCH_TIMEOUT,
        help=f"Per-request timeout in seconds (default {DEFAULT_FETCH_TIMEOUT}).",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    store: DataStore | None = None,
) -> int:
    """Orchestrator entry point.

    Parameters
    ----------
    argv : list[str] | None
        CLI arguments. ``None`` means ``sys.argv[1:]`` (production).
    store : DataStore | None
        Pre-constructed store. ``None`` means build a PostgresDataStore
        from the parsed args (production). Tests inject an
        InMemorySQLiteStore so they can inspect the resulting
        ``ohlcv_daily_adj`` rows after ``main()`` returns.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Issue #140: reset the MOEX ISS per-run cache at entry so a fresh
    # orchestrator run always starts empty, regardless of any state
    # left over from earlier runs in the same interpreter (tests, or
    # an interactive REPL session).
    _reset_moex_cache_for_tests()
    args = _parse_args() if argv is None else _parse_args_from(argv)

    if store is None:
        try:
            store = _build_store(args)
        except (StoreError, OSError) as exc:
            logger.error("cannot initialise store: %s: %s", type(exc).__name__, exc)
            return EXIT_FATAL
        # We constructed the store ourselves, so we own its lifecycle.
        # A caller-injected store is closed by the caller (tests want
        # to keep using it after main() returns).
        _owns_store = True
    else:
        _owns_store = False

    # At this point ``store`` is bound to a concrete DataStore. The type
    # narrowing is implicit because every code path above raises or
    # assigns.
    assert store is not None  # noqa: S101 — defensive, only for type checkers

    try:
        tickers = _list_tickers(
            store,
            only=args.tickers.split(",") if args.tickers else None,
        )
    except StoreError as exc:
        logger.error("cannot list tickers: %s: %s", type(exc).__name__, exc)
        # NOTE: store.close() happens in the finally block below (only
        # when we own the store, see _owns_store). Injected stores are
        # not closed by main().
        return EXIT_FATAL

    logger.info("apply_corporate_actions: %d tickers in scope", len(tickers))

    cache = _load_cache(args.cache_path) if not args.force else {}
    skip_window_days = 0 if args.force else args.skip_older_than_days

    session = requests.Session()
    session.headers["User-Agent"] = mca.USER_AGENT

    totals = {"applied": 0, "skipped_fresh": 0, "no_actions": 0, "error": 0, "rows_written": 0}
    applied_at = datetime.now(timezone.utc).isoformat()

    try:
        for i, meta in enumerate(tickers, start=1):
            if i % PROGRESS_HEARTBEAT_EVERY == 0 or i == len(tickers):
                logger.info(
                    "progress: %d/%d tickers processed (applied=%d, skipped=%d, error=%d)",
                    i,
                    len(tickers),
                    totals["applied"],
                    totals["skipped_fresh"],
                    totals["error"],
                )
            ticker = meta.ticker

            if not args.force and _is_fresh(cache.get(ticker), skip_window_days):
                logger.debug("ticker=%s fresh in cache; skipping", ticker)
                totals["skipped_fresh"] += 1
                continue

            try:
                actions = _fetch_actions_for_ticker(ticker, session, args.timeout)
            except requests.RequestException as exc:
                logger.warning(
                    "ticker=%s MOEX ISS fetch failed: %s: %s; skipping",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                totals["error"] += 1
                continue
            except Exception as exc:  # noqa: BLE001 — never kill the orchestrator
                logger.error(
                    "ticker=%s unexpected fetch error: %s: %s\n%s",
                    ticker,
                    type(exc).__name__,
                    exc,
                    traceback.format_exc(),
                )
                totals["error"] += 1
                continue

            if not actions:
                # No splits in the window — still mark as applied so we
                # don't re-fetch every week. Per-ticker empty-list is
                # the steady-state for most tickers.
                cache[ticker] = applied_at
                totals["no_actions"] += 1
                continue

            try:
                rows_written = _apply_for_ticker(store, ticker, actions, args.dry_run)
            except (StoreError, ValueError) as exc:
                logger.warning(
                    "ticker=%s apply failed: %s: %s; skipping",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                totals["error"] += 1
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "ticker=%s unexpected apply error: %s: %s\n%s",
                    ticker,
                    type(exc).__name__,
                    exc,
                    traceback.format_exc(),
                )
                totals["error"] += 1
                continue

            cache[ticker] = applied_at
            totals["applied"] += 1
            totals["rows_written"] += rows_written
    finally:
        # Always persist cache + close store, even on KeyboardInterrupt.
        # A partial cache is better than no cache — the weekly run will
        # pick up where it left off.
        try:
            if not args.dry_run:
                _save_cache(args.cache_path, cache)
            else:
                logger.info("dry-run: cache not written")
        except OSError as exc:
            logger.error("cache write failed: %s: %s", type(exc).__name__, exc)
        if _owns_store:
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("store.close failed: %s: %s", type(exc).__name__, exc)

    logger.info(
        "apply_corporate_actions: done applied=%d skipped_fresh=%d no_actions=%d " "error=%d rows_written=%d",
        totals["applied"],
        totals["skipped_fresh"],
        totals["no_actions"],
        totals["error"],
        totals["rows_written"],
    )
    # Non-zero exit if every attempted ticker errored — operator must
    # notice. Zero errors or partial success still exit 0; the per-
    # ticker error count is logged for forensics.
    if totals["applied"] == 0 and totals["no_actions"] == 0 and totals["error"] > 0:
        logger.error("apply_corporate_actions: every attempted ticker errored")
        return EXIT_FATAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "EXIT_OK",
    "EXIT_FATAL",
    "EXIT_USAGE",
    "DEFAULT_CACHE_PATH",
    "DEFAULT_SKIP_OLDER_THAN_DAYS",
    "PROGRESS_HEARTBEAT_EVERY",
    "_apply_for_ticker",
    "_build_store",
    "_fetch_actions_for_ticker",
    "_fetch_dividends_for_ticker",
    "_fetch_splits_for_ticker",
    "_is_fresh",
    "_list_tickers",
    "_load_cache",
    "_parse_dividend_entry",
    "_save_cache",
    "main",
]
