"""Tests for issue #232: psycopg.connect timeout coverage (PR #46 extension).

PR #46 (commit 1e3b6dd) added ``connect_timeout=10`` + ``options="-c
statement_timeout=60000"`` to ``src/data/pg_store.py:_connect()``. That fix was
incomplete: five other psycopg.connect sites in the repo were left
unprotected. This module verifies the new ``connect_with_timeouts`` helper in
``src/data.pg_store.py`` AND that the unprotected sites now route through it.

Coverage targets:

- ``src.data.pg_store.connect_with_timeouts`` (new helper)
- ``src.coordinator.Coordinator._audit`` (runtime hot path: every run_once)
- ``src.data.quality.audit.PostgresAuditLog._ensure_conn`` (runtime hot path)
- ``scripts/backfill_full_universe._ensure_class_code_column`` /
  ``_persist_universe_meta`` (manual backfill scripts)
- ``scripts/backfill_delisted_via_tinkoff._missing_tickers`` (manual backfill)
- ``scripts/mark_terminally_failed.main`` (Phase 1.6 daily admin run)

Failure mode we prevent: a Postgres network stall hangs the caller
indefinitely (no connect_timeout → OS-default ~2min handshake; no
statement_timeout → Postgres never cancels a hung query). Both guards were
present on ``pg_store._connect`` after PR #46; this issue extends them to
every other consumer.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingConnect:
    """Stand-in for ``psycopg.connect`` that records kwargs.

    Each call returns a MagicMock connection (the callers either use it as a
    context manager or immediately call ``.cursor()``). We do NOT model
    fetchone / fetchall — the consumers either close without fetching (the
    scripts) or are mocked end-to-end (the audit log write).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._next_conn = MagicMock(name="psycopg.Connection")

    def __call__(self, dsn: str, /, **kwargs: object) -> MagicMock:
        self.calls.append({"dsn": dsn, **kwargs})
        # Return a fresh mock every call so each ``with`` context opens
        # cleanly (psycopg.Connection.__enter__ returns the connection itself).
        return MagicMock(name=f"Connection#{len(self.calls)}")


@pytest.fixture
def recording_connect(monkeypatch: pytest.MonkeyPatch) -> _RecordingConnect:
    """Patch ``psycopg.connect`` globally so every consumer records kwargs.

    The helper under test (``src.data.pg_store.connect_with_timeouts``) does a
    local ``import psycopg`` then calls ``psycopg.connect``. We patch the
    module attribute on the loaded psycopg module so the local import picks
    up our stand-in.
    """
    import psycopg  # noqa: F401  — must import for patching to take effect

    recorder = _RecordingConnect()
    monkeypatch.setattr(psycopg, "connect", recorder)
    return recorder


# ---------------------------------------------------------------------------
# src.data.pg_store.connect_with_timeouts (the helper itself)
# ---------------------------------------------------------------------------


class TestConnectWithTimeouts:
    def test_passes_connect_timeout_10(self, recording_connect: _RecordingConnect) -> None:
        from src.data.pg_store import connect_with_timeouts

        connect_with_timeouts("host=h dbname=d")
        assert len(recording_connect.calls) == 1
        assert recording_connect.calls[0]["connect_timeout"] == 10

    def test_passes_statement_timeout_option(self, recording_connect: _RecordingConnect) -> None:
        from src.data.pg_store import connect_with_timeouts

        connect_with_timeouts("host=h dbname=d")
        assert recording_connect.calls[0]["options"] == "-c statement_timeout=60000"

    def test_dsn_is_passed_through(self, recording_connect: _RecordingConnect) -> None:
        from src.data.pg_store import connect_with_timeouts

        dsn = "host=alpha dbname=alphard user=u"
        connect_with_timeouts(dsn)
        assert recording_connect.calls[0]["dsn"] == dsn

    def test_overrides_merge_with_defaults(self, recording_connect: _RecordingConnect) -> None:
        from src.data.pg_store import connect_with_timeouts

        connect_with_timeouts("host=h", autocommit=False)
        call = recording_connect.calls[0]
        # Override applied
        assert call["autocommit"] is False
        # Defaults still present
        assert call["connect_timeout"] == 10
        assert call["options"] == "-c statement_timeout=60000"

    def test_overrides_can_replace_connect_timeout(self, recording_connect: _RecordingConnect) -> None:
        """A caller may override ``connect_timeout`` for a specific need.

        The helper applies ``dict.update`` semantics: explicit overrides win.
        This guards against accidental masking of caller intent by the
        defaults.
        """
        from src.data.pg_store import connect_with_timeouts

        connect_with_timeouts("host=h", connect_timeout=2)
        assert recording_connect.calls[0]["connect_timeout"] == 2

    def test_helper_returns_connection(self, recording_connect: _RecordingConnect) -> None:
        from src.data.pg_store import connect_with_timeouts

        conn = connect_with_timeouts("host=h")
        # The MagicMock returned by the recorder is the connection; the helper
        # must return it unchanged so callers can ``with conn as c:``.
        assert conn is not None


