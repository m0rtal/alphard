"""Postgres-backed DataStore.

NOT WIRED IN TESTS
------------------
This module is ``import``-ed by the package but the tests do not
exercise it directly — Phase 1.1 CI runs on an environment without a
running Postgres. The contract is verified via ``InMemorySQLiteStore``
(see ``sqlite_store.py``).

To use locally:
    export ALPHARD_PG_DSN="host=localhost dbname=alphard user=alphard"
    psql -f src/data/schema.sql

PHASE 2 NOTES
-------------
- ``vector(384)`` column on ``news_embedding`` is reserved for pgvector.
  Phase 1.1 schema does NOT require pgvector — only Phase 3+.
- ON CONFLICT clauses use the column names from the index, not the PK
  name, so they survive PK renames in future migrations.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any

from .models import CorporateAction, OHLCVRow, TickerMeta
from .store import DataStore, StoreError

logger = logging.getLogger(__name__)

# Defensive regex for SQL identifiers (search_path schema names, table names).
# Allows only safe characters: lowercase letters, digits, underscores.
# Must start with a letter or underscore.
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*(?:\s*,\s*[a-z_][a-z0-9_]*)*$")


class PostgresDataStore(DataStore):
    """PostgreSQL implementation of the DataStore contract.

    Parameters
    ----------
    dsn:
        Standard libpq DSN. If omitted, ``$ALPHARD_PG_DSN`` is consulted.
    schema_sql_path:
        Path to the schema file. Defaults to ``schema.sql`` next to this
        module. Phase 2 will switch to a real migration framework.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        schema_sql_path: str | None = None,
        search_path: str | None = None,
    ) -> None:
        dsn = dsn or os.environ.get("ALPHARD_PG_DSN")
        if not dsn:
            raise StoreError("PostgresDataStore: no DSN — pass dsn= or set $ALPHARD_PG_DSN")
        self._dsn = dsn
        self._schema_sql_path = schema_sql_path or os.path.join(os.path.dirname(__file__), "schema.sql")  # noqa: E501
        # Optional: keep a custom search_path on every (re)connect.
        # Used by tests to isolate against an alphard_test schema.
        # Validate that search_path only contains safe identifier(s) to
        # prevent SQL injection even if it ever comes from an untrusted
        # source (test fixture, future config loader).
        if search_path is not None and not _IDENTIFIER_RE.match(search_path):
            raise ValueError(f"invalid search_path {search_path!r}: must match {_IDENTIFIER_RE.pattern}")
        self._search_path = search_path
        # Imported lazily so the rest of the package works without psycopg.
        import psycopg

        self._psycopg = psycopg
        self._conn: Any = None  # lazy connect

    # ---------------------------------------------------------- connection

    def _connect(self) -> None:
        if self._conn is None or self._conn.closed:
            # H-NETWORK-DETECT (2026-08-20): backfill PID 19 sat idle for 17
            # hours against alphard-postgres:5432 because Python held an
            # open connection without sending any new query. Two timeout
            # guards prevent this from being a silent indefinite hang:
            #
            # - connect_timeout=10: cap TCP+startup handshake so we fail
            #   fast if Postgres is unreachable (instead of OS default
            #   ~2 minutes).
            # - options="-c statement_timeout=60000": force Postgres to
            #   cancel any individual query that runs longer than 60s.
            #   This is the real deadlock-buster: a hung query on the
            #   server side now raises QueryCanceled within 60s, the
            #   connection is returned to the pool, and the next caller
            #   gets a fresh attempt. Without this, a single bad ticker
            #   stall can wedge the backfill daemon for hours.
            self._conn = self._psycopg.connect(
                self._dsn,
                autocommit=True,
                connect_timeout=10,
                options="-c statement_timeout=60000",
            )
            if self._search_path:
                with self._conn.cursor() as cur:
                    # SET search_path cannot use %s placeholders (Postgres
                    # raises SyntaxError). search_path was already validated
                    # against _IDENTIFIER_RE in __init__, so f-string is
                    # provably safe — only identifiers matching
                    # [a-z_][a-z0-9_]*(, [a-z_][a-z0-9_]*)* are accepted.
                    cur.execute(f"SET search_path TO {self._search_path}")

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def __enter__(self) -> "PostgresDataStore":
        self._connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------- auth probe
    # (Phase 1.6 H-9: detect silent auth drift after redeploy)

    def auth_probe(self, source: str = "auth_probe") -> bool:
        """Verify the bot's DB credentials are real, not just readable.

        ``pg_isready`` returns success even when scram-hashed passwords in
        pg_authid are stale (e.g. the volume was preserved across a redeploy
        that rotated POSTGRES_PASSWORD). pg_isready only checks that the
        socket is open and the process responds, not that *our* credentials
        authenticate us.

        ``auth_probe`` instead does a real round-trip:

          1. SELECT 1 -- confirms SELECT works under our role.
          2. INSERT _auth_probe ... ON CONFLICT DO UPDATE -- confirms we
             have write access to a known table.

        Both must succeed for the probe to return True. Any psycopg error
        (auth failure, connection lost, permission denied) returns False.

        This is intentionally read/write — the failure mode we are
        protecting against is "connect succeeds, reads work, writes
        silently fail or hit a permission error". A pure SELECT probe
        would miss that.

        Parameters
        ----------
        source:
            Free-form label written to _auth_probe.source. Used to
            distinguish entrypoint-smoke from backfill-pre-run from
            healthcheck in logs.

        Returns
        -------
        bool
            True if both probe statements succeeded; False on any error.

        Side effects
        ------------
        Updates one row in _auth_probe (id=1). Idempotent.
        """
        try:
            self._connect()
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO _auth_probe (id, probed_at, source)
                    VALUES (1, NOW(), %s)
                    ON CONFLICT (id) DO UPDATE
                        SET probed_at = NOW(), source = EXCLUDED.source
                    """,
                    (source,),
                )
            self._conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001 — auth probe must never raise
            # Log at WARNING (visible in default stack) but never raise —
            # callers (entrypoint smoke, backfill pre-run) decide what to do.
            logger.warning(
                "PostgresDataStore.auth_probe failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    # ---------------------------------------------------------- health sentinel

    def record_daily_sync_run(
        self,
        status: str,
        bars: int = 0,
        tickers: int = 0,
        error: str | None = None,
    ) -> None:
        """Stamp _daily_sync_health with the outcome of a daily_sync run.

        Called by daily_sync.py after every run (success or failure).
        The watchdog in src.main reads ``last_successful_run_at`` and
        triggers a container restart if it's older than the threshold.

        Parameters
        ----------
        status:
            'ok' | 'failed' | 'timeout'. Anything else is rejected by
            the CHECK constraint; we don't want free-form strings.
        bars, tickers:
            Counters from the run. NULL on failure.
        error:
            Last error message; truncated to 2000 chars to keep the
            row small. NULL on success.
        """
        self._connect()
        if error and len(error) > 2000:
            error = error[:1997] + "..."
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO _daily_sync_health
                    (id, last_successful_run_at, last_run_status,
                     last_run_bars, last_run_tickers, last_run_error, updated_at)
                VALUES (
                    1,
                    CASE WHEN %s = 'ok' THEN NOW() ELSE last_successful_run_at END,
                    %s, %s, %s, %s, NOW()
                )
                ON CONFLICT (id) DO UPDATE SET
                    last_successful_run_at = CASE
                        WHEN EXCLUDED.last_run_status = 'ok' THEN NOW()
                        ELSE _daily_sync_health.last_successful_run_at
                    END,
                    last_run_status  = EXCLUDED.last_run_status,
                    last_run_bars    = EXCLUDED.last_run_bars,
                    last_run_tickers  = EXCLUDED.last_run_tickers,
                    last_run_error    = EXCLUDED.last_run_error,
                    updated_at        = NOW()
                """,
                (status, status, bars, tickers, error),
            )
        self._conn.commit()

    def last_daily_sync_run_at(self) -> Any:
        """Return ``_daily_sync_health.last_successful_run_at`` (TIMESTAMPTZ or None).

        Used by the watchdog in src.main to detect a stuck daily_sync
        daemon (e.g. daemon thread crashed inside a live process).
        Returns None if the sentinel row has never been stamped — that
        is the legitimate pre-first-run state, and the watchdog must
        not trigger a restart on a fresh container before the first
        scheduled run has had a chance to fire.
        """
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute("SELECT last_successful_run_at FROM _daily_sync_health WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else None

    # ---------------------------------------------------------- schema

    def init_schema(self) -> None:
        self._connect()
        with open(self._schema_sql_path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        with self._conn.cursor() as cur:
            cur.execute(sql)
        self._conn.commit()

    # ---------------------------------------------------------- ticker

    def upsert_ticker(self, meta: TickerMeta) -> None:
        self.upsert_tickers([meta])

    def upsert_tickers(self, metas: list[TickerMeta]) -> None:
        if not metas:
            return
        self._connect()
        sql = """
            INSERT INTO ticker_universe
                (ticker, figi, name, lot, isin, currency, delisted,
                 delisted_at, listed_at, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker) DO UPDATE SET
                figi = EXCLUDED.figi,
                name = EXCLUDED.name,
                lot = EXCLUDED.lot,
                isin = EXCLUDED.isin,
                currency = EXCLUDED.currency,
                delisted = EXCLUDED.delisted,
                -- Preserve historical delisted_at once set (delist_source
                -- runs separately and may have populated this before any
                -- later re-sync). Only overwrite listed_at — it always
                -- reflects the most recent Tinkoff listing date.
                listed_at = EXCLUDED.listed_at,
                source = EXCLUDED.source,
                updated_at = NOW()
        """
        rows = [
            (
                m.ticker,
                m.figi,
                m.name,
                m.lot,
                m.isin,
                m.currency,
                m.delisted,
                m.delisted_at,
                m.listed_at,
                m.source,
            )
            for m in metas
        ]
        with self._conn.cursor() as cur:
            cur.executemany(sql, rows)
        self._conn.commit()

    def list_tickers(self, *, include_delisted: bool = True) -> list[TickerMeta]:
        self._connect()
        sql = (
            "SELECT ticker, figi, name, lot, isin, currency, class_code, "
            "delisted, delisted_at, listed_at, source FROM ticker_universe"
        )
        if not include_delisted:
            sql += " WHERE delisted = FALSE"
        sql += " ORDER BY ticker"
        with self._conn.cursor() as cur:
            cur.execute(sql)
            out = [_row_to_ticker(r) for r in cur.fetchall()]
        return out

    def mark_delisted(self, ticker: str, at: date, *, reason: str = "") -> None:
        # BUGFIX (H-4): autocommit=True on connect means each execute() commits
        # independently. Wrap UPDATE + INSERT in a single transaction so they
        # either both succeed or both roll back — state stays in sync.
        self._connect()
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE ticker_universe SET delisted = TRUE, delisted_at = %s, "
                    "updated_at = NOW() WHERE ticker = %s",  # noqa: E501
                    (at, ticker),
                )
                cur.execute(
                    "INSERT INTO delisting_log (ticker, delisted_at, reason, source) "
                    "VALUES (%s, %s, %s, 'manual')",  # noqa: E501
                    (ticker, at, reason),
                )

    # ---------------------------------------------------------- OHLCV

    def upsert_ohlcv(self, rows: list[OHLCVRow]) -> int:
        """Upsert OHLCV bars. PK = (ticker, ts, source). ON CONFLICT keeps existing values.

        Behaviour: writes each (ticker, ts, source) row. If the row exists, the
        existing OHLCV values are KEPT (preserves whichever source arrived
        first); updated_at is bumped.

        Phase 2.6 step 2: the third PK column ``source`` lets two writers
        (Tinkoff MD and MOEX ISS) store bars for the same (ticker, date)
        without UPSERT collision. Existing single-source callers that
        construct ``OHLCVRow`` without setting ``source`` get the model's
        default ``source='tkf'``, so the UPSERT key is identical to v1 for
        every call site that did not opt in to multi-source.
        """
        if not rows:
            return 0
        self._connect()
        sql = """
            INSERT INTO ohlcv_daily
                (ticker, ts, source, open, high, low, close, volume, adj_close,
                 updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    NOW())
            ON CONFLICT (ticker, ts, source) DO UPDATE SET
                updated_at = NOW()
        """
        params = [
            (
                r.ticker,
                r.ts,
                r.source,
                str(r.open),
                str(r.high),
                str(r.low),
                str(r.close),
                str(r.volume),
                str(r.adj_close),
            )
            for r in rows
        ]
        with self._conn.cursor() as cur:
            cur.executemany(sql, params)
        self._conn.commit()
        return len(rows)

    def backfill_with_dedup(
        self,
        new_bars: list[OHLCVRow],
        source: str = "moex",
    ) -> dict[str, int]:
        """Insert bars but ONLY if (ticker, ts) is not yet covered by ANY source.

        Used by MOEX backfill script: skip dates already covered by Tinkoff
        or any other source. Returns dict with stats: {'inserted': N, 'skipped': M}.

        Phase 2.6 step 2: dedup is now keyed on (ticker, ts) alone — same
        as before — because the cross-source gate (Phase 2.6 step 3)
        needs both series to align on the same date. If ``source = 'tkf'``
        already covered the bar, the MOEX bar is intentionally skipped
        (rather than stored alongside as a divergent point) so the dedup
        contract matches the v1 behaviour exactly.
        """
        if not new_bars:
            return {"inserted": 0, "skipped": 0}

        pairs = list({(r.ticker, r.ts) for r in new_bars})
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ticker, ts FROM ohlcv_daily
                WHERE (ticker, ts) IN ({','.join(['(%s,%s)'] * len(pairs))})
                """,
                [v for pair in pairs for v in pair],
            )
            covered = {(row[0], row[1]) for row in cur.fetchall()}

        filtered = [r for r in new_bars if (r.ticker, r.ts) not in covered]
        skipped = len(new_bars) - len(filtered)
        if filtered:
            self.upsert_ohlcv(filtered)
        return {"inserted": len(filtered), "skipped": skipped}

    def migrate_deduplicate(self) -> int:
        """One-time migration: collapse duplicate (ticker, ts, source) rows.

        The current schema (Phase 2.6 step 2) has PK (ticker, ts, source),
        so no duplicates can exist. This function is a safety net for
        legacy states where the PK was dropped or where a partial
        migration left duplicates (e.g. an older image that ran the v1
        PK shape and then had the column added without the new PK).
        It deletes duplicates keeping the row with the lowest ctid
        (effectively whichever row was inserted first).

        Returns count of rows deleted.
        """
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT ctid,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker, ts, source
                               ORDER BY ctid
                           ) AS rn
                    FROM ohlcv_daily
                ),
                to_delete AS (
                    SELECT ctid FROM ranked WHERE rn > 1
                )
                DELETE FROM ohlcv_daily
                WHERE ctid IN (SELECT ctid FROM to_delete)
            """)
            deleted = int(cur.rowcount)
        self._conn.commit()
        return deleted

    def query_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
        source: str | None = None,
    ) -> list[OHLCVRow]:
        """Read OHLCV bars for ``ticker`` in ``[start, end]``.

        Phase 2.6 step 2: pass ``source='tkf'`` to read only one source,
        or omit to read every source tag. The OHLCVRow returned will
        carry the row's ``source`` field so callers can disambiguate
        when iterating over a multi-source result set.
        """
        self._connect()
        sql = (
            "SELECT ticker, ts, source, open, high, low, close, volume, adj_close "
            "FROM ohlcv_daily WHERE ticker = %s AND ts BETWEEN %s AND %s"
        )
        params: list[Any] = [ticker.upper(), start, end]
        if source is not None:
            sql += " AND source = %s"
            params.append(source)
        sql += " ORDER BY ts, source"
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_to_ohlcv(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- OHLCV adjusted (Phase 2.5 step 2b)

    def upsert_ohlcv_adj(self, rows: list[OHLCVRow]) -> int:
        """Upsert split-adjusted OHLCV bars into ``ohlcv_daily_adj``.

        Phase 2.5 step 2b lands before Phase 2.6 step 2 (the ``source``
        column on ``ohlcv_daily``). We persist adjusted bars in a
        parallel table so the raw feed stays intact and the migration
        path stays auditable. See ``src/data/store.py`` for the ABC
        contract and PR #74 body for the merge plan.
        """
        if not rows:
            return 0
        self._connect()
        sql = """
            INSERT INTO ohlcv_daily_adj
                (ticker, ts, open, high, low, close, volume, adj_close,
                 updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    NOW())
            ON CONFLICT (ticker, ts) DO UPDATE SET
                open       = EXCLUDED.open,
                high       = EXCLUDED.high,
                low        = EXCLUDED.low,
                close      = EXCLUDED.close,
                volume     = EXCLUDED.volume,
                adj_close  = EXCLUDED.adj_close,
                updated_at = NOW()
        """
        params = [
            (
                r.ticker,
                r.ts,
                str(r.open),
                str(r.high),
                str(r.low),
                str(r.close),
                str(r.volume),
                str(r.adj_close),
            )
            for r in rows
        ]
        with self._conn.cursor() as cur:
            cur.executemany(sql, params)
        self._conn.commit()
        return len(rows)

    def query_ohlcv_adj(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[OHLCVRow]:
        self._connect()
        sql = (
            "SELECT ticker, ts, open, high, low, close, volume, adj_close "
            "FROM ohlcv_daily_adj WHERE ticker = %s AND ts BETWEEN %s AND %s"
        )
        params: list[Any] = [ticker.upper(), start, end]
        sql += " ORDER BY ts"
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_to_ohlcv(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- corp actions

    def upsert_corporate_actions(self, rows: list[CorporateAction]) -> int:
        if not rows:
            return 0
        self._connect()
        sql = """
            INSERT INTO corporate_actions
                (ticker, ts, kind, value, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker, ts, kind, source) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
        """
        params = [(r.ticker, r.ts, r.kind, str(r.value), r.source) for r in rows]
        with self._conn.cursor() as cur:
            cur.executemany(sql, params)
        self._conn.commit()
        return len(rows)

    def query_corporate_actions(self, ticker: str, start: date, end: date) -> list[CorporateAction]:
        self._connect()
        sql = (
            "SELECT ticker, ts, kind, value, source FROM corporate_actions "
            "WHERE ticker = %s AND ts BETWEEN %s AND %s ORDER BY ts"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (ticker.upper(), start, end))
            return [_row_to_action(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- diagnostics

    def count_ohlcv(self, ticker: str | None = None) -> int:
        self._connect()
        with self._conn.cursor() as cur:
            if ticker:
                cur.execute("SELECT COUNT(*) FROM ohlcv_daily WHERE ticker = %s", (ticker.upper(),))
            else:
                cur.execute("SELECT COUNT(*) FROM ohlcv_daily")
            return int(cur.fetchone()[0])

    def earliest_ts(self, ticker: str) -> date | None:
        """Earliest stored bar for ``ticker``, or None if no rows."""
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(ts) FROM ohlcv_daily WHERE ticker = %s",
                (ticker.upper(),),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else None

    def latest_ts(self, ticker: str) -> date | None:
        """Latest stored bar for ``ticker``, or None if no rows."""
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(ts) FROM ohlcv_daily WHERE ticker = %s",
                (ticker.upper(),),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else None

    def ticker_meta(self, ticker: str) -> Any:
        """Universe row for ``ticker``: ``(listed_at, delisted_at)`` or None.

        Used by the backfill to compute the earliest date we can reach
        for this ticker (delisted ticker = back to delisted_at; live
        ticker = back to listed_at or MIN_YEAR).
        """
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT listed_at, delisted_at FROM ticker_universe WHERE ticker = %s",
                (ticker.upper(),),
            )
            row = cur.fetchone()
            return row if row else None

    def mark_backfill_complete(
        self,
        ticker: str,
        complete: bool = True,
    ) -> None:
        """Flip the ``backfill_complete`` flag for ``ticker``.

        ``True`` = the data-agent has all bars it can pull for this
        ticker. ML and backtest layers filter on this flag to avoid
        silently training on partial history.

        ``False`` = explicitly re-open the ticker for retries. Used
        when a future run discovers the previously-marked-complete
        ticker has stale data (e.g. an upstream archive gap fix
        adds a year that wasn't there yesterday).
        """
        self._connect()
        with self._conn.cursor() as cur:
            if complete:
                cur.execute(
                    """
                    UPDATE ticker_universe
                       SET backfill_complete = TRUE,
                           backfill_complete_at = NOW(),
                           updated_at = NOW()
                     WHERE ticker = %s
                    """,
                    (ticker.upper(),),
                )
            else:
                cur.execute(
                    """
                    UPDATE ticker_universe
                       SET backfill_complete = FALSE,
                           backfill_complete_at = NULL,
                           updated_at = NOW()
                     WHERE ticker = %s
                    """,
                    (ticker.upper(),),
                )
            self._conn.commit()

    def sync_universe_delisted(
        self,
        delisted_dates: dict[str, tuple["date | None", "date | None"]],
    ) -> int:
        """Bulk-update ``listed_at`` and ``delisted_at`` from MOEX ISS.

        Uses a single multi-row UPSERT so a universe-wide sync is one
        round trip. Returns the number of rows written. Tickers that
        don't appear in ``delisted_dates`` are left alone.

        Idempotent: re-running with the same dict is a no-op.

        Issue #150: the previous SQL used ``COALESCE(%s, listed_at)``
        for ``listed_at`` but a bare ``%s`` for ``delisted_at``. When
        ``fetch_delist_dates()`` returns ``(None, None)`` for a ticker
        (transient ISS outage, network blip, or a board whose
        ``listed_from``/``listed_till`` attributes are absent), the
        sync would OVERWRITE a previously-stored ``delisted_at`` with
        NULL — silently regressing a known delisted ticker to "active".
        The age-aware backfill completion formula
        (``expected_bars = trading_days(listed_at, today|delisted_at) * (1 - halts_pct)``)
        then computed the wrong denominator, the ticker stayed in
        "partial" forever, and ML/training layers filtered it out.

        Fix: wrap ``delisted_at`` in ``COALESCE`` too, matching the
        ``listed_at`` semantics. A None upstream now means "keep
        whatever we had on disk" — symmetric with listed_at. A new
        non-None date still overwrites (the canonical "I just learned
        this delisted today" path). To explicitly NULL a delisted_at,
        callers must use a separate maintenance path (none exists
        today — by design; once a ticker is delisted it stays
        delisted in our universe).
        """

        rows: list[tuple[str, object, object]] = []
        for ticker, (listed_at, delisted_at) in delisted_dates.items():
            # Map NULL dates to NULL in SQL (not None sentinel).
            rows.append(
                (
                    ticker.upper(),
                    listed_at,  # may be None → COALESCE keeps existing
                    delisted_at,  # may be None → COALESCE keeps existing
                )
            )
        if not rows:
            return 0
        self._connect()
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE ticker_universe
                   SET listed_at   = COALESCE(%s, listed_at),
                       delisted_at = COALESCE(%s, delisted_at),
                       updated_at  = NOW()
                 WHERE ticker = %s
                """,
                [(la, da, t) for t, la, da in rows],
            )
            self._conn.commit()
            return int(cur.rowcount)

    def backfill_complete_tickers(self) -> list[str]:
        """Universe tickers marked backfill_complete = TRUE. Useful for
        ML feature builders: ``SELECT * FROM ohlcv_daily WHERE ticker IN
        (...)`` to skip the partial-history ones.
        """
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute("SELECT ticker FROM ticker_universe WHERE backfill_complete = TRUE ORDER BY ticker")
            return [r[0] for r in cur.fetchall()]

    def is_backfill_complete(self, ticker: str) -> bool:
        """Check the flag without reading all bars. Cheap, no aggregates."""
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT backfill_complete FROM ticker_universe WHERE ticker = %s",
                (ticker.upper(),),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False


def _row_to_ticker(r: Any) -> TickerMeta:
    # Issue #104: when len(r) <= 10 we are looking at a legacy v1 result
    # without the source column. The previous fallback `source=r[9]` aliased
    # listed_at (a date) into the SourceType Literal slot, which fails
    # pydantic validation downstream. Default to "tkf" — the only writer
    # before Phase 2.6.
    return TickerMeta(
        ticker=r[0],
        figi=r[1],
        name=r[2],
        lot=int(r[3]),
        isin=r[4],
        currency=r[5] or "RUB",
        class_code=r[6] if len(r) > 10 else None,
        delisted=bool(r[7] if len(r) > 7 else False),
        delisted_at=r[8] if len(r) > 8 else None,
        listed_at=r[9] if len(r) > 9 else None,
        source=(r[10] if len(r) > 10 else "tkf"),
    )


def _row_to_ohlcv(r: Any) -> OHLCVRow:
    from decimal import Decimal

    # Phase 2.6 step 2: read the new ``source`` column. The shape is:
    #   * 9 columns: v2 schema — source is at index 2.
    #   * 8 columns: v1 fixture (legacy code path that SELECTs without
    #     the source column) — fall back to source='tkf' for
    #     backward-compat. The 8-column path is defensive only — every
    #     real SELECT in production goes through query_ohlcv() which
    #     uses the v2 projection.
    if len(r) > 8:
        source = r[2]
        open_v, high_v, low_v, close_v, volume_v, adj_close_v = r[3], r[4], r[5], r[6], r[7], r[8]
    else:
        source = "tkf"
        open_v, high_v, low_v, close_v, volume_v, adj_close_v = r[2], r[3], r[4], r[5], r[6], r[7]
    return OHLCVRow(
        ticker=r[0],
        ts=r[1],
        source=source,
        open=Decimal(str(open_v)),
        high=Decimal(str(high_v)),
        low=Decimal(str(low_v)),
        close=Decimal(str(close_v)),
        volume=Decimal(str(volume_v)),
        adj_close=Decimal(str(adj_close_v)),
    )


def _row_to_action(r: Any) -> CorporateAction:
    from decimal import Decimal

    return CorporateAction(
        ticker=r[0],
        ts=r[1],
        kind=r[2],
        value=Decimal(str(r[3])),
        source=r[4],
    )


__all__ = ["PostgresDataStore"]
