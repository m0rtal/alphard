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
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

MSK_TZ = timezone(timedelta(hours=3))  # MOEX closes 18:40 MSK; sync at 20:00 MSK to capture it.


DAILY_SYNC_INTERVAL_SECONDS = 3600  # Phase 1.6: user requirement — sync must always run.
DAILY_SYNC_SUBPROCESS_TIMEOUT = 600  # 10 min hard cap per sync; longer = kill.

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
        try:
            store.close()
        except Exception:
            pass


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
        sys.exit(0)


if __name__ == "__main__":
    main()