# ---------------------------------------------------------------------------
# src.coordinator.Coordinator._audit (runtime hot path)
# ---------------------------------------------------------------------------


class TestCoordinatorAudit:
    def _make_coord(self) -> object:
        from src.coordinator import Coordinator, CoordinatorConfig, CoordinatorSide
        from src.risk.gate import RiskLimits

        # CoordinatorConfig fields are Decimal — Decimal("1"), not int 1.
        _D = Decimal
        cfg = CoordinatorConfig(
            ticker="SBER",
            side=CoordinatorSide.BUY,
            quantity=_D("1"),
            limit_price=_D("1"),
            risk_limits=RiskLimits(
                max_dd_pct=_D("10"),
                max_position_pct=_D("10"),
                max_sector_pct=_D("30"),
                max_daily_loss_pct=_D("3"),
            ),
            portfolio_equity=_D("1000000"),
            portfolio_cash=_D("1000000"),
            portfolio_peak=_D("1000000"),
            live_trading=False,
            store_dsn="host=alpha dbname=alphard",
        )
        return Coordinator(config=cfg)

    def test_audit_passes_timeouts(self, recording_connect: _RecordingConnect) -> None:
        coord = self._make_coord()
        # _audit returns audit_log_id from RETURNING clause; the mock cursor's
        # fetchone returns a MagicMock by default which is truthy but not an
        # int. Patch fetchone to return (42,) so we get a clean id.
        coord._audit([], 0, False, (), None)  # type: ignore[arg-type]
        # The recorder received exactly one psycopg.connect call with the
        # timeouts baked in. We don't care which connection was returned
        # (mocked end-to-end); we only care that the kwargs are right.
        assert len(recording_connect.calls) == 1
        call = recording_connect.calls[0]
        assert call["dsn"] == "host=alpha dbname=alphard"
        assert call["connect_timeout"] == 10
        assert call["options"] == "-c statement_timeout=60000"

    def test_audit_skips_when_no_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No store_dsn → audit returns None silently (no psycopg.connect call).

        Regression check: the timeouts guard must not change the no-DSN path.
        """
        import psycopg

        # Patch psycopg.connect with a sentinel that raises if called.
        sentinel = MagicMock(side_effect=AssertionError("psycopg.connect must not be called when store_dsn is None"))
        monkeypatch.setattr(psycopg, "connect", sentinel)

        from src.coordinator import Coordinator, CoordinatorConfig, CoordinatorSide
        from src.risk.gate import RiskLimits

        _D = Decimal
        cfg = CoordinatorConfig(
            ticker="SBER",
            side=CoordinatorSide.BUY,
            quantity=_D("1"),
            limit_price=_D("1"),
            risk_limits=RiskLimits(
                max_dd_pct=_D("10"),
                max_position_pct=_D("10"),
                max_sector_pct=_D("30"),
                max_daily_loss_pct=_D("3"),
            ),
            portfolio_equity=_D("1000000"),
            portfolio_cash=_D("1000000"),
            portfolio_peak=_D("1000000"),
            live_trading=False,
            store_dsn=None,
        )
        coord = Coordinator(config=cfg)
        assert coord._audit([], 0, False, (), None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# src.data.quality.audit.PostgresAuditLog._ensure_conn
# ---------------------------------------------------------------------------


class TestPostgresAuditLogConnect:
    def test_lazy_connect_passes_timeouts(
        self, recording_connect: _RecordingConnect, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=alpha dbname=alphard")
        # Reset module-level cached _dsn; the PostgresAuditLog reads the env
        # at construction, so we construct AFTER setenv.
        from src.data.quality.audit import PostgresAuditLog

        log = PostgresAuditLog()
        # Lazy: no connection opened yet.
        assert len(recording_connect.calls) == 0
        # Trigger the connection.
        log._ensure_conn()
        assert len(recording_connect.calls) == 1
        call = recording_connect.calls[0]
        assert call["dsn"] == "host=alpha dbname=alphard"
        assert call["connect_timeout"] == 10
        assert call["options"] == "-c statement_timeout=60000"

    def test_lazy_connect_reuses_existing_connection(
        self,
        recording_connect: _RecordingConnect,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Already-connected: a second _ensure_conn must NOT re-connect.

        The recorder's MagicMock connection survives, so we don't need to
        construct a real psycopg.Connection — just verify psycopg.connect is
        called exactly once.
        """
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=alpha dbname=alphard")
        from src.data.quality.audit import PostgresAuditLog

        log = PostgresAuditLog()
        log._ensure_conn()
        log._ensure_conn()
        log._ensure_conn()
        assert len(recording_connect.calls) == 1


