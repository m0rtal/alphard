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

DAILY_SYNC_INTERVAL_SECONDS = 3600  # Phase 1.6: user requirement — sync must always run.
DAILY_SYNC_SUBPROCESS_TIMEOUT = 600  # 10 min hard cap per sync; longer = kill.


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

    Shutdown contract: main() sets `_shutdown_event` and exits the process.
    The daemon polls the event between iterations and on KeyboardInterrupt;
    either signal causes a clean return.
    """
    logger = logging.getLogger("alphard.daily_sync")
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
        # Sleep in short slices so shutdown is responsive.
        for _ in range(DAILY_SYNC_INTERVAL_SECONDS):
            if _shutdown_event.is_set():
                logger.info("daily_sync daemon received shutdown signal, exiting")
                return
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("daily_sync daemon sleep interrupted, exiting")
                return


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

    try:
        while not _shutdown_event.is_set():
            logger.info("Heartbeat — agents not yet active")
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
