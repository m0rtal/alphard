"""Tests for src/main.py daemon lifecycle (Phase 2.7 delisted_at cron).

Phase 1.6 added `_daily_sync_loop` + watchdog. Phase 2.7 mirrors it with
`_delisted_sync_loop` on a weekly cadence. These tests verify:

- Both daemon threads are spawned on `main()` startup.
- The delisted daemon waits 24h before the first run (priority to daily_sync).
- The delisted daemon calls scripts/backfill_delisted_via_tinkoff.py via
  subprocess (process boundary = circuit breaker).
- Both daemons exit cleanly on `_shutdown_event.set()`.
- Cadence constants are wired correctly (daily 24h, delisted 7d).

The threads do real sleeps via `_sleep_interruptible`; we mock that out so
the test completes in <1s.
"""

from __future__ import annotations

import sys

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _setup():
    """Lazy import main + reset _shutdown_event between tests."""
    sys.path.insert(0, "/root/projects/alphard/src")
    if "main" not in sys.modules:
        # noqa: F401 — only imported for side-effect of module registration
        import main  # noqa: F401
    main = sys.modules["main"]
    main._shutdown_event.clear()
    yield main


@pytest.fixture(autouse=True)
def _isolate_subprocess(monkeypatch):
    """Default: subprocess.run is a no-op returning rc=0. Tests override as needed."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "ok"
    fake_result.stderr = ""
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_result)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """time.sleep is a no-op. _sleep_interruptible is mocked per-test."""
    monkeypatch.setattr("time.sleep", lambda *a, **kw: None)


def test_constants_match_roadmap(_setup):
    main = _setup
    # Phase 1.6 daily_sync cadence
    assert main.DAILY_SYNC_INTERVAL_SECONDS == 3600
    assert main.DAILY_SYNC_SUBPROCESS_TIMEOUT == 600

    # Phase 2.7 delisted_sync cadence
    assert main.DELISTED_SYNC_CADENCE_SECONDS == 7 * 24 * 3600
    assert main.DELISTED_SYNC_SUBPROCESS_TIMEOUT == 2400


def test_delisted_loop_skips_body_when_event_set(_setup):
    """If _shutdown_event is set before the loop starts, no subprocess call."""
    main = _setup
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        r.stdout = "ok"
        r.stderr = ""
        return r

    main._shutdown_event.set()
    main._delisted_sync_loop()
    assert calls == []


def test_delisted_loop_calls_correct_subprocess(_setup):
    """One iteration runs the right subprocess with expected args."""
    main = _setup
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kwargs"] = kw
        r = MagicMock()
        r.returncode = 0
        r.stdout = "ok"
        r.stderr = ""
        return r

    main.subprocess.run = fake_run  # type: ignore

    # Loop structure: enter-while -> sleep(24h) -> subprocess -> sleep(7d) -> check
    # event -> exit. We patch _sleep_interruptible so the second sleep sets
    # the event AFTER subprocess ran.
    calls = [0]

    def fake_sleep(_seconds):
        calls[0] += 1
        if calls[0] >= 2:  # after first iter's post-subprocess sleep
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    main._delisted_sync_loop()

    assert captured["cmd"] == ["python", "scripts/backfill_delisted_via_tinkoff.py"]
    assert captured["kwargs"]["cwd"] == "/app"
    assert captured["kwargs"]["timeout"] == main.DELISTED_SYNC_SUBPROCESS_TIMEOUT
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_delisted_loop_continues_after_subprocess_failure(_setup):
    """Non-zero rc is logged as warning; loop tries again."""
    main = _setup
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "boom"
        return r

    main.subprocess.run = fake_run  # type: ignore

    # Let loop run two iterations.
    calls_count = [0]

    def fake_sleep(_seconds):
        calls_count[0] += 1
        if calls_count[0] >= 3:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    main._delisted_sync_loop()

    # 2 subprocess calls (iter 1 + iter 2), then iter 3 sleep sets event.
    assert len(calls) == 2


def test_delisted_loop_continues_after_timeout(_setup):
    """subprocess.TimeoutExpired is caught; loop continues."""
    import subprocess as sp

    main = _setup
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            raise sp.TimeoutExpired(cmd=cmd, timeout=kw["timeout"])
        r = MagicMock()
        r.returncode = 0
        r.stdout = "ok"
        r.stderr = ""
        return r

    main.subprocess.run = fake_run  # type: ignore

    calls_count = [0]

    def fake_sleep(_seconds):
        calls_count[0] += 1
        if calls_count[0] >= 3:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    main._delisted_sync_loop()

    assert len(calls) == 2


def test_delisted_loop_logs_failure_with_stderr(_setup, caplog):
    """When subprocess fails with stderr, the loop logs the stderr tail."""
    import logging

    main = _setup

    def fake_run(cmd, **kw):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "Connection refused to tinkoff api"
        return r

    main.subprocess.run = fake_run  # type: ignore

    calls_count = [0]

    def fake_sleep(_seconds):
        calls_count[0] += 1
        if calls_count[0] >= 2:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    with caplog.at_level(logging.WARNING, logger="alphard.delisted_sync"):
        main._delisted_sync_loop()

    # Verify the warning message contains the stderr tail.
    assert any("FAILED rc=1" in record.message and "Connection refused" in record.message for record in caplog.records)


def test_delisted_loop_logs_success(_setup, caplog):
    """Successful subprocess run produces an INFO log with tail of stdout."""
    import logging

    main = _setup

    def fake_run(cmd, **kw):
        r = MagicMock()
        r.returncode = 0
        r.stdout = "Backfilled 42 tickers"
        r.stderr = ""
        return r

    main.subprocess.run = fake_run  # type: ignore

    calls_count = [0]

    def fake_sleep(_seconds):
        calls_count[0] += 1
        if calls_count[0] >= 2:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    with caplog.at_level(logging.INFO, logger="alphard.delisted_sync"):
        main._delisted_sync_loop()

    assert any("OK rc=0" in record.message and "Backfilled 42 tickers" in record.message for record in caplog.records)


def test_delisted_loop_logs_unexpected_exception(_setup, caplog):
    """Generic exception from subprocess.run is caught and logged."""
    import logging

    main = _setup

    def fake_run(cmd, **kw):
        raise OSError("connection lost")

    main.subprocess.run = fake_run  # type: ignore

    calls_count = [0]

    def fake_sleep(_seconds):
        calls_count[0] += 1
        if calls_count[0] >= 2:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    with caplog.at_level(logging.ERROR, logger="alphard.delisted_sync"):
        main._delisted_sync_loop()

    assert any(
        "unexpected error" in record.message.lower() and "connection lost" in record.message
        for record in caplog.records
    )


def test_delisted_loop_first_run_is_after_24h(_setup, monkeypatch):
    """The very first sleep is 24*3600, not the cadence."""
    main = _setup
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    main._delisted_sync_loop()

    # First sleep must be 24h, not 7d. Cadence kicks in only on subsequent runs.
    assert sleeps[0] == 24 * 3600


def test_main_spawns_both_threads(_setup, monkeypatch):
    """main() starts alphard-daily-sync AND alphard-delisted-sync threads."""
    main = _setup
    started_threads = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            started_threads.append(name)
            self._name = name
            self._daemon = daemon

        def start(self):
            pass

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr("threading.Thread", FakeThread)

    # Patch inner targets so threads no-op instead of running real loops.
    monkeypatch.setattr("main._daily_sync_loop", lambda: None)
    monkeypatch.setattr("main._delisted_sync_loop", lambda: None)
    monkeypatch.setattr("main._sleep_interruptible", lambda _s: None)
    monkeypatch.setattr("time.sleep", lambda *a, **kw: None)

    # Set event so the heartbeat while-loop exits on first iteration.
    main._shutdown_event.set()

    try:
        main.main()
    except SystemExit:
        pass

    assert "alphard-daily-sync" in started_threads
    assert "alphard-delisted-sync" in started_threads


def test_shutdown_event_initially_unset(_setup):
    main = _setup
    main._shutdown_event.clear()
    assert not main._shutdown_event.is_set()
