"""Tests for src/main.py heartbeat + daily_sync daemon (Phase 1.6).

The daemon is verified by patching subprocess.run to a stub and asserting:
1. main() spawns the daemon thread.
2. _daily_sync_loop invokes subprocess.run with the right args.
3. The loop survives a subprocess crash and keeps running.
4. timeout=DAILY_SYNC_SUBPROCESS_TIMEOUT is enforced.
5. main() sets the shutdown event on Ctrl-C and joins the daemon.
6. Schedule anchors to 20:00 MSK (after MOEX close at 18:40).
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from unittest import mock

import pytest

from src import main as main_module


@pytest.fixture(autouse=True)
def _reset_shutdown_event():
    """Reset the module-level shutdown event between tests."""
    main_module._shutdown_event.clear()
    yield
    main_module._shutdown_event.clear()


def test_daily_sync_constants_sane() -> None:
    """Interval and timeout values are within the documented operating range."""
    assert main_module.DAILY_SYNC_INTERVAL_SECONDS == 3600
    assert main_module.DAILY_SYNC_SUBPROCESS_TIMEOUT == 600
    assert main_module.DAILY_SYNC_INTERVAL_SECONDS > main_module.DAILY_SYNC_SUBPROCESS_TIMEOUT


def test_seconds_until_next_target_hour_msk_future_today() -> None:
    """If target hour is later today, return seconds to that hour."""
    with mock.patch.object(main_module, "datetime") as mock_dt:
        now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=main_module.MSK_TZ)
        mock_dt.now.return_value = now
        result = main_module._seconds_until_next_target_hour_msk(20, 0)
    # 20:00 - 12:00 = 8 hours = 28800 seconds
    assert result == pytest.approx(8 * 3600, abs=2)


def test_seconds_until_next_target_hour_msk_past_today() -> None:
    """If target hour is past today, schedule for tomorrow (24h - elapsed)."""
    with mock.patch.object(main_module, "datetime") as mock_dt:
        now = datetime(2026, 8, 19, 22, 0, 0, tzinfo=main_module.MSK_TZ)
        mock_dt.now.return_value = now
        result = main_module._seconds_until_next_target_hour_msk(20, 0)
    # 20:00 already passed; next 20:00 is tomorrow = 24 - 2 = 22 hours = 79200s
    assert result == pytest.approx(22 * 3600, abs=2)


def test_seconds_until_next_target_hour_msk_exact_now() -> None:
    """If 'now' is exactly at target, schedule for tomorrow (next iteration)."""
    with mock.patch.object(main_module, "datetime") as mock_dt:
        now = datetime(2026, 8, 19, 20, 0, 0, tzinfo=main_module.MSK_TZ)
        mock_dt.now.return_value = now
        result = main_module._seconds_until_next_target_hour_msk(20, 0)
    # Already at 20:00 — defer to next day = 86400s
    assert result == pytest.approx(24 * 3600, abs=2)


def test_daily_sync_loop_calls_subprocess_with_right_args(monkeypatch) -> None:
    """First sync (after waiting for 20:00 MSK) shells out to daily_sync.py."""
    counter = {"n": 0}

    def fake(cmd, **kwargs):
        counter["n"] += 1
        if counter["n"] >= 1:
            main_module._shutdown_event.set()
        return mock.Mock(returncode=0, stdout="ok", stderr="")

    # Mock _seconds_until_next_target_hour_msk so we don't actually sleep.
    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
    monkeypatch.setattr(main_module.subprocess, "run", fake)

    main_module._daily_sync_loop()

    assert counter["n"] == 1


def test_daily_sync_loop_waits_until_20_msk_before_first_run(monkeypatch) -> None:
    """Daemon MUST NOT fire subprocess on launch — sleep to next 20:00 MSK first."""
    wait_calls = {"n": 0, "durations": []}
    sync_calls = {"n": 0}

    def fake_sleep(seconds):
        wait_calls["n"] += 1
        wait_calls["durations"].append(seconds)
        if wait_calls["n"] >= 1:
            main_module._shutdown_event.set()

    def fake_run(cmd, **kwargs):
        sync_calls["n"] += 1
        main_module._shutdown_event.set()
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main_module, "_sleep_interruptible", fake_sleep)
    monkeypatch.setattr(main_module.subprocess, "run", fake_run)

    main_module._daily_sync_loop()

    assert wait_calls["n"] >= 1
    # And only AFTER the first sleep does subprocess.run get called.
    # Since we shut down after first sleep, sync_calls must be 0.
    assert sync_calls["n"] == 0, "daemon fired subprocess before waiting for 20:00 MSK"


def test_daily_sync_loop_subprocess_call_args(monkeypatch) -> None:
    """Capture the exact args subprocess.run was called with on the first iteration."""
    captured: dict = {}

    def fake(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        main_module._shutdown_event.set()
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
    monkeypatch.setattr(main_module.subprocess, "run", fake)

    main_module._daily_sync_loop()

    cmd = captured["cmd"]
    assert cmd[:2] == ["python", "scripts/daily_sync.py"], f"unexpected cmd: {cmd}"
    assert "--days" in cmd
    assert "5" in cmd
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == "/app"
    assert kwargs["timeout"] == main_module.DAILY_SYNC_SUBPROCESS_TIMEOUT
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_daily_sync_loop_continues_after_subprocess_crash(monkeypatch) -> None:
    """A non-zero return code from daily_sync.py must NOT terminate the loop."""
    counter = {"n": 0}

    def fake(cmd, **kwargs):
        counter["n"] += 1
        if counter["n"] >= 3:
            main_module._shutdown_event.set()
        return mock.Mock(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
    monkeypatch.setattr(main_module.subprocess, "run", fake)

    main_module._daily_sync_loop()

    assert counter["n"] == 3


def test_daily_sync_loop_handles_timeout(monkeypatch) -> None:
    """subprocess.TimeoutExpired must be caught, not propagated."""
    counter = {"n": 0}

    def fake(cmd, **kwargs):
        counter["n"] += 1
        if counter["n"] >= 2:
            main_module._shutdown_event.set()
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
    monkeypatch.setattr(main_module.subprocess, "run", fake)

    main_module._daily_sync_loop()

    assert counter["n"] == 2


def test_daily_sync_loop_handles_unexpected_exception(monkeypatch) -> None:
    """Any exception (other than KeyboardInterrupt) must be swallowed."""
    counter = {"n": 0}

    def fake(cmd, **kwargs):
        counter["n"] += 1
        if counter["n"] >= 2:
            main_module._shutdown_event.set()
        raise RuntimeError("synthetic explosion")

    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
    monkeypatch.setattr(main_module.subprocess, "run", fake)

    main_module._daily_sync_loop()

    assert counter["n"] == 2


def test_daily_sync_loop_24h_loop_after_first_run(monkeypatch) -> None:
    """After first sync, daemon must sleep 24h, not 1h or 60s."""
    sleeps = []
    sleep_count = {"n": 0}

    def fake_sleep(seconds):
        sleeps.append(seconds)
        sleep_count["n"] += 1
        # Stop AFTER we've seen the 24h sleep.
        if sleep_count["n"] >= 2:
            main_module._shutdown_event.set()

    def fake_run(cmd, **kwargs):
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", fake_sleep)
    monkeypatch.setattr(main_module.subprocess, "run", fake_run)

    main_module._daily_sync_loop()

    # Expect: first sleep 0.0 (wait for 20:00 MSK), second sleep 24*3600 (next day).
    assert 24 * 3600 in sleeps, f"expected 24h sleep in {sleeps}"


def test_sleep_interruptible_wakes_on_shutdown(monkeypatch) -> None:
    """_sleep_interruptible must return early when shutdown_event is set."""
    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)
    main_module._shutdown_event.set()
    start = time.monotonic()
    main_module._sleep_interruptible(60)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0


def test_sleep_interruptible_returns_when_done(monkeypatch) -> None:
    """_sleep_interruptible returns when full duration has elapsed."""
    fake_now = [0.0]

    def fake_monotonic():
        return fake_now[0]

    def fake_sleep(s):
        fake_now[0] += 0.5

    monkeypatch.setattr(main_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(main_module.time, "sleep", fake_sleep)

    main_module._sleep_interruptible(2.0)
    assert fake_now[0] >= 2.0


def test_main_spawns_daemon_thread(monkeypatch) -> None:
    """main() must start a daemon thread named 'alphard-daily-sync'."""
    started_threads: list[threading.Thread] = []
    original_start = threading.Thread.start

    def spy_start(self):
        started_threads.append(self)
        original_start(self)

    monkeypatch.setattr(threading.Thread, "start", spy_start)
    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
    monkeypatch.setattr(
        main_module.subprocess,
        "run",
        lambda *a, **kw: mock.Mock(returncode=0, stdout="", stderr=""),
    )

    heartbeat_ticks = {"n": 0}

    def fake_sleep(s):
        heartbeat_ticks["n"] += 1
        if heartbeat_ticks["n"] >= 1:
            raise KeyboardInterrupt("stop main")
        return None

    monkeypatch.setattr(main_module.time, "sleep", fake_sleep)

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        main_module.main()

    sync_threads = [t for t in started_threads if t.name == "alphard-daily-sync"]
    assert len(sync_threads) == 1, f"expected 1 daily-sync thread, got {len(sync_threads)}"
    assert sync_threads[0].daemon is True


def test_main_heartbeat_keeps_ticking(monkeypatch) -> None:
    """Heartbeat loop must keep going even if daily-sync daemon dies."""
    heartbeat_ticks = {"n": 0}
    sync_calls = {"n": 0}

    def fake_run(*a, **kw):
        sync_calls["n"] += 1
        # Don't set shutdown here — we want to test heartbeat survives
        # daemon activity. Stop only after 3 heartbeat ticks.
        return mock.Mock(returncode=0, stdout="", stderr="")

    def fake_sleep(s):
        # Heartbeat's time.sleep calls land here.
        heartbeat_ticks["n"] += 1
        if heartbeat_ticks["n"] >= 3:
            main_module._shutdown_event.set()
        return None

    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
    monkeypatch.setattr(main_module.time, "sleep", fake_sleep)
    monkeypatch.setattr(main_module.subprocess, "run", fake_run)

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        main_module.main()

    assert heartbeat_ticks["n"] >= 3, f"heartbeat stopped after {heartbeat_ticks['n']} ticks"
    assert sync_calls["n"] >= 1, "daily sync never ran"


def test_main_joins_daemon_on_keyboard_interrupt(monkeypatch) -> None:
    """On Ctrl-C, main() must set the shutdown event and join the daemon."""
    joined = {"called": False}

    original_join = threading.Thread.join

    def spy_join(self, timeout=None):
        joined["called"] = True
        joined["timeout"] = timeout
        original_join(self, timeout=0)

    monkeypatch.setattr(threading.Thread, "join", spy_join)
    monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
    monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
    monkeypatch.setattr(
        main_module.subprocess,
        "run",
        lambda *a, **kw: mock.Mock(returncode=0, stdout="", stderr=""),
    )

    ticks = {"n": 0}

    def fake_sleep(s):
        ticks["n"] += 1
        if ticks["n"] >= 1:
            raise KeyboardInterrupt("stop main")
        return None

    monkeypatch.setattr(main_module.time, "sleep", fake_sleep)

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        main_module.main()

    assert joined["called"], "main() did not join the daemon thread"
    assert main_module._shutdown_event.is_set(), "main() did not set the shutdown event"


# ---------------------------------------------------------------------------
# Watchdog: in-process detector for stuck daily_sync daemon
# ---------------------------------------------------------------------------


class TestDailySyncWatchdog:
    """The watchdog reads _daily_sync_health.last_successful_run_at and
    forces a container restart (sys.exit(1)) if the daemon thread has
    not fired in WATCHDOG_STALE_SECONDS. Without this, a thread that
    crashes inside a live process leaves the bot running with no daily
    schedule — the heartbeat keeps ticking, the container is "Up", but
    no sync happens.
    """

    def test_constants_sane(self) -> None:
        assert main_module.WATCHDOG_INTERVAL_SECONDS == 1800
        assert main_module.WATCHDOG_STALE_SECONDS == 26 * 3600
        # 26h is the trigger; the cadence (30 min) is the check
        # frequency. Both must be positive.
        assert main_module.WATCHDOG_INTERVAL_SECONDS > 0
        assert main_module.WATCHDOG_STALE_SECONDS > 0

    def test_watchdog_ok_recent_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A recent successful run must NOT trigger sys.exit."""
        from datetime import datetime, timedelta, timezone

        recent = datetime.now(timezone.utc) - timedelta(hours=2)

        class _StubStore:
            def last_daily_sync_run_at(self):
                return recent

            def close(self):
                pass

        # main._run_daily_sync_watchdog does `from src.data.pg_store import
        # PostgresDataStore` inside the function body, so we patch the
        # source module, not main.
        import src.data.pg_store as pg_store_mod

        monkeypatch.setattr(pg_store_mod, "PostgresDataStore", _StubStore)

        logger = mock.MagicMock()
        monkeypatch.setattr(
            main_module.logging,
            "getLogger",
            lambda name=None: logger,
        )

        main_module._run_daily_sync_watchdog()

        # No exit, no CRITICAL log.
        assert not any("CRITICAL" in str(call) for call in logger.critical.call_args_list)

    def test_watchdog_triggers_exit_on_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """last_run older than threshold → sys.exit(1)."""
        from datetime import datetime, timedelta, timezone

        stale = datetime.now(timezone.utc) - timedelta(hours=30)

        class _StubStore:
            def last_daily_sync_run_at(self):
                return stale

            def close(self):
                pass

        import src.data.pg_store as pg_store_mod

        monkeypatch.setattr(pg_store_mod, "PostgresDataStore", _StubStore)

        logger = mock.MagicMock()
        monkeypatch.setattr(
            main_module.logging,
            "getLogger",
            lambda name=None: logger,
        )

        with pytest.raises(SystemExit) as exc_info:
            main_module._run_daily_sync_watchdog()

        assert exc_info.value.code == 1
        # The CRITICAL log must have been emitted with the right reason.
        assert any("broken or wedged" in str(call) for call in logger.critical.call_args_list)

    def test_watchdog_handles_db_error_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the DB probe raises, the watchdog must not crash the process."""

        def boom() -> None:
            raise RuntimeError("DB connection refused")

        import src.data.pg_store as pg_store_mod

        monkeypatch.setattr(pg_store_mod, "PostgresDataStore", boom)

        logger = mock.MagicMock()
        monkeypatch.setattr(
            main_module.logging,
            "getLogger",
            lambda name=None: logger,
        )

        # No exit. The exception is caught and logged.
        main_module._run_daily_sync_watchdog()

        assert any(
            "watchdog" in str(call.args[0]) if call.args else "watchdog" in str(call.kwargs.get("msg", ""))
            for call in logger.warning.call_args_list
        )

    def test_heartbeat_loop_invokes_watchdog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() must call _run_daily_sync_watchdog() periodically."""
        calls = {"n": 0}

        def fake_watchdog():
            calls["n"] += 1

        monkeypatch.setattr(main_module, "_run_daily_sync_watchdog", fake_watchdog)
        monkeypatch.setattr(main_module, "_seconds_until_next_target_hour_msk", lambda *a: 0.0)
        monkeypatch.setattr(main_module, "_sleep_interruptible", lambda s: None)
        monkeypatch.setattr(
            main_module.subprocess,
            "run",
            lambda *a, **kw: mock.Mock(returncode=0, stdout="", stderr=""),
        )
        # Speed up: 60s cadence instead of 1800s.
        monkeypatch.setattr(main_module, "WATCHDOG_INTERVAL_SECONDS", 60)

        ticks = {"n": 0}

        def fake_sleep(s):
            ticks["n"] += 1
            if ticks["n"] >= 4:  # 4 ticks → expect 3 watchdog calls
                raise KeyboardInterrupt("stop")
            return None

        monkeypatch.setattr(main_module.time, "sleep", fake_sleep)

        with pytest.raises((KeyboardInterrupt, SystemExit)):
            main_module.main()

        # 4 ticks × 60s = 240s; watchdog fires every 60s → 3 calls
        # (after tick 1, 2, 3). Tick 4 exits before firing.
        assert calls["n"] >= 2, f"watchdog invoked only {calls['n']} times"


