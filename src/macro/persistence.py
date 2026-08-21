"""Persistence helpers for the Macro Agent regime log (Phase 2.3).

We deliberately do NOT extend the ``DataStore`` ABC: the regime log is a
flat audit trail keyed by ``fetched_at``, not part of the per-ticker
OHLCV contract. Adding methods to ``DataStore`` would couple every
caller (PG, SQLite, test fakes) to a feature that only the Macro Agent
uses. Instead, ``persistence.py`` exposes thin helpers that take any
DB-API 2.0 connection (``sqlite3.Connection`` or ``psycopg2.extensions.connection``)
and dispatch the SQL based on the connection's dialect.

Why store-agnostic SQL in one module?
- The query patterns are tiny (one UPSERT, one SELECT) and don't
  benefit from the DataStore abstraction's typing.
- Tests pass an ``InMemorySQLiteStore``'s private ``_conn`` directly,
  production passes a psycopg2 connection; the SQL is symmetric.

Idempotency:
- ``upsert_regime`` ON CONFLICT(fetched_at) DO UPDATE — same fetched_at
  always rewrites the row. This is intentional: re-runs within the
  hourly window should refresh stale numbers, not append duplicates.
- A unique constraint on ``fetched_at`` ensures a sub-second retry does
  not produce a second row.

Both SELECT projections use this column order:
    0 fetched_at | 1 cbr_key_rate | 2 usdrub_close | 3 usdrub_5d_prev
    4 imoex_close | 5 imoex_60d_prev | 6 regime | 7 multiplier | 8 sources
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional

from .models import MacroRegime, MacroSnapshot

# ---------------------------------------------------------------------------
# SQL — kept here so production + tests share one definition.
# ---------------------------------------------------------------------------

_PG_UPSERT_SQL = """
INSERT INTO macro_regime_log
        (fetched_at, cbr_key_rate, usdrub_close, usdrub_5d_prev,
         imoex_close, imoex_60d_prev, regime, multiplier, sources)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (fetched_at) DO UPDATE SET
    cbr_key_rate   = EXCLUDED.cbr_key_rate,
    usdrub_close   = EXCLUDED.usdrub_close,
    usdrub_5d_prev = EXCLUDED.usdrub_5d_prev,
    imoex_close    = EXCLUDED.imoex_close,
    imoex_60d_prev = EXCLUDED.imoex_60d_prev,
    regime         = EXCLUDED.regime,
    multiplier     = EXCLUDED.multiplier,
    sources        = EXCLUDED.sources
"""

_SQLITE_UPSERT_SQL = """
INSERT INTO macro_regime_log
        (fetched_at, cbr_key_rate, usdrub_close, usdrub_5d_prev,
         imoex_close, imoex_60d_prev, regime, multiplier, sources)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (fetched_at) DO UPDATE SET
    cbr_key_rate   = excluded.cbr_key_rate,
    usdrub_close   = excluded.usdrub_close,
    usdrub_5d_prev = excluded.usdrub_5d_prev,
    imoex_close    = excluded.imoex_close,
    imoex_60d_prev = excluded.imoex_60d_prev,
    regime         = excluded.regime,
    multiplier     = excluded.multiplier,
    sources        = excluded.sources
"""

_SELECT_LATEST_SQL = """
SELECT fetched_at, cbr_key_rate, usdrub_close, usdrub_5d_prev,
       imoex_close, imoex_60d_prev, regime, multiplier, sources
FROM macro_regime_log
ORDER BY fetched_at DESC
LIMIT 1
"""


def _is_postgres(conn: Any) -> bool:
    """Detect psycopg2 connections vs sqlite3 (duck-typed).

    We don't import psycopg2 at module level — tests / local-dev run
    without it. ``psycopg2.extensions.connection`` exposes ``.cursor()``
    but NOT ``.execute``. SQLite's ``Connection`` exposes both. The
    absence of ``.execute`` is the discriminator.
    """
    return not hasattr(conn, "execute")


def _decimal_str(v: Decimal | float | int) -> str:
    return str(Decimal(str(v)))


def _parse_sources(raw: Any) -> dict[str, str]:
    """Decode the ``sources`` JSONB/TEXT column into a dict."""
    if isinstance(raw, Mapping):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    if raw is None:
        return {}
    return {}


def _coerce_decimal(v: Any) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def upsert_regime(conn: Any, regime: MacroRegime) -> None:
    """Upsert one ``MacroRegime`` row.

    ``conn`` is either a ``sqlite3.Connection`` or a ``psycopg2``
    connection. The regime's ``snapshot`` must be set — we store its
    raw numbers, not the label alone.
    """
    if regime.snapshot is None:
        raise ValueError("upsert_regime requires regime.snapshot to be set")

    snap: MacroSnapshot = regime.snapshot
    params = (
        snap.fetched_at,
        _decimal_str(snap.cbr_key_rate),
        _decimal_str(snap.usdrub_close),
        _decimal_str(snap.usdrub_5d_prev),
        _decimal_str(snap.imoex_close),
        _decimal_str(snap.imoex_60d_prev),
        regime.regime,
        _decimal_str(regime.multiplier),
        json.dumps(dict(snap.sources), sort_keys=True, default=str),
    )

    if _is_postgres(conn):
        cur = conn.cursor()
        try:
            cur.execute(_PG_UPSERT_SQL, params)
            conn.commit()
        finally:
            cur.close()
    else:
        cur = conn.execute(_SQLITE_UPSERT_SQL, params)
        conn.commit()
        cur.close()


def latest_regime(conn: Any) -> Optional[MacroRegime]:
    """Return the most-recent ``MacroRegime`` row, or None if empty."""
    if _is_postgres(conn):
        cur = conn.cursor()
        try:
            cur.execute(_SELECT_LATEST_SQL)
            row = cur.fetchone()
        finally:
            cur.close()
    else:
        cur = conn.execute(_SELECT_LATEST_SQL)
        row = cur.fetchone()
        cur.close()

    if row is None:
        return None

    fetched_at = row[0]
    if isinstance(fetched_at, str):
        fetched_at = datetime.fromisoformat(fetched_at)

    snap = MacroSnapshot(
        fetched_at=fetched_at,
        cbr_key_rate=_coerce_decimal(row[1]),
        usdrub_close=_coerce_decimal(row[2]),
        usdrub_5d_prev=_coerce_decimal(row[3]),
        imoex_close=_coerce_decimal(row[4]),
        imoex_60d_prev=_coerce_decimal(row[5]),
        sources=_parse_sources(row[8]),
    )
    return MacroRegime(
        regime=row[6],
        multiplier=_coerce_decimal(row[7]),
        reason="loaded from persistence",
        snapshot=snap,
    )


__all__ = ["upsert_regime", "latest_regime"]
