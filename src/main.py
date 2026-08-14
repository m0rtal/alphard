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
import sys
import time


def main() -> None:
    """Phase 0 heartbeat stub. Replaced by Coordinator in Phase 5.2."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("alphard")

    logger.info("Alphard bot starting (Phase 0 stub)... ")
    logger.warning("No agents implemented yet. Phase 0 ships a heartbeat only.")

    # Phase 1 replaces this loop with FastAPI app + /health + /metrics.
    try:
        while True:
            logger.info("Heartbeat — agents not yet active")
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down... ")
        sys.exit(0)


if __name__ == "__main__":
    main()
