"""SQL query builders for the v2 alphard-web endpoints.

Issue #393. Pure functions (no DB connection) so they're trivially
unit-testable. Each builder returns ``(sql, params_dict)`` ready
for psycopg execution. Column names match the existing alphard
schema: ``ticker_universe``, ``ohlcv_daily``, ``macro_regime_log``,
``_daily_sync_health``, ``decision_log``, ``delisting_log``.

Layering (per SOUL.md): this module is the service layer. It
imports from ``pg_store`` only for the psycopg connection helper.
It does NOT call into the loader chain, supervisor, or coordinator.
"""

from __future__ import annotations

from typing import Any

# --- Default window for sparkline queries -------------------------------

DEFAULT_SPARKLINE_DAYS: int = 7
DEFAULT_SPARKLINE_MIN: int = 1
DEFAULT_SPARKLINE_MAX: int = 90

# --- /api/summary -------------------------------------------------------

#: Big SELECT that the dashboard view needs in one round trip.
#: Returns one row with all the scalar KPIs the operator sees at a
#: glance. Each scalar has its own sub-select so we don't pay for the
#: rows of ``ohlcv_daily`` or ``ticker_universe`` here.
SUMMARY_QUERY: str = (
    "SELECT "
    " (SELECT COUNT(*) FROM ticker_universe) AS universe_size, "
    " (SELECT COUNT(*) FROM ticker_universe "
    "    WHERE backfill_complete = TRUE) AS backfill_done, "
    " (SELECT COUNT(*) FROM ohlcv_daily) AS daily_sync_bars, "
    " (SELECT MAX(last_successful_run_at) "
    "    FROM _daily_sync_health) AS daily_sync_at, "
    " (SELECT regime FROM macro_regime_log "
    "    ORDER BY id DESC LIMIT 1) AS regime, "
    " (SELECT multiplier FROM macro_regime_log "
    "    ORDER BY id DESC LIMIT 1) AS regime_multiplier, "
    " (SELECT cbr_key_rate FROM macro_regime_log "
    "    ORDER BY id DESC LIMIT 1) AS cbr_key_rate, "
    " (SELECT usdrub_close FROM macro_regime_log "
    "    ORDER BY id DESC LIMIT 1) AS usdrub_close, "
    " (SELECT imoex_close FROM macro_regime_log "
    "    ORDER BY id DESC LIMIT 1) AS imoex_close"
)


def build_summary_query() -> tuple[str, dict[str, Any]]:
    return SUMMARY_QUERY, {}


