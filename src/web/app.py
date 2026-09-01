"""alphard-web backend.

Issue #390. Single-purpose internal service that queries
`alphard-postgres` directly and serves the dashboard HTML +
JSON endpoints. Replaces Grafana + Prometheus for the four gauges
the operator uses.

Layering rule (from SOUL.md):
- This is the service layer. It only calls into the existing
  `pg_store.connect_psycopg()` helper for raw psycopg connections.
  It does NOT call into the loader chain, supervisor, or coordinator.
"""

from __future__ import annotations

from typing import Any

# Issue #390: default timeseries window. One month covers the typical
# supervisor pass rate (~17 tickers/min) without flooding the chart.
DEFAULT_TIMESERIES_DAYS: int = 30


# --- Query builders ------------------------------------------------------
# Kept as pure functions (no DB connection) so they're trivially unit
# testable. Each builder returns (sql, params_dict) ready for psycopg.


def build_kpis_query() -> tuple[str, dict[str, Any]]:
    """Returns one row with: universe_size, tickers_complete, ohlcv_rows, last_bar_at."""
    sql = (
        "SELECT "
        " (SELECT COUNT(*) FROM ticker_universe) AS universe_size, "
        " (SELECT COUNT(*) FROM ticker_universe WHERE backfill_complete = TRUE) "
        "   AS tickers_complete, "
        " (SELECT COUNT(*) FROM ohlcv_daily) AS ohlcv_rows, "
        " (SELECT MAX(ts)::date FROM ohlcv_daily) AS last_bar_at"
    )
    return sql, {}


def build_ohlcv_timeseries_query(days: int = DEFAULT_TIMESERIES_DAYS) -> tuple[str, dict[str, Any]]:
    """Returns one row per day with cumulative ohlcv_rows count."""
    sql = (
        "SELECT "
        "  date_trunc('day', ts)::date AS bucket, "
        "  COUNT(*) AS rows_in_bucket, "
        "  SUM(COUNT(*)) OVER (ORDER BY date_trunc('day', ts)::date) AS cum_rows "
        "FROM ohlcv_daily "
        "WHERE ts >= NOW() - make_interval(days => %(days)s) "
        "GROUP BY bucket "
        "ORDER BY bucket ASC"
    )
    return sql, {"days": int(days)}


def build_top_tickers_query(limit: int = 10) -> tuple[str, dict[str, Any]]:
    """Returns top-N tickers by bar count."""
    sql = (
        "SELECT ticker, COUNT(*) AS bar_count "
        "FROM ohlcv_daily "
        "GROUP BY ticker "
        "ORDER BY bar_count DESC "
        "LIMIT %(limit)s"
    )
    return sql, {"limit": int(limit)}


# --- Payload shapers ----------------------------------------------------
# Convert raw DB rows to the JSON shape exposed by the HTTP API.
# Kept as separate pure functions so the API contract is testable
# without a DB.


def kpis_row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Map a single KPIs row to the public JSON payload."""
    return {
        "universe_size": int(row["universe_size"]),
        "tickers_complete": int(row["tickers_complete"]),
        "ohlcv_rows": int(row["ohlcv_rows"]),
        "last_bar_at": (
            row["last_bar_at"].isoformat() if hasattr(row["last_bar_at"], "isoformat") else str(row["last_bar_at"])
        ),
    }


def health_payload(db_ok: bool) -> dict[str, Any]:
    """Health endpoint payload."""
    return {"ok": bool(db_ok), "db": bool(db_ok)}
