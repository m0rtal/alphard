"""Tests for src/main.py macro_sync daemon (Phase 2.3 Macro Agent).

Mirrors tests/test_main_corp_actions_apply.py. The daemon:

- Spawns from main() with name "alphard-macro-sync".
- Calls scripts/run_macro_sync.py via subprocess on an hourly cadence
  with timeout=MACRO_SYNC_SUBPROCESS_TIMEOUT.
- First run waits MACRO_SYNC_FIRST_RUN_DELAY_SECONDS (5 min) after launch.
- Subsequent runs every MACRO_SYNC_CADENCE_SECONDS (1h).
- Survives subprocess failures, timeouts, and unexpected exceptions.
- main() joins the thread on shutdown with timeout=10s.

Like the other daemon tests, time.sleep is mocked and _sleep_interruptible
is patched per-test so the suite runs in <1s.
"""

from __future__ import annotations

import sys
import threading
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
    """Cadence constants match the locked spec from issue #70."""
    main = _setup
    assert main.MACRO_SYNC_CADENCE_SECONDS == 3600
    assert main.MACRO_SYNC_FIRST_RUN_DELAY_SECONDS == 5 * 60
    assert main.MACRO_SYNC_SUBPROCESS_TIMEOUT == 300


# ---------- loop semantics ----------


def test_macro_sync_loop_skips_body_when_event_set(_setup, monkeypatch):
    """If _shutdown_event is set before the loop starts, no subprocess call."""
    main = _setup
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    main._shutdown_event.set()  # pre-shutdown
    main._macro_sync_loop()
    assert calls == []


def test_macro_sync_loop_first_call_after_5min_delay(_setup, monkeypatch):
    """Issue #70: first run waits MACRO_SYNC_FIRST_RUN_DELAY_SECONDS."""
    main = _setup
    sleep_calls: list[float] = []

    def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)
        # After the first (startup) sleep, let the loop proceed into
        # one subprocess iteration, then set the event so the next
        # cadence sleep exits cleanly.
        if len(sleep_calls) >= 1:
            main._shutdown_event.set()

    monkeypatch.setattr(main, "_sleep_interruptible", fake_sleep)
    main._macro_sync_loop()
    # First sleep is the startup delay (5 min = 300s).
    assert sleep_calls[0] == main.MACRO_SYNC_FIRST_RUN_DELAY_SECONDS


def test_macro_sync_loop_calls_subprocess_with_correct_script(_setup, monkeypatch):
    """Subprocess invocation targets scripts/run_macro_sync.py with no args."""
    main = _setup
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append(cmd)
        return MagicMock(returncode=0, stdout="macro_sync OK", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    # First sleep = 5-min startup delay; skip it.
    # Second sleep = 1h cadence; AFTER that sleep, set the event so the
    # loop exits. The subprocess is invoked once between the two sleeps.
    sleep_count = {"n": 0}

    def fake_sleep(secs: float) -> None:
        sleep_count["n"] += 1
        if sleep_count["n"] >= 2:
            main._shutdown_event.set()

    monkeypatch.setattr(main, "_sleep_interruptible", fake_sleep)
    main._macro_sync_loop()
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "python"
    assert cmd[1] == "scripts/run_macro_sync.py"


def test_macro_sync_loop_survives_subprocess_failure(_setup, monkeypatch):
    """Subprocess rc != 0 does NOT kill the loop — we log and continue."""
    main = _setup
    invocation_count = {"n": 0}

    def fake_run(cmd, **kw):
        invocation_count["n"] += 1
        if invocation_count["n"] == 1:
            return MagicMock(returncode=1, stdout="", stderr="boom")
        # Second call: succeed and exit.
        main._shutdown_event.set()
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(main, "_sleep_interruptible", lambda _: None)
    main._macro_sync_loop()
    assert invocation_count["n"] == 2  # loop ran twice


def test_macro_sync_loop_survives_timeout(_setup, monkeypatch):
    """subprocess.TimeoutExpired is caught and logged, loop continues."""
    main = _setup
    import subprocess

    invocation_count = {"n": 0}

    def fake_run(cmd, **kw):
        invocation_count["n"] += 1
        if invocation_count["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="run_macro_sync", timeout=300)
        main._shutdown_event.set()
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(main, "_sleep_interruptible", lambda _: None)
    main._macro_sync_loop()
    assert invocation_count["n"] == 2


def test_macro_sync_loop_survives_unexpected_exception(_setup, monkeypatch):
    """Any other exception is caught and the loop continues."""
    main = _setup
    invocation_count = {"n": 0}

    def fake_run(cmd, **kw):
        invocation_count["n"] += 1
        if invocation_count["n"] == 1:
            raise RuntimeError("totally unexpected")
        main._shutdown_event.set()
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(main, "_sleep_interruptible", lambda _: None)
    main._macro_sync_loop()
    assert invocation_count["n"] == 2


def test_macro_sync_loop_exits_on_shutdown_between_iterations(_setup, monkeypatch):
    """If _shutdown_event is set during the cadence sleep, the loop exits."""
    main = _setup
    invocation_count = {"n": 0}

    def fake_run(cmd, **kw):
        invocation_count["n"] += 1
        return MagicMock(returncode=0, stdout="ok", stderr="")

    def fake_sleep(secs: float) -> None:
        # After the first run, set the event so the loop exits cleanly.
        if invocation_count["n"] >= 1:
            main._shutdown_event.set()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(main, "_sleep_interruptible", fake_sleep)
    main._macro_sync_loop()
    assert invocation_count["n"] == 1  # exactly one subprocess call


def test_macro_sync_loop_subprocess_uses_correct_timeout(_setup, monkeypatch):
    """The subprocess call sets timeout=MACRO_SYNC_SUBPROCESS_TIMEOUT."""
    main = _setup
    captured_kwargs: list[dict] = []

    def fake_run(cmd, **kw):
        captured_kwargs.append(kw)
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    # Skip past both the startup sleep and the cadence sleep.
    sleep_count = {"n": 0}

    def fake_sleep(secs: float) -> None:
        sleep_count["n"] += 1
        if sleep_count["n"] >= 2:
            main._shutdown_event.set()

    monkeypatch.setattr(main, "_sleep_interruptible", fake_sleep)
    main._macro_sync_loop()
    assert captured_kwargs[0]["timeout"] == main.MACRO_SYNC_SUBPROCESS_TIMEOUT
    assert captured_kwargs[0]["cwd"] == "/app"


# ---------- main() integration ----------


def test_main_starts_macro_sync_daemon_thread(_setup, monkeypatch):
    """main() spawns the macro_sync daemon with the right name."""
    main = _setup
    started_threads: list[threading.Thread] = []
    orig_thread = main.threading.Thread

    def capturing_thread(*args, **kwargs):
        t = orig_thread(*args, **kwargs)
        started_threads.append(t)
        return t

    monkeypatch.setattr(main.threading, "Thread", capturing_thread)
    # Patch main() to stop after spawning the daemon thread.
    monkeypatch.setattr(main._shutdown_event, "is_set", lambda: True)
    try:
        main.main()
    except SystemExit:
        pass  # main() sys.exit(0) at the end
    thread_names = [t.name for t in started_threads]
    assert "alphard-macro-sync" in thread_names
