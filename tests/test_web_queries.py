"""TDD tests for alphard-web v2 query builders (issue #393).

Pure-function unit tests — no DB connection required.
"""

from __future__ import annotations

# --- /api/summary ------------------------------------------------------


def test_summary_query_returns_expected_columns() -> None:
    from src.web.queries import build_summary_query, summary_row_to_payload

    sql, params = build_summary_query()
    assert "ticker_universe" in sql
    assert "ohlcv_daily" in sql
    assert "macro_regime_log" in sql
    assert "regime" in sql
    assert params == {}

    # Empty/missing rows must serialise to None / 0, not KeyError.
    payload = summary_row_to_payload(
        {
            "universe_size": 100,
            "backfill_done": 50,
            "daily_sync_bars": 12345,
            "daily_sync_at": None,
            "regime": None,
            "regime_multiplier": None,
            "cbr_key_rate": None,
            "usdrub_close": None,
            "imoex_close": None,
        }
    )
    assert payload["universe_size"] == 100
    assert payload["backfill_done"] == 50
    assert payload["backfill_pct"] == 50.0
    assert payload["daily_sync_bars"] == 12345
    assert payload["regime"] is None


def test_summary_pct_handles_zero_universe() -> None:
    from src.web.queries import summary_row_to_payload

    payload = summary_row_to_payload(
        {
            "universe_size": 0,
            "backfill_done": 0,
            "daily_sync_bars": 0,
            "daily_sync_at": None,
            "regime": None,
            "regime_multiplier": None,
            "cbr_key_rate": None,
            "usdrub_close": None,
            "imoex_close": None,
        }
    )
    assert payload["backfill_pct"] == 0.0  # not ZeroDivisionError


# --- /api/sparkline ----------------------------------------------------


def test_sparkline_tickers_query_groups_by_day() -> None:
    from src.web.queries import build_sparkline_tickers_query

    sql, params = build_sparkline_tickers_query(days=7)
    assert "date_trunc('day'" in sql
    assert "backfill_complete" in sql
    assert "GROUP BY" in sql
    assert params == {"days": 7}


def test_sparkline_bars_query_filters_by_window() -> None:
    from src.web.queries import build_sparkline_bars_query

    sql, params = build_sparkline_bars_query(days=14)
    assert "ohlcv_daily" in sql
    assert "GROUP BY" in sql
    assert params == {"days": 14}


# --- /api/tickers ------------------------------------------------------


def test_tickers_list_query_no_filters() -> None:
    from src.web.queries import build_tickers_list_query

    sql, params = build_tickers_list_query(limit=10, offset=0)
    assert "ticker_universe t" in sql
    assert "LEFT JOIN" in sql
    assert "bar_count" in sql
    assert "LIMIT %(limit)s OFFSET %(offset)s" in sql
    assert params["limit"] == 10
    assert params["offset"] == 0


def test_tickers_list_query_q_filter() -> None:
    from src.web.queries import build_tickers_list_query

    sql, params = build_tickers_list_query(q="SBER")
    assert "ILIKE %(q)s" in sql
    assert params["q"] == "%SBER%"


def test_tickers_list_query_status_done() -> None:
    from src.web.queries import build_tickers_list_query

    sql, _ = build_tickers_list_query(status="done")
    assert "backfill_complete = TRUE" in sql


def test_tickers_list_query_status_no_data() -> None:
    """no-data = backfill_complete=False AND no ohlcv_daily rows for ticker.

    We accept either a ``COALESCE(cnt, 0) = 0`` pattern (LEFT JOIN
    aggregate) or an explicit ``NOT EXISTS`` — both are correct.
    The COALESCE form is what the live implementation uses because
    it joins the per-ticker aggregate already in the SELECT list,
    so the planner only materialises one subquery.
    """
    from src.web.queries import build_tickers_list_query

    sql, _ = build_tickers_list_query(status="no-data")
    assert "backfill_complete = FALSE" in sql
    assert "NOT EXISTS" in sql or "COALESCE(bar_count.cnt, 0) = 0" in sql


