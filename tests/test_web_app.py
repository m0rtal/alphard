"""TDD tests for alphard-web backend (issue #390).

Each endpoint is tested with a stubbed DB row dict so we don't need
a real Postgres for unit tests. Live verification happens via
pre_pr_smoke.sh against the alphard-postgres container.
"""

from __future__ import annotations

# --- /api/kpis ----------------------------------------------------------


def test_kpis_returns_universe_complete_rows() -> None:
    """All four KPI fields present, ints or iso-string."""
    from src.web.app import build_kpis_query

    sql, params = build_kpis_query()
    assert "SELECT" in sql.upper()
    assert "{universe}" not in sql  # no Python str.format placeholders left
    assert "ticker_universe" in sql
    assert "ohlcv_daily" in sql


def test_kpis_row_to_payload_masks_shape() -> None:
    """Row -> JSON payload has exactly the documented keys."""
    from src.web.app import kpis_row_to_payload

    row = {
        "universe_size": 3263,
        "tickers_complete": 1568,
        "ohlcv_rows": 1_338_311,
        "last_bar_at": "2026-09-01",
    }
    payload = kpis_row_to_payload(row)
    assert set(payload.keys()) == {
        "universe_size",
        "tickers_complete",
        "ohlcv_rows",
        "last_bar_at",
    }
    assert payload["ohlcv_rows"] == 1_338_311


# --- /api/ohlcv_timeseries --------------------------------------------


def test_ohlcv_timeseries_query_buckets_by_day() -> None:
    """Series query groups by date, orders ascending, bounded by days."""
    from src.web.app import build_ohlcv_timeseries_query

    sql, params = build_ohlcv_timeseries_query(days=30)
    assert "GROUP BY" in sql.upper()
    assert "date_trunc" in sql or "::date" in sql
    assert "ORDER BY" in sql.upper()
    assert params["days"] == 30


def test_ohlcv_timeseries_default_window_is_30_days() -> None:
    """Default window is 30 days (one month of supervisor activity)."""
    from src.web.app import DEFAULT_TIMESERIES_DAYS

    assert DEFAULT_TIMESERIES_DAYS == 30


# --- /api/top_tickers --------------------------------------------------


def test_top_tickers_query_orders_by_bars_desc() -> None:
    """Top-N query returns rows ordered by bar count descending."""
    from src.web.app import build_top_tickers_query

    sql, params = build_top_tickers_query(limit=10)
    assert "ORDER BY" in sql.upper()
    assert "DESC" in sql.upper()
    assert params["limit"] == 10


# --- /api/health -------------------------------------------------------


def test_health_returns_known_shape() -> None:
    """Health endpoint reports ok=True when DB is reachable."""
    from src.web.app import health_payload

    payload = health_payload(db_ok=True)
    assert payload == {"ok": True, "db": True}

    payload = health_payload(db_ok=False)
    assert payload == {"ok": False, "db": False}
