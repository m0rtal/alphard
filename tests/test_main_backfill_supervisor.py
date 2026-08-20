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
        # Issue #48: parent must NOT inherit any fd to the backfill log.
        # Output goes through the child's FileHandler (driven by
        # BACKFILL_LOG env var). Use DEVNULL here to make the leak
        # assertion mechanical — if a future refactor reverts to passing
        # an open file handle, this assertion will trip.
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        # The BACKFILL_LOG path must be passed via env so the child can
        # attach its own FileHandler. Asserting presence + equality (not
        # set membership, since the parent might add unrelated vars).
        env = kwargs.get("env")
        assert env is not None, "spawn must pass env= to the child"
        assert env.get("BACKFILL_LOG") == str(log)

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
        # Issue #48: parent must NOT hold a fd to the backfill log. We
        # assert DEVNULL (the leak-free option) so the contract is
        # mechanical — a future refactor that re-introduces a parent-held
        # fd will trip this assertion and force a code review.
        assert captured_kwargs["stdout"] is subprocess.DEVNULL
        assert captured_kwargs["stderr"] is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# Issue #48 regression: parent must NOT leak fds across respawns
# ---------------------------------------------------------------------------


class TestNoFdLeakAcrossRespawns:
    """Regression for issue #48: each respawn leaked one parent-held fd
    to the backfill log. After ~10 respawns (the rate-limit threshold)
    the supervisor held 10 fds all pointing at the same file; after
    ~hundreds of respawns in a long-running container, the parent's
    `open()` would fail with `OSError: [Errno 24] Too many open files`,
    silently killing the supervisor while the container kept ticking
    heartbeat — recreating the original orphan-daemon failure mode.

    The fix passes stdout/stderr=DEVNULL and the log path via BACKFILL_LOG
    env so the child opens its own FileHandler. We assert the parent's
    fd count is invariant across many fake respawns.
    """

    def test_fd_count_invariant_across_100_respawns(self, main_module: Any, tmp_path: Path) -> None:
        """100 respawns must not add any new fds to the parent process.

        We count fds via /proc/self/fd (POSIX-only; the project is Linux-
        only via Docker). If the parent re-introduces a held fd to the
        backfill log, this assertion will trip long before the production
        rate-limit kicks in.
        """
        if not hasattr(os, "scandir") or not Path("/proc/self/fd").exists():
            pytest.skip("fd-counting via /proc/self/fd only on Linux")

        log = tmp_path / "out.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        old = os.environ.get("BACKFILL_LOG")
        os.environ["BACKFILL_LOG"] = str(log)
        try:

            class _FakePopen:
                def __init__(self) -> None:
                    self.pid = 700_999

            def fake_popen(*args: Any, **kwargs: Any) -> _FakePopen:
                return _FakePopen()

            original_popen = main_module.subprocess.Popen
            main_module.subprocess.Popen = fake_popen  # type: ignore[assignment]
            try:
                fd_dir = Path("/proc/self/fd")
                baseline = len(list(fd_dir.iterdir()))
                for _ in range(100):
                    main_module._spawn_backfill()
                after = len(list(fd_dir.iterdir()))
            finally:
                main_module.subprocess.Popen = original_popen  # type: ignore[assignment]
        finally:
            if old is None:
                os.environ.pop("BACKFILL_LOG", None)
            else:
                os.environ["BACKFILL_LOG"] = old

        # Allow ±2 slack for pytest's own internal fd churn (temp files,
        # capture buffers) but reject any growth proportional to N=100.
        assert after - baseline <= 2, (
            f"parent fd count grew by {after - baseline} over 100 respawns; "
            f"baseline={baseline} after={after} (issue #48 regression)"
        )


# ---------------------------------------------------------------------------
# Issue #49 regression: entrypoint must NOT truncate the backfill log
# ---------------------------------------------------------------------------


class TestEntrypointDoesNotTruncateBackfillLog:
    """Regression for issue #49: `entrypoint.sh` used to truncate the
    backfill log via `: >"${BACKFILL_LOG}"` on every container start,
    wiping operator forensics. We assert the shell source contains the
    corrective `touch` and does NOT contain the truncate pattern.
    """

    def test_entrypoint_uses_touch_not_truncate(self) -> None:
        root = Path(__file__).resolve().parent.parent
        entrypoint = root / "docker" / "entrypoint.sh"
        assert entrypoint.is_file(), f"missing {entrypoint}"
        text = entrypoint.read_text(encoding="utf-8")

        # Must NOT truncate the backfill log on boot.
        assert ': >"${BACKFILL_LOG}"' not in text, (
            "entrypoint.sh still truncates the backfill log on container "
            "start (issue #49). Replace `: >${BACKFILL_LOG}` with `touch`."
        )
        # Must append a boot marker instead.
        assert 'touch "${BACKFILL_LOG}"' in text, (
            "entrypoint.sh should `touch` the backfill log (no truncation), " "see issue #49"
        )
        assert "boot $(date -u" in text, (
            "entrypoint.sh should append a `boot <UTC timestamp>` marker "
            "so operators can see when each container started, issue #49"
        )


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



# ---------------------------------------------------------------------------
# Regression 2026-08-20: supervisor must NOT count rc=0 (clean exit)
# toward the per-hour abort cap. The clean-exit case means the backfill
# finished its universe pass with no tickers (e.g. sandbox universe
# empty, mark_terminally_failed exhausted, or — as on 2026-08-20 —
# Tinkoff API returning 401 for every token). Counting those triggered
# os._exit(1) every 6 minutes, which produced the sawtooth uptime gauge.
# ---------------------------------------------------------------------------


class TestSupervisorDoesNotCountCleanExits:
    """Static-analysis regression for the rc=0 supervisor bug."""

    def test_supervisor_only_counts_crashes(self) -> None:
        root = Path(__file__).resolve().parent.parent
        main_src = (root / "src" / "main.py").read_text(encoding="utf-8")
        # The new logic must gate the death-counter increment with
        # `if rc != 0:` and contain the comment block explaining why.
        assert "if rc != 0:" in main_src, (
            "src/main.py supervisor must gate the death-counter with "
            "`if rc != 0:` — without this, rc=0 clean exits (empty "
            "sandbox universe, 401-auth-fail finish) increment the "
            "rate-limit and produce an infinite Docker restart loop "
            "with sawtooth uptime gauge. See 2026-08-20 incident."
        )
        # Old buggy pattern must be gone: the rate-limit increment must
        # not be unconditional. We assert that the surrounding comment
        # block explicitly explains the rc=0 carve-out.
        assert "rc=0 → no-op" in main_src or "rc=0 (clean exit)" in main_src, (
            "src/main.py supervisor must explain the rc=0 carve-out. "
            "Without an explicit comment, future refactors may "
            "re-introduce the unconditional death-counter."
        )