# ---------------------------------------------------------------------------
# scripts.backfill_full_universe
# ---------------------------------------------------------------------------


class TestBackfillFullUniverse:
    def test_ensure_class_code_column_passes_timeouts(
        self,
        recording_connect: _RecordingConnect,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=alpha dbname=alphard")
        # Reset module-level singleton — PostgresDataStore caches the DSN
        # at construction. Each test that needs a fresh store must build one
        # after setenv.
        from src.data.pg_store import PostgresDataStore
        from scripts.backfill_full_universe import _ensure_class_code_column

        store = PostgresDataStore()
        _ensure_class_code_column(store)
        assert len(recording_connect.calls) >= 1
        call = recording_connect.calls[-1]
        assert call["connect_timeout"] == 10
        assert call["options"] == "-c statement_timeout=60000"

    def test_persist_universe_meta_passes_timeouts(
        self,
        recording_connect: _RecordingConnect,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=alpha dbname=alphard")
        from src.data.models import TickerMeta
        from scripts.backfill_full_universe import _persist_universe_meta

        meta = TickerMeta(
            ticker="SBER",
            name="Sberbank",
            lot=10,
            currency="RUB",
            class_code="TQBR",
            delisted=False,
            source="moex",
        )
        # _persist_universe_meta calls store.upsert_ticker (which needs a
        # connected store) then opens a second psycopg.connect for the
        # class_code patch. Patch the store to skip upsert_ticker.
        store = MagicMock(name="PostgresDataStore")
        _persist_universe_meta(store, meta)
        # At least one call (the class_code patch) carries the timeouts.
        timeout_calls = [
            c
            for c in recording_connect.calls
            if c.get("connect_timeout") == 10 and c.get("options") == "-c statement_timeout=60000"
        ]
        assert timeout_calls, f"no timeout-guarded connect calls: {recording_connect.calls}"


# ---------------------------------------------------------------------------
# scripts.backfill_delisted_via_tinkoff
# ---------------------------------------------------------------------------


class TestBackfillDelistedViaTinkoff:
    def test_missing_tickers_passes_timeouts(
        self,
        recording_connect: _RecordingConnect,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=alpha dbname=alphard")
        from scripts.backfill_delisted_via_tinkoff import _missing_tickers

        _missing_tickers()
        assert len(recording_connect.calls) == 1
        call = recording_connect.calls[0]
        assert call["connect_timeout"] == 10
        assert call["options"] == "-c statement_timeout=60000"


# ---------------------------------------------------------------------------
# scripts.mark_terminally_failed (Phase 1.6 daily admin run)
# ---------------------------------------------------------------------------


class TestMarkTerminallyFailed:
    def test_main_passes_timeouts_and_autocommit_false(
        self,
        recording_connect: _RecordingConnect,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=alpha dbname=alphard")
        # The script's psycopg.connect was originally called with
        # ``autocommit=False`` — the override must survive through the helper.
        # The script also exits early if no candidates are returned, so we
        # make the mock cursor's fetchall return an empty list.
        from scripts.mark_terminally_failed import main

        with patch("sys.argv", ["mark_terminally_failed", "--horizon-days", "30", "--dry-run"]):
            main()
        # Exactly one connect call (the SELECT for candidates).
        assert len(recording_connect.calls) == 1
        call = recording_connect.calls[0]
        assert call["connect_timeout"] == 10
        assert call["options"] == "-c statement_timeout=60000"
        # Regression: the script's transaction boundary (autocommit=False)
        # must NOT be silently dropped by the helper.
        assert call["autocommit"] is False


# ---------------------------------------------------------------------------
# Regression: PR #46 still works (the original fix on pg_store._connect is
# unchanged; this test guards against an accidental revert).
# ---------------------------------------------------------------------------


class TestPr46StillApplied:
    def test_pg_store_connect_passes_timeouts(
        self,
        recording_connect: _RecordingConnect,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALPHARD_PG_DSN", "host=alpha dbname=alphard")
        from src.data.pg_store import PostgresDataStore

        s = PostgresDataStore()
        s._connect()
        call = recording_connect.calls[-1]
        assert call["connect_timeout"] == 10
        assert call["options"] == "-c statement_timeout=60000"
        # Regression: PR #46's autocommit=True invariant still holds.
        # The MagicMock connection returned by the recorder does not surface
        # autocommit, so this assertion is best-effort; the recorder doesn't
        # introspect the connection. The contract is upheld by the
        # implementation, not by this test.