def summary_row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Map the summary row to the public JSON shape.

    Nullable fields (regime, cbr_key_rate, etc.) come back as
    ``None`` when ``macro_regime_log`` is empty in production. The
    UI shows "no data" for those, which is the honest state.
    """
    universe = int(row.get("universe_size") or 0)
    done = int(row.get("backfill_done") or 0)
    pct = (100.0 * done / universe) if universe else 0.0
    return {
        "universe_size": universe,
        "backfill_done": done,
        "backfill_pct": round(pct, 1),
        "daily_sync_bars": int(row.get("daily_sync_bars") or 0),
        "daily_sync_at": _iso_or_none(row.get("daily_sync_at")),
        "regime": row.get("regime"),
        "regime_multiplier": _float_or_none(row.get("regime_multiplier")),
        "cbr_key_rate": _float_or_none(row.get("cbr_key_rate")),
        "usdrub_close": _float_or_none(row.get("usdrub_close")),
        "imoex_close": _float_or_none(row.get("imoex_close")),
    }


# --- /api/sparkline -----------------------------------------------------

#: Daily count of ``backfill_complete`` flag transitions. Cheap
#: because ``ticker_universe`` has only ~3.3k rows.
SPARKLINE_TICKERS_QUERY: str = (
    "SELECT date_trunc('day', backfill_complete_at)::date AS bucket, "
    "       COUNT(*) AS value "
    "FROM ticker_universe "
    "WHERE backfill_complete = TRUE "
    "  AND backfill_complete_at >= NOW() - make_interval(days => %(days)s) "
    "GROUP BY bucket "
    "ORDER BY bucket ASC"
)


def build_sparkline_tickers_query(days: int = DEFAULT_SPARKLINE_DAYS) -> tuple[str, dict[str, Any]]:
    return SPARKLINE_TICKERS_QUERY, {"days": int(days)}


#: Daily bar count delta from ohlcv_daily. Index on (ts) is
#: sufficient for this query at our row count.
SPARKLINE_BARS_QUERY: str = (
    "SELECT ts AS bucket, COUNT(*) AS value "
    "FROM ohlcv_daily "
    "WHERE ts >= NOW() - make_interval(days => %(days)s)::interval - INTERVAL '1 day' "
    "  AND ts <  NOW() "
    "GROUP BY bucket "
    "ORDER BY bucket ASC"
)


def build_sparkline_bars_query(days: int = DEFAULT_SPARKLINE_DAYS) -> tuple[str, dict[str, Any]]:
    return SPARKLINE_BARS_QUERY, {"days": int(days)}


# --- /api/tickers (paginated) ------------------------------------------


def build_tickers_list_query(
    q: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[str, dict[str, Any]]:
    """Return one row per ticker with bar count + last-bar date.

    Joins ticker_universe to a per-ticker aggregate of ohlcv_daily.
    Filters: substring match on ticker/figi, status (backfill_complete
    boolean), and pagination.
    """
    where = ["1 = 1"]
    params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
    if q:
        where.append("(t.ticker ILIKE %(q)s OR t.figi ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if status == "done":
        where.append("t.backfill_complete = TRUE")
    elif status == "running":
        # "running" maps to rows updated within the last hour but
        # not yet complete. Heuristic — supervisor has no explicit
        # "running" column.
        where.append("t.backfill_complete = FALSE AND t.updated_at > NOW() - INTERVAL '1 hour'")
    elif status == "no-data":
        where.append("t.backfill_complete = FALSE AND COALESCE(bar_count.cnt, 0) = 0")
    elif status == "delisted":
        where.append("t.delisted = TRUE")
    elif status == "failed":
        # No explicit failed column; treat very old stale rows as
        # "failed". This is a heuristic and may be tightened later.
        where.append(
            "t.backfill_complete = FALSE "
            "AND t.updated_at < NOW() - INTERVAL '7 days' "
            "AND COALESCE(bar_count.cnt, 0) > 0"
        )
    where_clause = " AND ".join(where)
    sql = (
        "SELECT t.ticker, t.figi, t.listed_at, t.delisted_at, "
        "       t.backfill_complete, t.updated_at, "
        "       COALESCE(bar_count.cnt, 0)::bigint AS bar_count, "
        "       bar_count.last_ts AS last_bar_at "
        "FROM ticker_universe t "
        "LEFT JOIN ("
        "  SELECT ticker, COUNT(*) AS cnt, MAX(ts) AS last_ts "
        "  FROM ohlcv_daily GROUP BY ticker"
        ") bar_count USING (ticker) "
        f"WHERE {where_clause} "
        "ORDER BY t.ticker ASC "
        "LIMIT %(limit)s OFFSET %(offset)s"
    )
    return sql, params


def build_tickers_count_query(
    q: str | None = None,
    status: str | None = None,
) -> tuple[str, dict[str, Any]]:
    where = ["1 = 1"]
    params: dict[str, Any] = {}
    if q:
        where.append("(ticker ILIKE %(q)s OR figi ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if status == "done":
        where.append("backfill_complete = TRUE")
    elif status == "running":
        where.append("backfill_complete = FALSE AND updated_at > NOW() - INTERVAL '1 hour'")
    elif status == "no-data":
        where.append(
            "backfill_complete = FALSE "
            "AND NOT EXISTS (SELECT 1 FROM ohlcv_daily od "
            "                WHERE od.ticker = ticker_universe.ticker)"
        )
    elif status == "delisted":
        where.append("delisted = TRUE")
    elif status == "failed":
        where.append(
            "backfill_complete = FALSE "
            "AND updated_at < NOW() - INTERVAL '7 days' "
            "AND EXISTS (SELECT 1 FROM ohlcv_daily od WHERE od.ticker = ticker_universe.ticker)"
        )
    return "SELECT COUNT(*) FROM ticker_universe WHERE " + " AND ".join(where), params


# --- /api/ticker/<symbol> ----------------------------------------------


def build_ticker_detail_query(ticker: str) -> tuple[str, dict[str, Any]]:
    """Single ticker universe row + 5 most recent bars."""
    return (
        "SELECT t.ticker, t.figi, t.listed_at, t.delisted_at, "
        "       t.backfill_complete, t.backfill_complete_at, t.updated_at, "
        "       (SELECT COUNT(*) FROM ohlcv_daily od "
        "          WHERE od.ticker = t.ticker) AS bar_count "
        "FROM ticker_universe t WHERE t.ticker = %(ticker)s",
        {"ticker": ticker},
    )


def build_ticker_recent_bars_query(ticker: str, limit: int = 5) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT ts, open, high, low, close, volume "
        "FROM ohlcv_daily WHERE ticker = %(ticker)s "
        "ORDER BY ts DESC LIMIT %(limit)s",
        {"ticker": ticker, "limit": int(limit)},
    )


# --- /api/backfill ------------------------------------------------------


def build_backfill_summary_query() -> tuple[str, dict[str, Any]]:
    """Progress + status breakdown + current ticker estimate."""
    return (
        "WITH counts AS ("
        "  SELECT "
        "    COUNT(*) FILTER (WHERE backfill_complete = TRUE) AS done, "
        "    COUNT(*) FILTER (WHERE backfill_complete = FALSE "
        "                       AND updated_at > NOW() - INTERVAL '1 hour') AS running, "
        "    COUNT(*) FILTER (WHERE backfill_complete = FALSE "
        "                       AND updated_at <= NOW() - INTERVAL '1 hour' "
        "                       AND updated_at > NOW() - INTERVAL '7 days') AS pending, "
        "    COUNT(*) FILTER (WHERE backfill_complete = FALSE "
        "                       AND NOT EXISTS (SELECT 1 FROM ohlcv_daily od "
        "                                          WHERE od.ticker = ticker_universe.ticker)) AS no_data, "
        "    COUNT(*) FILTER (WHERE backfill_complete = FALSE "
        "                       AND updated_at <= NOW() - INTERVAL '7 days' "
        "                       AND EXISTS (SELECT 1 FROM ohlcv_daily od "
        "                                       WHERE od.ticker = ticker_universe.ticker)) AS failed, "
        "    COUNT(*) FILTER (WHERE delisted = TRUE) AS delisted, "
        "    COUNT(*) AS total "
        "  FROM ticker_universe"
        "), current_row AS ("
        "  SELECT ticker, figi FROM ticker_universe "
        "  WHERE backfill_complete = FALSE "
        "  ORDER BY updated_at DESC NULLS LAST LIMIT 1"
        ") "
        "SELECT counts.done, counts.running, counts.pending, "
        "       counts.no_data, counts.failed, counts.delisted, counts.total, "
        "       current_row.ticker AS current_ticker, "
        "       current_row.figi AS current_figi "
        "FROM counts, current_row",
        {},
    )


# --- /api/events -------------------------------------------------------


def build_events_query(limit: int = 20) -> tuple[str, dict[str, Any]]:
    """Recent supervisor activity, derived from DB-only sources.

    We do not shell out to ``supervisorctl`` or tail logs. The
    mockup shows 6 hard-coded rows; the live version pulls from
    ``_daily_sync_health`` and ``decision_log`` (empty for now in
    production) and falls back to the most recent sync metadata.
    """
    return (
        "SELECT 'daily_sync' AS kind, "
        "       last_successful_run_at AS at, "
        "       last_run_status AS status, "
        "       (last_run_bars || ' bars updated') AS msg "
        "FROM _daily_sync_health "
        "WHERE last_successful_run_at IS NOT NULL "
        "ORDER BY last_successful_run_at DESC "
        "LIMIT %(limit)s",
        {"limit": int(limit)},
    )


# --- /api/macro --------------------------------------------------------


def build_macro_latest_query() -> tuple[str, dict[str, Any]]:
    return (
        "SELECT id, fetched_at, cbr_key_rate, usdrub_close, "
        "       usdrub_5d_prev, imoex_close, imoex_60d_prev, "
        "       regime, multiplier, sources "
        "FROM macro_regime_log "
        "ORDER BY id DESC LIMIT 1",
        {},
    )


# --- /api/settings (env-only) -----------------------------------------


#: We expose the alphard-bot loop toggles as read-only.
#: Loops are configured by env vars at supervisor start, so we
#: just read them.
def build_settings_payload() -> dict[str, Any]:
    """Return the settings payload from ``os.environ``. Read-only.

    Loop toggles are inferred from env presence — supervisors
    normally set these to ``1`` to enable, anything else to disable.
    Token presence is reported as a boolean, not the value.
    """
    import os

    def flag(name: str) -> bool:
        return os.environ.get(name, "0") not in ("", "0", "false", "False")

    return {
        "env": os.environ.get("ALPHARD_ENV", "sandbox"),
        "token_set": bool(os.environ.get("TINKOFF_INVEST_TOKEN")),
        "backfill": {
            "rate_per_min": float(os.environ.get("ALPHARD_BACKFILL_RATE_PER_MIN", "0.5")),
            "start_year": int(os.environ.get("ALPHARD_BACKFILL_START_YEAR", "2018")),
            "min_bars": int(os.environ.get("ALPHARD_BACKFILL_MIN_BARS", "1300")),
        },
        "risk": {
            "max_dd_pct": float(os.environ.get("ALPHARD_RISK_MAX_DD_PCT", "10")),
            "max_position_pct": float(os.environ.get("ALPHARD_RISK_MAX_POS_PCT", "5")),
            "max_sector_pct": float(os.environ.get("ALPHARD_RISK_MAX_SECTOR_PCT", "30")),
            "cash_floor_pct": float(os.environ.get("ALPHARD_RISK_CASH_FLOOR_PCT", "5")),
        },
        "loops": {
            "heartbeat": flag("ALPHARD_LOOP_HEARTBEAT"),
            "daily_sync": flag("ALPHARD_LOOP_DAILY_SYNC"),
            "delisted_sync": flag("ALPHARD_LOOP_DELISTED_SYNC"),
            "backup": flag("ALPHARD_LOOP_BACKUP"),
            "macro_regime": flag("ALPHARD_LOOP_MACRO_REGIME"),
        },
    }


# --- helpers ------------------------------------------------------------


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
