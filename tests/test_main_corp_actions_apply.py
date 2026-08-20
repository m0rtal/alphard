"""Tests for src/main.py corp_actions_apply daemon (Phase 2.5 step 2b).

Mirrors tests/test_main_delisted_sync.py but for the corp_actions_apply
daemon thread. The daemon:

- Spawns from main() with name "alphard-corp-actions-apply".
- Calls scripts/apply_corporate_actions.py via subprocess on a weekly
  cadence with timeout=CORP_ACTIONS_APPLY_SUBPROCESS_TIMEOUT.
- First run waits 24h after launch (priority to daily_sync and
  delisted_sync).
- Survives subprocess failures, timeouts, and unexpected exceptions.
- Logs rc, stdout tail, stderr tail at INFO/WARNING/ERROR.
- main() joins the thread on shutdown with timeout=10s.

Like test_main_delisted_sync.py, time.sleep is mocked and
_sleep_interruptible is patched per-test so the suite runs in <1s.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC_PATH = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)


@pytest.fixture(autouse=True)
def _setup():
    """Lazy import main + reset _shutdown_event between tests."""
    if "main" not in sys.modules:
        # noqa: F401 — only imported for side-effect of module registration
        import main as _alphard_main  # noqa: F401
    main = sys.modules["main"]
    main._shutdown_event.clear()
    yield main
    main._shutdown_event.clear()


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


# ---------- constants ----------


def test_constants_match_roadmap(_setup):
    """Cadence constants are wired to the documented values."""
    main = _setup
    assert main.CORP_ACTIONS_APPLY_CADENCE_SECONDS == 7 * 24 * 3600
    assert main.CORP_ACTIONS_APPLY_SUBPROCESS_TIMEOUT == 3600


# ---------- loop semantics ----------


def test_corp_actions_loop_skips_body_when_event_set(_setup):
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

    main.subprocess.run = fake_run  # type: ignore

    main._shutdown_event.set()
    main._corp_actions_apply_loop()
    assert calls == []


def test_corp_actions_loop_calls_correct_subprocess(_setup):
    """One iteration runs the right subprocess with expected args."""
    main = _setup
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kwargs"] = kw
        r = MagicMock()
        r.returncode = 0
        r.stdout = "applied 42 tickers"
        r.stderr = ""
        return r

    main.subprocess.run = fake_run  # type: ignore

    # First sleep = 24h before first run; second sleep = cadence 7d.
    # Patch so the SECOND sleep triggers the shutdown event.
    calls = [0]

    def fake_sleep(_seconds):
        calls[0] += 1
        if calls[0] >= 2:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    main._corp_actions_apply_loop()

    assert captured["cmd"] == ["python", "scripts/apply_corporate_actions.py"]
    assert captured["kwargs"]["cwd"] == "/app"
    assert captured["kwargs"]["timeout"] == main.CORP_ACTIONS_APPLY_SUBPROCESS_TIMEOUT
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_corp_actions_loop_first_run_is_after_24h(_setup):
    """The very first sleep is 24*3600, not the cadence."""
    main = _setup
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    main._corp_actions_apply_loop()

    # First sleep must be 24h, not 7d. Cadence kicks in only on subsequent runs.
    assert sleeps[0] == 24 * 3600


def test_corp_actions_loop_continues_after_subprocess_failure(_setup):
    """Non-zero rc is logged as warning; loop tries again."""
    main = _setup
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "MOEX ISS 503"
        return r

    main.subprocess.run = fake_run  # type: ignore

    # Let loop run two iterations: first sleep 24h, second sleep cadence,
    # third sleep cadence -> triggers shutdown.
    calls_count = [0]

    def fake_sleep(_seconds):
        calls_count[0] += 1
        if calls_count[0] >= 3:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    main._corp_actions_apply_loop()

    # 2 subprocess calls (iter 1 + iter 2); iter 3 sleep sets event.
    assert len(calls) == 2


def test_corp_actions_loop_continues_after_timeout(_setup):
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

    main._corp_actions_apply_loop()

    assert len(calls) == 2


def test_corp_actions_loop_logs_failure_with_stderr(_setup, caplog):
    """When subprocess fails with stderr, the loop logs the stderr tail."""
    main = _setup

    def fake_run(cmd, **kw):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "MOEX ISS connection refused"
        return r

    main.subprocess.run = fake_run  # type: ignore

    calls_count = [0]

    def fake_sleep(_seconds):
        calls_count[0] += 1
        if calls_count[0] >= 2:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    with caplog.at_level(logging.WARNING, logger="alphard.corp_actions_apply"):
        main._corp_actions_apply_loop()

    # Verify the warning message contains the stderr tail.
    assert any(
        "FAILED rc=1" in record.message and "MOEX ISS connection refused" in record.message
        for record in caplog.records
    )


def test_corp_actions_loop_logs_success(_setup, caplog):
    """Successful subprocess run produces an INFO log with tail of stdout."""
    main = _setup

    def fake_run(cmd, **kw):
        r = MagicMock()
        r.returncode = 0
        r.stdout = "apply_corporate_actions: done applied=42 skipped_fresh=10 no_actions=20"
        r.stderr = ""
        return r

    main.subprocess.run = fake_run  # type: ignore

    calls_count = [0]

    def fake_sleep(_seconds):
        calls_count[0] += 1
        if calls_count[0] >= 2:
            main._shutdown_event.set()

    main._sleep_interruptible = fake_sleep  # type: ignore

    with caplog.at_level(logging.INFO, logger="alphard.corp_actions_apply"):
        main._corp_actions_apply_loop()

    assert any(
        "OK rc=0" in record.message and "applied=42" in record.message
        for record in caplog.records
    )


# ---------- main() thread spawn + join ----------


def test_main_spawns_corp_actions_thread(_setup, monkeypatch):
    """main() starts alphard-corp-actions-apply thread alongside the others."""
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
    monkeypatch.setattr("main._corp_actions_apply_loop", lambda: None)
    monkeypatch.setattr("main._sleep_interruptible", lambda _s: None)
    monkeypatch.setattr("time.sleep", lambda *a, **kw: None)

    # Set event so the heartbeat while-loop exits on first iteration.
    main._shutdown_event.set()

    try:
        main.main()
    except SystemExit:
        pass

    assert "alphard-corp-actions-apply" in started_threads, (
        f"corp_actions thread not spawned; got {started_threads}"
    )