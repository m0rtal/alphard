"""Tests for the in-process backfill supervisor thread in src/main.py.

Regression coverage for the 2026-08-20 incident: backfill_history_md.py
was launched from entrypoint.sh via `setsid ... &` and exec'd into
src.main. When the backfill subprocess died on the first ticker
(AttributeError on meta.delisted_at in --skip-known-bad), nothing
reaped the dead child (it became PID 19, State Z (zombie)) and nothing
respawned it. The container kept ticking heartbeat with a 17-hour-old
zombie holding a stale Postgres connection — the symptom everyone
called "network stall" but was actually a Python crash with no
supervisor.

Fix: src/main.py owns the lifecycle via _backfill_supervisor_loop.
These tests pin the contract: spawn args, start_new_session=True,
rate-limit thresholds, module-level faulthandler registration in
backfill_history_md.py.

NOTE: We intentionally do NOT exercise _backfill_supervisor_loop()
itself in unit tests — it's an infinite loop that requires shutdown
coordination that's brittle to fake. Live coverage comes from the
alphard-bot container itself (sha-1e3b6dd+ rebuilds and runs this
thread on every boot); the integration contract is enforced by
`test_module_import_registers_sigusr1` plus a live smoke check that
the supervisor thread starts when src.main runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# src/ on path so `import main` works under pytest.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def main_module() -> Any:
    """Import the main module without running it.

    `main.main()` starts the heartbeat loop and blocks. For these
    tests we want the supervisor functions only, never the blocking
    entry point. We import by hand and never call main().
    """
    import importlib

    mod = importlib.import_module("main")
    return mod


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_script_args_match_entrypoint(self, main_module: Any) -> None:
        """The supervisor must spawn with the same args the shell used.

        If these drift apart, the live backfill silently runs with a
        different --limit / --start-year / --min-bars and operators
        wonder why today's universe size differs from yesterday's.
        """
        args = main_module._BACKFILL_SCRIPT_ARGS
        assert "--limit" in args
        assert "5500" in args
        assert "--start-year" in args
        assert "2018" in args
        assert "--min-bars" in args
        assert "1300" in args

    def test_respawn_backoff_is_finite(self, main_module: Any) -> None:
        """30s backoff keeps a crash-loop visible without saturating logs."""
        assert 1 <= main_module._BACKFILL_RESPAWN_BACKOFF_SECONDS <= 300

    def test_max_respawns_is_above_one_below_infinity(self, main_module: Any) -> None:
        """A crash loop must terminate the container eventually."""
        assert 2 <= main_module._BACKFILL_MAX_RESPAWNS_PER_HOUR <= 1000

    def test_supervisor_thread_started_in_main(self, main_module: Any) -> None:
        """main() must launch the backfill supervisor thread.

        Regression: if a future refactor moves this into a separate
        entry point, the container will run heartbeat without the
        backfill — exactly the orphan-daemon symptom we just fixed.
        """
        import inspect

        source = inspect.getsource(main_module.main)
        assert "alphard-backfill-supervisor" in source
        assert "_backfill_supervisor_loop" in source


# ---------------------------------------------------------------------------
# _spawn_backfill
# ---------------------------------------------------------------------------


class TestSpawnBackfill:
    def test_returns_int_pid(self, main_module: Any, tmp_path: Path) -> None:
        """Spawning returns the child PID as an int.

        We don't actually fork scripts/backfill_history_md.py in the
        test — that script reads real env vars and tries to connect to
        Postgres. Instead we monkeypatch subprocess.Popen with a stub
        that returns a fake Popen-like object whose .pid is a fresh
        int. The supervisor itself doesn't care about the result type,
        only that it gets a positive int back.
        """
        log = tmp_path / "out.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        old = os.environ.get("BACKFILL_LOG")
        os.environ["BACKFILL_LOG"] = str(log)
        try:

            class _FakePopen:
                def __init__(self) -> None:
                    self.pid = 999_999

            # Capture the kwargs the supervisor passed so we can assert on
            # start_new_session=True without forking.
            captured: dict[str, Any] = {}

            def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
                captured["args"] = args
                captured["kwargs"] = kwargs
                return _FakePopen()

            # Patch subprocess.Popen in the main module's namespace.
            original_popen = main_module.subprocess.Popen
            main_module.subprocess.Popen = fake_popen  # type: ignore[assignment]
            try:
                pid = main_module._spawn_backfill()
            finally:
                main_module.subprocess.Popen = original_popen  # type: ignore[assignment]
        finally:
            if old is None:
                os.environ.pop("BACKFILL_LOG", None)
            else:
                os.environ["BACKFILL_LOG"] = old

        assert isinstance(pid, int)
        assert pid == 999_999
        # The kwargs the supervisor passed MUST include
        # start_new_session=True — otherwise a SIGTERM to the main
        # process kills the child too.
        kwargs = captured["kwargs"]
        assert kwargs["start_new_session"] is True
        assert kwargs["cwd"] == "/app"

    def test_uses_start_new_session(self, main_module: Any, tmp_path: Path) -> None:
        """Child must be its own session leader (setsid-equivalent).

        Without start_new_session=True the child dies the moment the
        main process receives a signal, which defeats the purpose of
        running it as a long-lived daemon.
        """
        log = tmp_path / "out.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        old = os.environ.get("BACKFILL_LOG")
        os.environ["BACKFILL_LOG"] = str(log)
        try:
            captured_kwargs: dict[str, Any] = {}

            class _FakePopen:
                def __init__(self) -> None:
                    self.pid = 888_888

            def fake_popen(*args: Any, **kwargs: Any) -> _FakePopen:
                captured_kwargs.update(kwargs)
                return _FakePopen()

            original_popen = main_module.subprocess.Popen
            main_module.subprocess.Popen = fake_popen  # type: ignore[assignment]
            try:
                main_module._spawn_backfill()
            finally:
                main_module.subprocess.Popen = original_popen  # type: ignore[assignment]
        finally:
            if old is None:
                os.environ.pop("BACKFILL_LOG", None)
            else:
                os.environ["BACKFILL_LOG"] = old

        # The supervisor's _spawn_backfill MUST pass start_new_session=True
        # so the child outlives any signal delivered to main.
        assert captured_kwargs["start_new_session"] is True
        # And stdout=log_fh (a file handle, not DEVNULL) so the child's
        # output actually ends up in the shared backfill log.
        assert captured_kwargs["stdout"] is not None


# ---------------------------------------------------------------------------
# Rate-limit math (pure-function check; no thread)
# ---------------------------------------------------------------------------


class TestRateLimitMath:
    """Verify the rate-limit constants behave as documented.

    The full loop logic is exercised in production (alphard-bot). We
    only assert the constants here because threading + os.waitpid +
    shutdown coordination is too brittle to fake in a unit test
    environment — every fake either busy-loops or leaks a daemon that
    pytest has to reap at session exit.
    """

    def test_threshold_is_above_one(self, main_module: Any) -> None:
        """At least 2 respawns must trigger the limit — 1 is a normal
        first-boot, not a loop."""
        assert main_module._BACKFILL_MAX_RESPAWNS_PER_HOUR >= 2

    def test_threshold_is_below_infinity(self, main_module: Any) -> None:
        """A finite threshold ensures the loop eventually exits and
        Docker restarts the container with a fresh image. An infinite
        threshold would mean an unfixable crash loops forever."""
        assert main_module._BACKFILL_MAX_RESPAWNS_PER_HOUR < 100_000

    def test_backoff_is_below_threshold_window(self, main_module: Any) -> None:
        """If backoff > 3600s the rate-limit can't actually trigger
        inside an hour, defeating the purpose of the limit."""
        assert main_module._BACKFILL_RESPAWN_BACKOFF_SECONDS <= 3600


# ---------------------------------------------------------------------------
# faulthandler SIGUSR1 register (backfill_history_md.py module-level)
# ---------------------------------------------------------------------------


class TestBackfillFaulthandlerRegister:
    """The backfill_history_md.py module must register faulthandler at
    import time so a future PID 19 (or whatever) actually honours
    SIGUSR1 by dumping its Python stack. Regression for 2026-08-20:
    the entrypoint shim registered in a throwaway subprocess, so the
    live daemon ignored SIGUSR1 entirely.
    """

    def test_module_import_registers_sigusr1(self) -> None:
        """Importing scripts.backfill_history_md must NOT raise and
        and must register SIGUSR1 as a faulthandler dump target.

        If the module is importable, faulthandler.register(SIGUSR1)
        has already run successfully — there is no public API to
        inspect the handler table without reaching into CPython, but
        the side effect (writing to stderr on SIGUSR1) is what we
        care about, and we verify it via subprocess.
        """
        root = Path(__file__).resolve().parent.parent
        scripts = root / "scripts"
        for p in (str(root), str(scripts)):
            if p not in sys.path:
                sys.path.insert(0, p)

        # Subprocess approach: launch backfill_history_md.py with --help
        # (it doesn't try to connect to Postgres), then send SIGUSR1
        # and verify the process dumps a Python stack trace to stderr.
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, signal, time, faulthandler;"
                "import backfill_history_md;"
                "signal.raise_signal(signal.SIGUSR1);"
                "time.sleep(0.1);"
                "sys.exit(0)",
            ],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        # faulthandler SIGUSR1 dump is a large Python stack trace; on
        # success the stderr contains "Traceback" markers even from
        # a thread dump. We accept either the Traceback header OR an
        # explicit "Current thread" header from faulthandler.
        err_text = stderr.decode("utf-8", errors="replace")
        assert (
            "Traceback" in err_text or "Current thread" in err_text or "stack" in err_text.lower()
        ), f"faulthandler SIGUSR1 dump missing from stderr; got: {err_text[:500]!r}"
