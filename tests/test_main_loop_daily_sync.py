"""Tests for src/main.py heartbeat + daily_sync daemon (Phase 1.6).

The daemon is verified by patching subprocess.run to a stub and asserting:
1. main() spawns the daemon thread.
2. _daily_sync_loop invokes subprocess.run with the right args.
3. The loop survives a subprocess crash and keeps running.
4. timeout=DAILY_SYNC_SUBPROCESS_TIMEOUT is enforced.
5. main() sets the shutdown event on Ctrl-C and joins the daemon.
"""

from __future__ import annotations

import subprocess
import threading
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


def test_daily_sync_loop_calls_subprocess_with_right_args(monkeypatch) -> None:
    """First iteration of the daemon shells out to scripts/daily_sync.py --days 5."""
    counter = {"n": 0}

    def fake(cmd, **kwargs):
        counter["n"] += 1
        if counter["n"] >= 1:
            # Tell the daemon to exit after the first call.
            main_module._shutdown_event.set()
        return mock.Mock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(main_module.subprocess, "run", fake)
    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)

    main_module._daily_sync_loop()

    assert counter["n"] == 1


def test_daily_sync_loop_subprocess_call_args(monkeypatch) -> None:
    """Capture the exact args subprocess.run was called with on the first iteration."""
    captured: dict = {}

    def fake(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        main_module._shutdown_event.set()
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main_module.subprocess, "run", fake)
    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)

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

    monkeypatch.setattr(main_module.subprocess, "run", fake)
    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)

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

    monkeypatch.setattr(main_module.subprocess, "run", fake)
    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)

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

    monkeypatch.setattr(main_module.subprocess, "run", fake)
    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)

    main_module._daily_sync_loop()

    assert counter["n"] == 2


def test_daily_sync_loop_exits_on_shutdown_event(monkeypatch) -> None:
    """Setting the shutdown event must cause the daemon to exit promptly."""
    counter = {"n": 0}

    def fake(cmd, **kwargs):
        counter["n"] += 1
        # Set shutdown right after first call so daemon exits on sleep check.
        main_module._shutdown_event.set()
        return mock.Mock(returncode=0, stdout="", stderr="")

    sleeps = {"n": 0}

    def fake_sleep(s):
        sleeps["n"] += 1
        # The daemon sleeps in 1s slices; stop after a few.
        if sleeps["n"] >= 3:
            pass

    monkeypatch.setattr(main_module.subprocess, "run", fake)
    monkeypatch.setattr(main_module.time, "sleep", fake_sleep)

    main_module._daily_sync_loop()

    assert counter["n"] == 1


def test_main_spawns_daemon_thread(monkeypatch) -> None:
    """main() must start a daemon thread named 'alphard-daily-sync'."""
    started_threads: list[threading.Thread] = []
    original_start = threading.Thread.start

    def spy_start(self):
        started_threads.append(self)
        original_start(self)

    monkeypatch.setattr(threading.Thread, "start", spy_start)
    monkeypatch.setattr(
        main_module.subprocess,
        "run",
        lambda *a, **kw: (
            main_module._shutdown_event.set(),
            mock.Mock(returncode=0, stdout="", stderr=""),
        )[-1],
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
        if sync_calls["n"] >= 2:
            main_module._shutdown_event.set()
            return mock.Mock(returncode=0, stdout="", stderr="")
        raise RuntimeError("simulated crash")

    def fake_sleep(s):
        heartbeat_ticks["n"] += 1
        if heartbeat_ticks["n"] >= 3:
            main_module._shutdown_event.set()
        return None

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
        # Don't actually wait — we're in a test, the daemon is mocked.
        original_join(self, timeout=0)

    monkeypatch.setattr(threading.Thread, "join", spy_join)
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
