"""Alphard bot entrypoint (Phase 0 stub).

WHAT IS HERE
------------
A heartbeat loop that runs forever and logs every 60 seconds. This is
intentionally minimal — every real component (data ingestion, agents,
broker connector, risk gate, portfolio optimizer) lives elsewhere.

WHAT IS NOT HERE (intentional gaps, deferred to later phases)
-------------------------------------------------------------
- Broker integration (Tinkoff / MOEX): Phase 1.3
- Data ingestion (Tinkoff REST, MOEX ISS, AlgoPack): Phase 1.1
- 8 autonomous agents (Data / Quant / Macro / Portfolio / Execution / News /
  Audit / Risk): Phase 1+
- Risk Agent lives in src/risk/gate.py — see RiskLimits/RiskDecision
  pydantic models for the money-gate contract. This entrypoint does not
  call RiskGate directly; the Coordinator (Phase 5.2) wires agents.
- HTTP /health endpoint: Phase 1 (FastAPI stub at minimum).
- Decision lineage JSONB → Postgres: Phase 3.
- Prometheus /metrics endpoint: Phase 3.
- ML model scoring (LightGBM / cross-sectional): Phase 2.
- Macro regime detection (HMM or symptom-based): Phase 2.
- Rebalance logic: Phase 4.
- Token rotation, kill-switch, ML drift detection: Phase 4.

CURRENT BEHAVIOUR (Phase 0 only)
--------------------------------
- Emit a structured log line every 60s declaring bot is alive.
- Refuse to start unless TINKOFF_SANDBOX_TOKEN or TINKOFF_REAL_TOKEN
  is set (entrypoint.sh sanity gate), or ALLOW_NO_BROKER=true for dev.
- Exit cleanly on SIGINT.

See docs/RUNBOOK.md for incident-response procedures and the
docs/AUDIT-Phase0-FINAL.md for the security audit context.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

MSK_TZ = timezone(timedelta(hours=3))  # MOEX closes 18:40 MSK; sync at 20:00 MSK to capture it.


DAILY_SYNC_INTERVAL_SECONDS = 3600  # Phase 1.6: user requirement — sync must always run.
DAILY_SYNC_SUBPROCESS_TIMEOUT = 600  # 10 min hard cap per sync; longer = kill.

# Phase 2.7: delisted_at weekly cron. Backfills delisted_at via Tinkoff gRPC
# market_data.get_candles, running on a weekly cadence (delisted events are
# slow-moving). Mirrors daily_sync structure: subprocess + sentinel + watchdog.
DELISTED_SYNC_CADENCE_SECONDS = 7 * 24 * 3600  # 7 days between runs.
DELISTED_SYNC_SUBPROCESS_TIMEOUT = 2400  # 40 min hard cap; larger window than daily.

# Phase 2.5 step 2b: weekly apply_corporate_actions cron. Pulls MOEX ISS
# splits per ticker and re-applies them to raw OHLCV bars, persisting
# split-adjusted bars to ohlcv_daily_adj. Same daemon pattern as
# delisted_sync: subprocess + sentinel + watchdog. 7-day cadence mirrors
# delisted_sync because split events are also slow-moving (a handful
# per year on MOEX).
CORP_ACTIONS_APPLY_CADENCE_SECONDS = 7 * 24 * 3600  # 7 days between runs.
# 60 min hard cap: walking 3000 tickers through apply_split_adjustment
# is fast (~1ms/ticker), so even a worst-case cold-cache run finishes
# inside 30 min. 60 min gives headroom for MOEX ISS latency.
CORP_ACTIONS_APPLY_SUBPROCESS_TIMEOUT = 3600

# Phase 1.6 audit: in-process watchdog for the daily_sync daemon thread.
# A thread that crashes inside a live process leaves no signal — heartbeat
# keeps ticking, container stays "Up", but the daily schedule is silently
# broken. The watchdog reads _daily_sync_health.last_successful_run_at
# every 30 min and sys.exit(1) if it's older than the threshold, which
# causes Docker to restart the container (restart: unless-stopped) and
# the daemon thread to be re-spawned fresh.
WATCHDOG_INTERVAL_SECONDS = 1800  # 30 min between watchdog checks.
WATCHDOG_STALE_SECONDS = 26 * 3600  # 26h — generous over 24h daily cadence.


def _seconds_until_next_target_hour_msk(target_hour: int, target_minute: int) -> float:
    """How many seconds until the next target_hour:target_minute MSK.

    Phase 1.6 requirement: daily_sync must run AFTER MOEX closes its daily
    candle (18:40 MSK). We schedule for 20:00 MSK = 80 minutes after close,
    giving Tinkoff plenty of time to ingest and expose the closed bar.

    If target is already past today, schedule for tomorrow.
    """
    now_msk = datetime.now(MSK_TZ)
    target = now_msk.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if target <= now_msk:
        target = target + timedelta(days=1)
    delta = target - now_msk
    return max(0.0, delta.total_seconds())


_shutdown_event = threading.Event()  # Phase 1.6: signals daemon threads to exit


# ---------------------------------------------------------------------------
# Phase 2.x fix (2026-08-20): backfill supervisor
# ---------------------------------------------------------------------------
#
# Original entrypoint.sh did `setsid python3 ... &` and exec'd into src.main.
# That left the backfill as an orphaned grandchild: when it crashed (and it
# did, see below), nothing reaped it AND nothing restarted it, so the
# container kept ticking heartbeat with a zombie PID 19 holding a stale
# Postgres connection — exactly the "network stall" symptom everyone
# misdiagnosed for 17 hours on sha-bc867a2.
#
# Real root cause (caught 2026-08-20 via py-spy / State: Z (zombie) on
# /proc/19/stat): the --skip-known-bad flag (commit bc867a2) accessed
# ``meta.delisted_at`` on a raw psycopg tuple returned by
# PostgresDataStore.ticker_meta() — AttributeError on first ticker, exit 1,
# zombie, no supervisor, no restart.
#
# Fix: bring the launch INSIDE src.main so a real Python supervisor can
# waitpid() the child and respawn on death. The shell-level `setsid` launch
# in entrypoint.sh is removed; this thread owns the lifecycle. Backoff
# between respawns is bounded so a tight crash loop is visible in logs.
_BACKFILL_SCRIPT_ARGS: tuple[str, ...] = (
    "--limit",
    "5500",
    "--start-year",
    "2018",
    "--min-bars",
    "1300",
)
_BACKFILL_RESPAWN_BACKOFF_SECONDS = 30
_BACKFILL_MAX_RESPAWNS_PER_HOUR = 10  # >10 deaths/hour = fatal: stop the loop

# Module-level logger so _spawn_backfill can log without depending on
# main() having called logging.basicConfig() yet.
_supervisor_logger = logging.getLogger("alphard.backfill_supervisor")


def _spawn_backfill() -> int:
    """Fork+setsid a fresh backfill daemon. Returns the child PID.

    Using subprocess.Popen with start_new_session=True (the Python equivalent
    of shell `setsid`) so the child is its own session leader and survives
    any signal delivered to this main process. Output is appended to the
    shared log so all forensics live in one file.

    The log path is passed via the BACKFILL_LOG env var and the child opens
    its own FileHandler. The parent does NOT inherit any fd to it — earlier
    versions did, which leaked one fd per respawn until the soft fd limit
    (1024 default / 4096 hard on Linux) tripped silently and the supervisor
    died without the container noticing (issue #48). The child-owns-the-fd
    pattern eliminates the leak at source.
    """
    log_path = os.environ.get("BACKFILL_LOG", "/app/logs/backfill_history_md.log")
    child_env = os.environ.copy()
    child_env["BACKFILL_LOG"] = log_path
    proc = subprocess.Popen(
        ["python3", "scripts/backfill_history_md.py", *_BACKFILL_SCRIPT_ARGS],
        cwd="/app",
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _supervisor_logger.info(
        f"_spawn_backfill: pid={proc.pid} args={_BACKFILL_SCRIPT_ARGS} log={log_path} "
        f"(child opens FileHandler from BACKFILL_LOG; parent holds no fd)"
    )
    return proc.pid


def _backfill_supervisor_loop() -> None:
    """Supervise the backfill daemon: waitpid, respawn on death, back off.

    Runs forever until ``_shutdown_event``. Counts ONLY crashes
    (``rc != 0``) toward the per-hour rate limit
    (``_BACKFILL_MAX_RESPAWNS_PER_HOUR``). Clean exits (``rc == 0``) —
    e.g. universe already complete, ``mark_terminally_failed`` exhausted,
    or sandbox universe empty — are respawned without incrementing the
    death counter. This carve-out was added in PR #57 after the
    2026-08-20 sawtooth-uptime incident (root cause: empty-universe
    ``rc=0`` exits tripped the cap every ~6 minutes and zeroed the
    container-uptime gauge on every Docker restart). See issue #59 for
    the invariant hardening (``rc`` is always bound before use, even
    under ``python -O``).

    On excessive CRASHES (>10/hour) it logs CRITICAL and exits non-zero
    so Docker restart kicks in with fresh state.
    """
    death_timestamps: list[float] = []
    while not _shutdown_event.is_set():
        pid = _spawn_backfill()
        # Default to rc=0 so every exit path leaves `rc` bound. This
        # makes the post-loop code safe under `python -O` (where a
        # runtime check on `rc` would be compiled out) and explicit
        # about what the ChildProcessError branch means: "child
        # disappeared, treat as clean exit, do not count toward the
        # death cap".
        rc = 0
        # Block until child exits or shutdown fires.
        while not _shutdown_event.is_set():
            try:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                # Already reaped by something else (host cgroup reaper,
                # docker exec, operator `kill -9` from another shell).
                # Treat as clean exit: rc stays 0 (the default above),
                # no death-counter increment, no WARN spam in normal
                # operation. Log only as INFO so the operator can see
                # reaps if they look.
                _supervisor_logger.info(
                    f"_backfill_supervisor_loop: child pid={pid} reaped by something "
                    f"other than us; treating as clean exit"
                )
                break
            if waited_pid == pid:
                rc = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
                _supervisor_logger.warning(
                    f"_backfill_supervisor_loop: child pid={pid} exited rc={rc}; "
                    f"respawning in {_BACKFILL_RESPAWN_BACKOFF_SECONDS}s"
                )
                break
            # Sleep interruptibly so shutdown is responsive.
            _sleep_interruptible(5)
        else:
            # Shutdown requested while waiting — kill child and exit.
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            break
        # Backoff before respawn.
        _sleep_interruptible(_BACKFILL_RESPAWN_BACKOFF_SECONDS)
        # Prune `death_timestamps` on EVERY iteration (cheap when empty,
        # bounded when not) so the list cannot grow unbounded on long
        # stretches of clean exits. Only APPEND on crashes — clean exits
        # must not poison the rate-limit window. See issue #60: pre-fix
        # the prune+append was inside `if rc != 0:`, so each clean exit
        # leaked one float and the list grew ~2880 entries/day at the
        # 30s respawn cadence (≈1M/year, ≈28 MB/year).
        now = time.monotonic()
        death_timestamps = [t for t in death_timestamps if now - t < 3600]
        # Rate-limit: count ONLY CRASHES (rc != 0) in the last hour. The
        # code-path producing rc=0 is: (a) the ChildProcessError branch
        # above (default), (b) a clean backfill finish — empty sandbox
        # universe, ``mark_terminally_failed_exhausted``, or every ticker
        # skipped via ``_is_complete``. Note that auth failures in the
        # backfill return rc=1 (auth_probe, src/main.py:597) and per-
        # ticker errors return rc=2 (errors accumulator, src/main.py:756),
        # so the 2026-08-20 incident's "Tinkoff 401 every 2 seconds"
        # narrative was operator-observation shorthand, not the literal
        # exit path — the literal exit path was "universe pass finished
        # with no writes". See Grafana panel ``alphard_backfill_rc`` at
        # 2026-08-20T10:30Z for the actual sequence.
        if rc != 0:
            death_timestamps.append(now)
            if len(death_timestamps) > _BACKFILL_MAX_RESPAWNS_PER_HOUR:
                _supervisor_logger.critical(
                    f"_backfill_supervisor_loop: backfill crashed {len(death_timestamps)} "
                    f"times in the last hour (>_BACKFILL_MAX_RESPAWNS_PER_HOUR); "
                    f"aborting container so Docker can restart cleanly with fresh state."
                )
                os._exit(1)


def _daily_sync_loop() -> None:
    """Run scripts/daily_sync.py as an isolated subprocess every hour.

    Why subprocess instead of in-process call?
    - daily_sync.main() does its own loader/store init and tear-down. Reusing
      our main-thread pg_store connection would conflict with backfill and
      risk leaking transactions across runs.
    - A subprocess crash (loader OOM, Tinkoff rate-limit) MUST NOT kill the
      heartbeat. Process boundary = circuit breaker.

    Why 1 hour cadence?
    - Tinkoff candles update on real trades. Daily bars close at 18:40 MSK;
      19:00 + 1h delay = 20:00 first run covers yesterday's bar.
    - 24 daily-sync runs / day is wasteful (each ~30s for top 20). Hourly
      keeps the universe warm without burning gRPC rate-limit tokens.

    Schedule (Phase 1.6 user requirement):
    - First sync waits until the next 20:00 MSK (after MOEX close at 18:40).
    - Subsequent syncs repeat every 24h, anchored to MSK wall-clock time.
    - On any launch, the daemon sleeps to the next target, not "right now" —
      a container started at 12:00 must NOT immediately sync (would race
      the still-open candle).
    """
    logger = logging.getLogger("alphard.daily_sync")

    sync_hour_msk = 20
    sync_minute_msk = 0
    seconds_to_first = _seconds_until_next_target_hour_msk(sync_hour_msk, sync_minute_msk)
    logger.info(f"daily_sync scheduled: next run at 20:00 MSK " f"(in {seconds_to_first / 3600:.1f}h)")
    _sleep_interruptible(seconds_to_first)

    while not _shutdown_event.is_set():
        logger.info("Triggering daily_sync subprocess (--days 5)")
        try:
            r = subprocess.run(
                ["python", "scripts/daily_sync.py", "--days", "5"],
                capture_output=True,
                text=True,
                timeout=DAILY_SYNC_SUBPROCESS_TIMEOUT,
                cwd="/app",
            )
            if r.returncode == 0:
                tail = r.stdout[-500:] if r.stdout else ""
                logger.info(f"daily_sync OK rc={r.returncode}: {tail!r}")
            else:
                tail = (r.stderr or "")[-500:]
                logger.warning(f"daily_sync FAILED rc={r.returncode}: {tail!r}")
        except subprocess.TimeoutExpired:
            logger.warning(f"daily_sync timeout after {DAILY_SYNC_SUBPROCESS_TIMEOUT}s; " "subprocess killed")
        except Exception as exc:  # noqa: BLE001 — never kill the heartbeat
            logger.error(f"daily_sync unexpected error: {exc}")
        # Wait 24h to the next 20:00 MSK. We don't recompute via
        # _seconds_until_next_target_hour_msk here because if the sync
        # itself took 30 minutes (subprocess timeout path) the math
        # would shift. We anchor on 24h-since-last-fire, which keeps the
        # rhythm roughly daily even if a run drags.
        if _shutdown_event.is_set():
            logger.info("daily_sync daemon received shutdown signal, exiting")
            return
        _sleep_interruptible(24 * 3600)


def _delisted_sync_loop() -> None:
    """Run scripts/backfill_delisted_via_tinkoff.py weekly.

    Phase 2.7: pulls delisted_at via Tinkoff gRPC market_data.get_candles
    chunks. Backfills universe rows where class_code IS NULL OR delisted=True
    AND ohlcv_daily is empty. Runs on a 7-day cadence (delisted events are
    slow-moving).

    Why subprocess instead of in-process call?
    - backfill_delisted_via_tinkoff.py walks the universe ticker-by-ticker and
      runs 1-year chunks per ticker. Long-running, blocking, allocates Tinkoff
      gRPC connections. In-process call would starve the heartbeat.
    - Subprocess crash MUST NOT kill the heartbeat. Process boundary = circuit
      breaker. Same rationale as daily_sync.

    Schedule (Phase 2.7):
    - First run waits 24h after launch (let daily_sync settle first).
    - Subsequent runs repeat every 7 days. Anchored on 7d-since-last-fire.
    - On any launch, the daemon sleeps before the first run.
    """
    logger = logging.getLogger("alphard.delisted_sync")

    # Wait 24h before first run: daily_sync gets priority, delisted is weekly.
    logger.info(
        f"delisted_sync scheduled: first run in 24h, "
        f"then every {DELISTED_SYNC_CADENCE_SECONDS / 3600 / 24:.0f} days"
    )
    _sleep_interruptible(24 * 3600)

    while not _shutdown_event.is_set():
        logger.info("Triggering delisted_sync subprocess")
        try:
            r = subprocess.run(
                ["python", "scripts/backfill_delisted_via_tinkoff.py"],
                capture_output=True,
                text=True,
                timeout=DELISTED_SYNC_SUBPROCESS_TIMEOUT,
                cwd="/app",
            )
            if r.returncode == 0:
                tail = r.stdout[-500:] if r.stdout else ""
                logger.info(f"delisted_sync OK rc={r.returncode}: {tail!r}")
            else:
                tail = (r.stderr or "")[-500:]
                logger.warning(f"delisted_sync FAILED rc={r.returncode}: {tail!r}")
        except subprocess.TimeoutExpired:
            logger.warning(f"delisted_sync timeout after {DELISTED_SYNC_SUBPROCESS_TIMEOUT}s; " "subprocess killed")
        except Exception as exc:  # noqa: BLE001 — never kill the heartbeat
            logger.error(f"delisted_sync unexpected error: {exc}")
        if _shutdown_event.is_set():
            logger.info("delisted_sync daemon received shutdown signal, exiting")
            return
        _sleep_interruptible(DELISTED_SYNC_CADENCE_SECONDS)


def _corp_actions_apply_loop() -> None:
    """Run scripts/apply_corporate_actions.py weekly.

    Phase 2.5 step 2b: fetches MOEX ISS splits per ticker and re-applies
    them to raw OHLCV bars via ``src.data.adjustment.apply_split_adjustment``.
    Persists the result to ``ohlcv_daily_adj`` (a parallel table — raw
    ``ohlcv_daily`` is never overwritten).

    Why subprocess instead of in-process call?
    - The orchestrator walks the entire universe (3000+ tickers) and
      holds open a Postgres connection for the duration. In-process
      would block the heartbeat for tens of minutes. Subprocess
      isolates the connection lifecycle and any latent IO errors from
      the heartbeat thread.
    - Subprocess crash MUST NOT kill the heartbeat. Process boundary
      = circuit breaker, same rationale as daily_sync and delisted_sync.

    Schedule (Phase 2.5 step 2b):
    - First run waits 24h after launch (let daily_sync and delisted_sync
      settle first; corp_actions_apply is the lowest-priority of the
      three because split events are slow-moving).
    - Subsequent runs repeat every 7 days. Anchored on 7d-since-last-fire
      to keep the rhythm weekly even if a single run drags.
    - The script owns its own per-ticker idempotency cache (default
      /var/lib/alphard/cache/corp_actions_applied.json, 7-day window),
      so a re-run within the same week is a fast no-op.
    """
    logger = logging.getLogger("alphard.corp_actions_apply")

    # Wait 24h before first run: daily_sync and delisted_sync get
    # priority. Corp actions are slow-moving (a handful per year) so
    # a 24h startup delay is invisible.
    logger.info(
        f"corp_actions_apply scheduled: first run in 24h, "
        f"then every {CORP_ACTIONS_APPLY_CADENCE_SECONDS / 3600 / 24:.0f} days, "
        f"subprocess_timeout={CORP_ACTIONS_APPLY_SUBPROCESS_TIMEOUT}s"
    )
    _sleep_interruptible(24 * 3600)

    while not _shutdown_event.is_set():
        logger.info("Triggering apply_corporate_actions subprocess")
        try:
            r = subprocess.run(
                ["python", "scripts/apply_corporate_actions.py"],
                capture_output=True,
                text=True,
                timeout=CORP_ACTIONS_APPLY_SUBPROCESS_TIMEOUT,
                cwd="/app",
            )
            if r.returncode == 0:
                tail = r.stdout[-500:] if r.stdout else ""
                logger.info(f"corp_actions_apply OK rc={r.returncode}: {tail!r}")
            else:
                tail = (r.stderr or "")[-500:]
                logger.warning(f"corp_actions_apply FAILED rc={r.returncode}: {tail!r}")
        except subprocess.TimeoutExpired:
            logger.warning(
                f"corp_actions_apply timeout after {CORP_ACTIONS_APPLY_SUBPROCESS_TIMEOUT}s; "
                "subprocess killed"
            )
        except Exception as exc:  # noqa: BLE001 — never kill the heartbeat
            logger.error(f"corp_actions_apply unexpected error: {exc}")
        if _shutdown_event.is_set():
            logger.info("corp_actions_apply daemon received shutdown signal, exiting")
            return
        _sleep_interruptible(CORP_ACTIONS_APPLY_CADENCE_SECONDS)


def _sleep_interruptible(seconds: float) -> None:
    """Sleep up to `seconds`, but wake up immediately on shutdown_event.

    Replaces naked time.sleep() so Ctrl-C / daemon shutdown actually does
    something useful. Polls every 1s — cheap enough for daemon workloads.
    """
    end = time.monotonic() + seconds
    while not _shutdown_event.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        try:
            time.sleep(min(1.0, remaining))
        except KeyboardInterrupt:
            return


def _run_daily_sync_watchdog() -> None:
    """Detect a stuck daily_sync daemon thread and force a container restart.

    Reads _daily_sync_health.last_successful_run_at. If it's missing
    (never_run) or older than WATCHDOG_STALE_SECONDS, the daily_sync
    daemon thread has either crashed silently or is wedged. The only
    reliable recovery is to restart the process; Docker does this
    automatically via `restart: unless-stopped` when we exit non-zero.

    Pre-first-run state (last_successful_run_at IS NULL right after
    container start) must NOT trigger a restart — the daemon thread
    is still waiting for its first 20:00 MSK slot. We skip the check
    until 24h after container start to give the daemon a fair chance.

    Any DB error in the watchdog is logged and swallowed — the heartbeat
    must never propagate exceptions that would crash the main process.
    """
    from src.data.pg_store import PostgresDataStore  # late import: avoid cycle

    logger = logging.getLogger("alphard.watchdog")
    try:
        store = PostgresDataStore()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"watchdog: cannot init store ({type(exc).__name__}: {exc}) — skipping")
        return

    try:
        last_run = store.last_daily_sync_run_at()
        if last_run is None:
            # Sentinel never stamped. Distinguish "just started" from
            # "daemon silently broke and never stamped". Use container
            # uptime as the heuristic: if we've been up < 24h, this is
            # the legitimate pre-first-run state. If older, the daemon
            # is broken.
            from datetime import datetime, timezone

            # /proc/1/stat field 22 is start_time in clock ticks since boot.
            # We need clock ticks → seconds via sysconf(_SC_CLK_TCK).
            # posix.clock_gettime(CLOCK_BOOTTIME) is simpler and exact.
            try:
                import os

                # Prefer /proc/1/stat start time if available.
                with open("/proc/1/stat") as fh:
                    fields = fh.read().split()
                start_jiffies = int(fields[21])
                clk_tck = os.sysconf("SC_CLK_TCK")
                start_sec = start_jiffies / clk_tck
                # We don't know container boot epoch without /proc/stat —
                # but in our case /proc/1 is the entrypoint-sh, which
                # forks python, so its start time is the container start.
                # To convert to wall clock: read /proc/stat btime.
                with open("/proc/stat") as fh:
                    btime = 0
                    for line in fh:
                        if line.startswith("btime "):
                            btime = int(line.split()[1])
                            break
                if btime == 0:
                    raise RuntimeError("btime not found in /proc/stat")
                boot_time = datetime.fromtimestamp(btime, tz=timezone.utc)
                container_start = boot_time.replace() + (
                    datetime.fromtimestamp(start_sec, tz=timezone.utc) - datetime.fromtimestamp(0, tz=timezone.utc)
                )
                uptime_sec = (datetime.now(timezone.utc) - container_start).total_seconds()
            except Exception:  # noqa: BLE001
                # Fallback: assume pre-first-run and skip. Better to
                # skip than to crash the heartbeat.
                logger.info("watchdog: cannot determine container uptime — skipping " "(assume pre-first-run)")
                return
            if uptime_sec < 24 * 3600:
                logger.info(
                    f"watchdog: no daily_sync run yet, but container age "
                    f"= {uptime_sec / 3600:.1f}h < 24h — skipping"
                )
                return
            logger.critical(
                "watchdog: _daily_sync_health never stamped after 24h "
                "uptime — daily_sync daemon thread is broken or wedged. "
                "Triggering container restart via sys.exit(1)."
            )
            store.close()
            sys.exit(1)

        from datetime import datetime, timezone

        age_sec = (datetime.now(timezone.utc) - last_run).total_seconds()
        if age_sec > WATCHDOG_STALE_SECONDS:
            logger.critical(
                f"watchdog: last_successful_run_at = {last_run.isoformat()} "
                f"({age_sec / 3600:.1f}h ago) > {WATCHDOG_STALE_SECONDS / 3600:.0f}h "
                f"threshold — daily_sync daemon thread is broken or wedged. "
                f"Triggering container restart via sys.exit(1)."
            )
            store.close()
            sys.exit(1)

        logger.info(f"watchdog: daily_sync OK (last run {age_sec / 3600:.1f}h ago)")
    except Exception as exc:  # noqa: BLE001
        # Watchdog must never propagate. If the watchdog itself crashes,
        # the heartbeat still ticks and a human can investigate.
        logger.warning(f"watchdog: probe failed ({type(exc).__name__}: {exc}) — " f"skipping this cycle")
    finally:
        # Fail-secure (issue #14 D.1): a broken ``store.close()`` MUST
        # be logged, not silently swallowed. The historical
        # ``except Exception: pass`` masked Postgres connection
        # failures during shutdown, leaving operators blind to
        # post-mortem gaps.
        try:
            store.close()
        except Exception as exc:
            logger.warning(
                "store.close() failed during shutdown: %s: %s",
                type(exc).__name__,
                exc,
            )


def main() -> None:
    """Phase 0 heartbeat stub. Replaced by Coordinator in Phase 5.2.

    In-process daily sync daemon (Phase 1.6): runs in a background thread
    so the heartbeat keeps ticking regardless of sync outcomes. See
    `_daily_sync_loop` for the contract.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("alphard")

    logger.info("Alphard bot starting (Phase 0 stub)... ")
    logger.warning("Phase 1 ships heartbeat + 1h daily_sync daemon. " "Coordinator (Phase 5.2) replaces this loop.")

    # Phase 2.x (2026-08-20): backfill supervisor. Owns the lifecycle of
    # the backfill_history_md.py subprocess — spawns once at container
    # start, waitpid's on death, respawns with backoff. Without this the
    # `setsid ... &` pattern in entrypoint.sh left the daemon as an
    # orphaned grandchild that nobody could reap or restart. See the
    # long docstring above _spawn_backfill for the full incident history.
    backfill_thread = threading.Thread(
        target=_backfill_supervisor_loop, daemon=True, name="alphard-backfill-supervisor"
    )
    backfill_thread.start()
    logger.info("backfill-supervisor daemon started (owning backfill_history_md.py subprocess)")

    # Phase 1.6: spin up the daily-sync daemon thread. Daemon=True so it
    # exits with the main process; the heartbeat keeps ticking regardless
    # of sync outcomes (subprocess isolates failures).
    sync_thread = threading.Thread(target=_daily_sync_loop, daemon=True, name="alphard-daily-sync")
    sync_thread.start()
    logger.info(
        f"daily-sync daemon started (interval={DAILY_SYNC_INTERVAL_SECONDS}s, "
        f"subprocess_timeout={DAILY_SYNC_SUBPROCESS_TIMEOUT}s)"
    )

    logger.info("daily-sync daemon started")

    # Phase 2.7: weekly delisted_at cron. Same daemon pattern as daily_sync:
    # subprocess + sentinel-able. First run waits 24h after launch.
    delisted_thread = threading.Thread(target=_delisted_sync_loop, daemon=True, name="alphard-delisted-sync")
    delisted_thread.start()
    logger.info(
        f"delisted-sync daemon started (cadence={DELISTED_SYNC_CADENCE_SECONDS / 3600 / 24:.0f}d, "
        f"subprocess_timeout={DELISTED_SYNC_SUBPROCESS_TIMEOUT}s)"
    )

    # Phase 2.5 step 2b: weekly corp_actions_apply cron. Same daemon pattern
    # as delisted_sync: subprocess + sentinel + watchdog. Lowest priority
    # of the three weekly daemons (split events are slow-moving); first run
    # waits 24h after launch.
    corp_actions_thread = threading.Thread(
        target=_corp_actions_apply_loop, daemon=True, name="alphard-corp-actions-apply"
    )
    corp_actions_thread.start()
    logger.info(
        f"corp-actions-apply daemon started "
        f"(cadence={CORP_ACTIONS_APPLY_CADENCE_SECONDS / 3600 / 24:.0f}d, "
        f"subprocess_timeout={CORP_ACTIONS_APPLY_SUBPROCESS_TIMEOUT}s)"
    )

    # Phase 2.8 step 1: Prometheus metrics HTTP server. Stdlib ThreadingHTTPServer
    # bound to ALPHARD_METRICS_PORT (default 8765) on 0.0.0.0. Exposes
    # /health (cheap liveness probe) and /metrics (Prometheus text exposition
    # format). Port-bind failure is logged at WARNING and ignored — Prometheus
    # scrape is observability, not a hard dependency for the trading loop.
    metrics_port = int(os.environ.get("ALPHARD_METRICS_PORT", "8765"))
    try:
        from src.metrics_server import MetricsServer

        _metrics_server = MetricsServer(host="0.0.0.0", port=metrics_port)
        _metrics_server.start()
        # Stash the registry on module-level so heartbeat / supervisor loops can
        # emit counters and gauges without re-importing.
        globals()["_metrics_registry"] = _metrics_server.registry
        logger.info(f"metrics-server started (0.0.0.0:{metrics_port}, /health + /metrics)")
    except OSError as exc:
        logger.warning(f"metrics-server failed to bind :{metrics_port}: {exc}; continuing without metrics")
        globals()["_metrics_registry"] = None

    # Watchdog: checks the _daily_sync_health sentinel every 30 min. If
    # last_successful_run_at is older than WATCHDOG_STALE_SECONDS (26h),
    # the daily_sync daemon thread has either crashed or is wedged, and
    # the only recovery is to restart this process — which Docker does
    # automatically because the compose service has `restart: unless-stopped`.
    # Without this, a silent daemon thread crash leaves the bot running
    # with no daily schedule until a human notices. sys.exit(1) in the
    # heartbeat loop is the simplest, most reliable signal to Docker.
    heartbeat_counter = 0
    try:
        while not _shutdown_event.is_set():
            logger.info("Heartbeat — agents not yet active")
            heartbeat_counter += 1
            # Phase 2.8 step 1: emit heartbeat metric. Bump counter and update
            # the last-tick gauge so Prometheus can alert on stale heartbeats
            # via (time() - alphard_heartbeat_last_tick_timestamp) > N.
            _registry = globals().get("_metrics_registry")
            if _registry is not None:
                _registry.inc_counter("alphard_heartbeats_total")
                _registry.set_gauge("alphard_heartbeat_last_tick_timestamp", time.time())
            # Run the watchdog check every Nth tick instead of every tick.
            # 30 min / 60s = 30 ticks; 60*30=1800s. Use min counter to be
            # robust to sleep overshoots.
            if heartbeat_counter * 60 >= WATCHDOG_INTERVAL_SECONDS:
                heartbeat_counter = 0
                _run_daily_sync_watchdog()
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down... ")
    finally:
        _shutdown_event.set()
        sync_thread.join(timeout=10)
        if sync_thread.is_alive():
            logger.warning("daily-sync daemon did not exit within 10s")
        delisted_thread.join(timeout=10)
        if delisted_thread.is_alive():
            logger.warning("delisted-sync daemon did not exit within 10s")
# Issue #72: join the backfill supervisor so we don't exit
        # mid-_spawn_backfill and orphan the child process. The
        # supervisor's outer `while not _shutdown_event.is_set()` exits
        # on the next _sleep_interruptible poll (<= 1s) and breaks out
        # of the inner waitpid loop; the in-flight child is NOT killed
        # by the supervisor (let it finish its current pass) — the
        # os._exit(1) on the death-cap branch is the only path that
        # forces an early child kill, and that one is a Docker-driven
        # restart path. Timeout is _BACKFILL_RESPAWN_BACKOFF_SECONDS+5
        # so we always outlive one full backoff cycle.
        backfill_thread.join(timeout=_BACKFILL_RESPAWN_BACKOFF_SECONDS + 5)
        if backfill_thread.is_alive():
            logger.warning(
                "backfill-supervisor did not exit within "
                f"{_BACKFILL_RESPAWN_BACKOFF_SECONDS + 5}s; "
                "child PID may be orphaned — verify with "
                "`ps -ef | grep backfill_history_md` after container exit"
            )
        corp_actions_thread.join(timeout=10)
        if corp_actions_thread.is_alive():
            logger.warning("corp-actions-apply daemon did not exit within 10s")
        _metrics_registry_obj = globals().get("_metrics_registry")
        if _metrics_registry_obj is not None and "_metrics_server" in globals():
            try:
                globals()["_metrics_server"].stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"metrics-server stop failed: {exc}")
        sys.exit(0)


if __name__ == "__main__":
    main()
