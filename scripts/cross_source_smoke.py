#!/usr/bin/env python3
"""Cross-source validation smoke test runner (Phase 2.6).

Phase 2.6 wires the Level-2 Quality Gate (``cross_source.check_cross_source``
from src/data/quality/cross_source.py) into the bot's runtime so that
divergence between Tinkoff MD and MOEX ISS feeds is detected before
downstream signals (ML, decision_log) consume bad data.

WHY A SMOKE TEST (NOT A FULL WIRING)
-------------------------------------
The current Postgres schema stores a SINGLE source per (ticker, ts) —
upsert on the (ticker, ts, source) PK means: if daily_sync writes
``tinkoff_md`` and backfill writes ``moex_iss``, the same date has
TWO rows. The cross_source gate requires BOTH series to align on
the same date.

Phase 2.6 step 1: prove the gate works end-to-end on synthetic data
that mimics the multi-source layout. Step 2 (not in this script):
extend the schema to a multi-source (ticker, ts, source) layout and
add a multi-source loader that mirrors Tinkoff vs MOEX. Step 2 requires
a schema migration + every loader touched — that's a separate ticket.

So this script is a SCOPED smoke test: it shows the gate works on
three realistic scenarios (correlated / diverged / insufficient data)
and exits non-zero on any unexpected outcome. The CI/Docker image
can run it as a health check during deploy.

SCENARIOS
---------
1. **correlated_aligned**: two series with 60 days of small noise
   around the same drift. Expect NO HIGH issues. Pearson > 0.99.
2. **diverged_split**: same drift, but one series has a 5% step
   at day 30 (mimics an unadjusted split). Expect HIGH
   XSC_DIVERGENCE_HIGH + MEDIUM XSC_CORRELATION_LOW.
3. **insufficient_data**: only 3 aligned dates. Expect HIGH
   XSC_SOURCE_MISSING (low correlation denominator).

OUTPUT
------
- Human-readable summary on stdout
- Exit code 0 = all scenarios produced expected outcomes
- Exit code 1 = one or more scenarios diverged from expectations
  (also the default Python exit code for any unhandled exception;
  the script has no `sys.exit(2)` call, so a separate "unexpected
  error" exit code is intentionally NOT documented — that contract
  would be unreachable with current deterministic inputs.)
"""

from __future__ import annotations

import logging
import math
import random
import sys
from datetime import date, timedelta

# Make alphard.src importable when run from /app
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.data.quality.cross_source import (  # noqa: E402
    CrossSourceParams,
    SourceSeries,
    check_cross_source,
)
from src.data.quality.severity import Severity  # noqa: E402

logger = logging.getLogger("alphard.cross_source_smoke")


def _make_series(
    closes: list[float],
    source_name: str,
    start: date,
) -> SourceSeries:
    """Build a SourceSeries from a close list and starting date.

    SourceSeries expects ``bars`` as ``tuple[tuple[date, float], ...]``
    — (date, close) pairs. We mint those directly to avoid the
    Bar-coinheritance dance (Level 2 only needs date + close).
    """
    if not closes:
        raise ValueError("closes must be non-empty")
    bars = tuple((start + timedelta(days=i), float(c)) for i, c in enumerate(closes))
    return SourceSeries(source_name=source_name, bars=bars)


def _random_walk_close(
    n: int,
    start: float,
    drift: float,
    sigma: float,
    seed: int,
) -> list[float]:
    """Deterministic geometric Brownian motion with a fixed seed."""
    rng = random.Random(seed)
    closes: list[float] = [start]
    for _ in range(n - 1):
        ret = drift + sigma * rng.gauss(0, 1)
        closes.append(closes[-1] * math.exp(ret))
    return closes


def _issue_count(report, sev: Severity) -> int:
    """Helper: count issues at a given severity."""
    return len(report.by_severity(sev))


def scenario_correlated_aligned() -> tuple[str, int, int]:
    """Two series with matching drift + small noise. Expect clean."""
    start = date(2024, 1, 1)
    a = _random_walk_close(60, 250.0, 0.0005, 0.01, seed=42)
    # b follows a with tiny noise — represents a healthy mirror source
    rng = random.Random(7)
    b = [x * (1 + rng.gauss(0, 0.001)) for x in a]
    sa = _make_series(a, "tinkoff_md", start)
    sb = _make_series(b, "moex_iss", start)
    report = check_cross_source("SBER", sa, sb)
    high = _issue_count(report, Severity.HIGH)
    medium = _issue_count(report, Severity.MEDIUM)
    return ("correlated_aligned", high, medium)


def scenario_diverged_split() -> tuple[str, int, int]:
    """Same drift, but one series has a 5% step at day 30 (unadjusted split)."""
    start = date(2024, 1, 1)
    a = _random_walk_close(60, 250.0, 0.0005, 0.01, seed=42)
    b = list(a)
    # Inject a 5% step at day 30 — stock split, one source applied
    # it, the other did not. Same slice seen as a discontinuity.
    for i in range(30, 60):
        b[i] = b[i] * 0.95
    sa = _make_series(a, "tinkoff_md", start)
    sb = _make_series(b, "moex_iss", start)
    report = check_cross_source("SBER", sa, sb, params=CrossSourceParams())
    return (
        "diverged_split",
        _issue_count(report, Severity.HIGH),
        _issue_count(report, Severity.MEDIUM),
    )


def scenario_insufficient_data() -> tuple[str, int, int]:
    """Only 3 aligned dates. Expect HIGH XSC_SOURCE_MISSING."""
    start = date(2024, 1, 1)
    a = _random_walk_close(3, 250.0, 0.0005, 0.01, seed=42)
    b = list(a)
    sa = _make_series(a, "tinkoff_md", start)
    sb = _make_series(b, "moex_iss", start)
    report = check_cross_source("SBER", sa, sb)
    return (
        "insufficient_data",
        _issue_count(report, Severity.HIGH),
        _issue_count(report, Severity.MEDIUM),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Cross-source validation smoke test (Phase 2.6)")

    scenarios = [
        scenario_correlated_aligned,
        scenario_diverged_split,
        scenario_insufficient_data,
    ]

    # Expected outcomes: (expected_high_count, must_have_high?)
    expectations = {
        "correlated_aligned": (0, False),  # 0 HIGH issues expected
        "diverged_split": (None, True),  # at least 1 HIGH expected
        "insufficient_data": (1, True),  # exactly 1 HIGH XSC_SOURCE_MISSING
    }

    rc = 0
    for fn in scenarios:
        name, high, medium = fn()
        exp_high, exp_any = expectations[name]
        verdict = "OK"
        if exp_high is not None and high != exp_high:
            verdict = f"FAIL (expected {exp_high} HIGH, got {high})"
            rc = 1
        elif exp_any and high == 0:
            verdict = "FAIL (expected HIGH issues, got 0)"
            rc = 1
        elif not exp_any and high > 0:
            verdict = f"FAIL (expected clean, got {high} HIGH)"
            rc = 1
        logger.info(f"  scenario={name} high={high} medium={medium} verdict={verdict}")

    if rc == 0:
        logger.info("All scenarios produced expected outcomes.")
    else:
        logger.error("One or more scenarios diverged from expectations.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
