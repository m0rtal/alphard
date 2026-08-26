"""Tests for src/main.py _universe_metrics_loop (Phase 2.8 step 2).

The loop:
- Refreshes two Prometheus gauges via the in-process ``_metrics_registry``.
- Runs two ``SELECT COUNT(*)`` queries against ``ticker_universe``
  (one for the total universe size, one for the backfill_complete subset).
- Skips work entirely when ``$ALPHARD_PG_DSN`` is unset.
- Survives ``psycopg.Error`` and generic ``Exception`` without dying.

We mock ``psycopg.connect`` by injecting a fake module into ``sys.modules``
BEFORE importing ``main``. The loop does ``import psycopg`` lazily inside
its body — if psycopg is already cached, the import returns the cached
module and our fake is ignored. So we delete any cached psycopg first.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

_SRC_PATH = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

# Ensure psycopg is NOT cached before any test imports main; the loop's
# ``import psycopg`` would otherwise return the real driver and bypass our
# fake. ``del sys.modules['psycopg']`` is safe — main is only imported
# lazily (inside ``_setup``).
sys.modules.pop("psycopg", None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _setup():
    """Lazy import main, reset shutdown event, seed DSN env var."""
    os.environ["ALPHARD_PG_DSN"] = "host=fake port=5432 dbname=alphard"
    if "main" not in sys.modules:
        # noqa: F401 — only imported for side-effect of module registration
        import main as _alphard_main  # noqa: F401
    main = sys.modules["main"]
    main._shutdown_event.clear()
    # Reset the metrics registry stub between tests so we can observe
    # exactly what each test case publishes.
    main._metrics_registry = None  # type: ignore[attr-defined]
    yield main
    main._shutdown_event.clear()
    main._metrics_registry = None  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """time.sleep is a no-op so the loop polls shutdown_event eagerly."""
    monkeypatch.setattr("time.sleep", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _isolate_psycopg(monkeypatch):
    """Install an isolated fake psycopg for this test only, restore on teardown.

    Critical: must restore the real ``psycopg`` module on teardown so
    later tests (e.g. ``test_pg_store_integration.py`` against a live
    Postgres service) see the genuine module. Otherwise ``sys.modules``
    retains our fake, and the integration tests fail with
    ``RuntimeError: not a psycopg error`` because the fake's
    ``psycopg.connect`` raises a synthetic error that pg_store cannot
    recover from.
    """
    # Drop any cached psycopg before installing the fake — the loop's
    # lazy ``import psycopg`` would otherwise return the real driver
    # and bypass our fake.
    sys.modules.pop("psycopg", None)
    yield
    # Restore: drop the fake so subsequent tests re-import the real
    # module on next ``import psycopg``.
    sys.modules.pop("psycopg", None)


def _install_registry(main):
    """Install a real MetricsRegistry so the loop can write to it."""
    from src.metrics_server import MetricsRegistry

    reg = MetricsRegistry()
    main._metrics_registry = reg  # type: ignore[attr-defined]
    return reg


def _patch_psycopg(total_row, complete_row):
    """Inject a fake psycopg module whose ``connect()`` returns a stub connection.

    psycopg's API uses two nested ``with`` blocks:

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()

    So ``connect(...)`` must return a context manager whose ``__enter__`` yields
    a connection, and ``conn.cursor()`` must return a context manager whose
    ``__enter__`` yields the cursor itself (psycopg's Cursor supports both
    ``with conn.cursor() as cur`` and bare ``cur = conn.cursor()``).

    Returns (fake_module, cursor_mock) so callers can inspect calls.
    """
    cursor = mock.MagicMock()
    cursor.execute = mock.MagicMock()
    # Two fetchone() calls per iteration: one per COUNT(*) query.
    cursor.fetchone = mock.MagicMock(side_effect=[total_row, complete_row])

    # Make ``with conn.cursor() as cur`` yield `cursor` itself (not a new mock).
    cursor.__enter__ = mock.MagicMock(return_value=cursor)
    cursor.__exit__ = mock.MagicMock(return_value=None)

    conn = mock.MagicMock()
    conn.cursor = mock.MagicMock(return_value=cursor)

    # Make ``with psycopg.connect(dsn) as conn`` yield `conn` itself.
    conn.__enter__ = mock.MagicMock(return_value=conn)
    conn.__exit__ = mock.MagicMock(return_value=None)

    fake_psycopg = mock.MagicMock()
    fake_psycopg.connect = mock.MagicMock(return_value=conn)
    # The loop's ``except psycopg.Error`` clause needs the attribute.
    fake_psycopg.Error = Exception
    sys.modules["psycopg"] = fake_psycopg
    return fake_psycopg, cursor


def _patch_psycopg_connect_raises(exc_factory):
    """Inject a fake psycopg whose connect() raises a fresh exception per call."""
    fake_psycopg = mock.MagicMock()
    fake_psycopg.connect = mock.MagicMock(side_effect=exc_factory)
    fake_psycopg.Error = type("Error", (Exception,), {})
    sys.modules["psycopg"] = fake_psycopg
    return fake_psycopg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_constants_match_spec(_setup):
    """Refresh cadence matches the issue spec (300s = 5 min)."""
    main = _setup
    assert main.UNIVERSE_METRICS_REFRESH_SECONDS == 300


def test_gauges_updated_after_successful_select(_setup):
    """Loop publishes both gauges from the SELECT COUNT(*) results."""
    main = _setup
    reg = _install_registry(main)
    fake, cursor = _patch_psycopg((42,), (7,))
    sleep_calls = {"n": 0}

    def fake_sleep(seconds):
        sleep_calls["n"] += 1
        # One iteration → set shutdown so the loop exits.
        main._shutdown_event.set()

    with mock.patch.object(main, "_sleep_interruptible", side_effect=fake_sleep):
        main._universe_metrics_loop()

    # Both gauges reflect the cursor values.
    assert reg.get_gauge("alphard_tickers_in_universe_total") == 42.0
    assert reg.get_gauge("alphard_tickers_with_full_history_total") == 7.0
    # SQL check: one COUNT(*) on bare ticker_universe, one filtered by backfill_complete.
    executed_sqls = [c.args[0] for c in cursor.execute.call_args_list]
    assert any(
        "FROM ticker_universe" in sql and "backfill_complete" not in sql for sql in executed_sqls
    ), f"missing bare COUNT(*) in {executed_sqls}"
    assert any(
        "backfill_complete = TRUE" in sql for sql in executed_sqls
    ), f"missing filtered COUNT(*) in {executed_sqls}"
    # One iteration → one sleep before shutdown.
    assert sleep_calls["n"] >= 1


def test_psycopg_error_does_not_kill_loop(_setup):
    """A psycopg.Error during connect leaves gauges untouched; loop survives."""
    main = _setup
    reg = _install_registry(main)
    reg.set_gauge("alphard_tickers_in_universe_total", 100.0)
    reg.set_gauge("alphard_tickers_with_full_history_total", 50.0)

    call_count = {"n": 0}

    def boom(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            main._shutdown_event.set()
        # Raise a psycopg.Error-equivalent (instance of the fake Error class).
        raise main_psycopg_Error("synthetic connection refused")

    main_psycopg_Error = type("Error", (Exception,), {})

    def exc_factory(*a, **kw):
        return boom(*a, **kw)

    fake_psycopg = mock.MagicMock()
    fake_psycopg.connect = mock.MagicMock(side_effect=exc_factory)
    fake_psycopg.Error = main_psycopg_Error
    sys.modules["psycopg"] = fake_psycopg

    with mock.patch.object(main, "_sleep_interruptible", lambda s: None):
        main._universe_metrics_loop()

    # Gauges untouched (prior values remain).
    assert reg.get_gauge("alphard_tickers_in_universe_total") == 100.0
    assert reg.get_gauge("alphard_tickers_with_full_history_total") == 50.0
    # Loop survived: connect was attempted at least twice.
    assert call_count["n"] >= 2, "loop died after first psycopg.Error"


def test_loop_disabled_when_dsn_unset(_setup, caplog):
    """With ALPHARD_PG_DSN unset, loop exits immediately with a warning."""
    main = _setup
    del os.environ["ALPHARD_PG_DSN"]
    reg = _install_registry(main)

    with caplog.at_level("WARNING", logger="alphard.universe_metrics"):
        main._universe_metrics_loop()

    assert any("ALPHARD_PG_DSN not set" in rec.message for rec in caplog.records)
    # Gauges never written.
    assert reg.get_gauge("alphard_tickers_in_universe_total") == 0.0
    assert reg.get_gauge("alphard_tickers_with_full_history_total") == 0.0


def test_thread_joins_on_shutdown(_setup):
    """main() spawns and joins the alphard-universe-metrics daemon thread on Ctrl-C."""
    main = _setup
    started_threads = []
    original_start = threading.Thread.start

    def spy_start(self):
        started_threads.append(self)
        original_start(self)

    # Make the loop's psycopg.connect raise a benign error so the loop body
    # completes without real I/O. The except clause catches it and the loop
    # sleeps again — _shutdown_event will be set by the heartbeat faker.
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic — loop body catches in except")

    fake_psycopg = mock.MagicMock()
    fake_psycopg.connect = mock.MagicMock(side_effect=boom)
    fake_psycopg.Error = type("Error", (Exception,), {})
    sys.modules["psycopg"] = fake_psycopg

    _install_registry(main)

    # Stub _spawn_backfill so the backfill-supervisor thread (also spawned by
    # main()) does not try to exec ``scripts/backfill_history_md.py`` — CI's
    # cwd is the repo root, not /app, so an unstubbed Popen would raise
    # FileNotFoundError inside the daemon thread, polluting test output.
    fake_popen = mock.MagicMock()
    fake_popen.pid = 99999
    with mock.patch.object(threading.Thread, "start", spy_start):
        with mock.patch.object(main, "_seconds_until_next_target_hour_msk", lambda *a: 0.0):
            with mock.patch.object(main, "_sleep_interruptible", lambda s: None):
                with mock.patch.object(
                    main.subprocess,
                    "run",
                    lambda *a, **kw: mock.Mock(returncode=0, stdout="", stderr=""),
                ):
                    with mock.patch.object(main.subprocess, "Popen", mock.MagicMock(return_value=fake_popen)):
                        ticks = {"n": 0}

                        def fake_sleep(s):
                            ticks["n"] += 1
                            if ticks["n"] >= 1:
                                raise KeyboardInterrupt("stop main")
                            return None

                        with mock.patch.object(main.time, "sleep", fake_sleep):
                            with pytest.raises((KeyboardInterrupt, SystemExit)):
                                main.main()

    um_threads = [t for t in started_threads if t.name == "alphard-universe-metrics"]
    assert len(um_threads) == 1
    assert um_threads[0].daemon is True
    # Thread must have exited (join succeeded in finally).
    assert not um_threads[0].is_alive(), "universe-metrics thread did not join on shutdown"


def test_loop_iterates_multiple_times(_setup):
    """Between iterations, _sleep_interruptible is called once per cycle."""
    main = _setup
    reg = _install_registry(main)
    call_count = {"n": 0}

    def fake_connect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            main._shutdown_event.set()
        cursor = mock.MagicMock()
        cursor.execute = mock.MagicMock()
        cursor.fetchone = mock.MagicMock(side_effect=[(10,), (2,)])
        cursor.__enter__ = mock.MagicMock(return_value=cursor)
        cursor.__exit__ = mock.MagicMock(return_value=None)
        conn = mock.MagicMock()
        conn.cursor = mock.MagicMock(return_value=cursor)
        conn.__enter__ = mock.MagicMock(return_value=conn)
        conn.__exit__ = mock.MagicMock(return_value=None)
        return conn

    fake_psycopg = mock.MagicMock()
    fake_psycopg.connect = mock.MagicMock(side_effect=fake_connect)
    fake_psycopg.Error = Exception
    sys.modules["psycopg"] = fake_psycopg

    with mock.patch.object(main, "_sleep_interruptible", lambda s: None):
        main._universe_metrics_loop()

    assert call_count["n"] == 3
    # Final gauges reflect the LAST iteration.
    assert reg.get_gauge("alphard_tickers_in_universe_total") == 10.0
    assert reg.get_gauge("alphard_tickers_with_full_history_total") == 2.0


def test_generic_exception_also_caught(_setup):
    """A non-psycopg exception is caught by the broad ``except Exception`` clause."""
    main = _setup
    reg = _install_registry(main)
    call_count = {"n": 0}

    def boom(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            main._shutdown_event.set()
        raise RuntimeError("not a psycopg error")

    fake_psycopg = mock.MagicMock()
    fake_psycopg.connect = mock.MagicMock(side_effect=boom)
    fake_psycopg.Error = type("Error", (Exception,), {})
    sys.modules["psycopg"] = fake_psycopg

    with mock.patch.object(main, "_sleep_interruptible", lambda s: None):
        # Should NOT raise — RuntimeError is caught by the fallback clause.
        main._universe_metrics_loop()

    assert call_count["n"] >= 2
    # No gauge value set since every iteration failed.
    assert reg.get_gauge("alphard_tickers_in_universe_total") == 0.0