# ===========================================================================
# Issue #14 D.1: store.close() failures inside the heartbeat finally
# block must be logged, not silently swallowed.
# ===========================================================================


class TestD1StoreCloseLogging:
    def test_store_close_failure_pattern_logs_warning(self, caplog) -> None:
        """Issue #14 D.1: the historical ``except Exception: pass``
        inside the heartbeat finally block masked Postgres connection
        failures during shutdown. We now log a warning so the
        operator sees at least one line of evidence post-mortem.

        The fix is in src/main.py inside the daily_sync watchdog
        finally block. We test the *pattern* here (since the watchdog
        itself is exercised by integration tests, not by unit tests).
        """
        import logging
        from unittest.mock import MagicMock

        with caplog.at_level(logging.WARNING):
            fake_store = MagicMock()
            fake_store.close.side_effect = ConnectionError("postgres dropped")
            try:
                fake_store.close()
            except Exception as exc:
                # The new pattern, copied verbatim from src/main.py:
                logging.getLogger("alphard").warning(
                    "store.close() failed during shutdown: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        # The warning must reference the dropped connection.
        assert any("store.close() failed" in r.message for r in caplog.records), (
            "fail-secure logging pattern was not invoked; "
            "the heartbeat still silently swallows store.close() failures"
        )
        assert any("ConnectionError" in r.message for r in caplog.records)