def test_tickers_count_query_mirrors_filters() -> None:
    """The count query must apply the same filters as the list query."""
    from src.web.queries import build_tickers_count_query, build_tickers_list_query

    list_sql, _ = build_tickers_list_query(q="GAZP", status="delisted")
    cnt_sql, _ = build_tickers_count_query(q="GAZP", status="delisted")
    # Both must reference GAZP filter and delisted filter.
    assert "ILIKE %(q)s" in cnt_sql
    assert "GAZP" not in cnt_sql  # sanitised via params
    assert "delisted = TRUE" in cnt_sql


# --- /api/ticker/<symbol> --------------------------------------------


def test_ticker_detail_query_targets_specific_ticker() -> None:
    from src.web.queries import build_ticker_detail_query

    sql, params = build_ticker_detail_query("SBER")
    assert "WHERE t.ticker = %(ticker)s" in sql
    assert "bar_count" in sql
    assert params == {"ticker": "SBER"}


def test_ticker_recent_bars_query_orders_desc() -> None:
    from src.web.queries import build_ticker_recent_bars_query

    sql, params = build_ticker_recent_bars_query("SBER", limit=5)
    assert "ORDER BY ts DESC" in sql
    assert "LIMIT %(limit)s" in sql
    assert params == {"ticker": "SBER", "limit": 5}


# --- /api/backfill -----------------------------------------------------


def test_backfill_summary_uses_count_filter() -> None:
    from src.web.queries import build_backfill_summary_query

    sql, params = build_backfill_summary_query()
    assert "COUNT(*) FILTER" in sql
    assert "ticker_universe" in sql
    assert "current_row" in sql
    assert params == {}


# --- /api/events -------------------------------------------------------


def test_events_query_pulls_from_daily_sync_health() -> None:
    from src.web.queries import build_events_query

    sql, params = build_events_query(limit=10)
    assert "_daily_sync_health" in sql
    assert "last_successful_run_at" in sql
    assert params == {"limit": 10}


# --- /api/macro --------------------------------------------------------


def test_macro_latest_orders_by_id_desc() -> None:
    from src.web.queries import build_macro_latest_query

    sql, params = build_macro_latest_query()
    assert "macro_regime_log" in sql
    assert "ORDER BY id DESC" in sql
    assert params == {}


# --- /api/settings (env-driven) ---------------------------------------


def test_settings_payload_shape_is_stable() -> None:
    """Settings payload keys are stable contract; future PRs add fields."""
    import os

    os.environ["ALPHARD_ENV"] = "sandbox"
    os.environ["TINKOFF_INVEST_TOKEN"] = "dummy"  # noqa: S105 — test only
    os.environ["ALPHARD_LOOP_HEARTBEAT"] = "1"
    os.environ["ALPHARD_LOOP_DAILY_SYNC"] = "1"

    from src.web.queries import build_settings_payload

    payload = build_settings_payload()
    assert payload["env"] == "sandbox"
    assert payload["token_set"] is True
    assert "backfill" in payload
    assert "risk" in payload
    assert "loops" in payload
    assert payload["loops"]["heartbeat"] is True
    assert payload["loops"]["daily_sync"] is True


def test_settings_loop_flag_understands_truthy_values() -> None:
    """Loops are 'on' if the env var is anything other than 0/false/empty."""
    import os

    os.environ["ALPHARD_LOOP_BACKUP"] = "1"
    from src.web.queries import build_settings_payload

    assert build_settings_payload()["loops"]["backup"] is True

    os.environ["ALPHARD_LOOP_BACKUP"] = "0"
    assert build_settings_payload()["loops"]["backup"] is False

    os.environ["ALPHARD_LOOP_BACKUP"] = ""
    assert build_settings_payload()["loops"]["backup"] is False

    # Cleanup
    del os.environ["ALPHARD_LOOP_BACKUP"]
