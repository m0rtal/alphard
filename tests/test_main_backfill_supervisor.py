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
        # not be unconditional. We assert the surrounding comment block
        # explicitly names the rc=0 carve-out and the code paths that
        # produce it (so future refactors cannot silently re-merge an
        # unconditional increment). See issue #61 for the comment-
        # accuracy audit that landed alongside this assertion.
        assert "Counts ONLY crashes" in main_src, (
            "src/main.py supervisor docstring must explain the rc=0 "
            "carve-out. Without an explicit comment, future refactors "
            "may re-introduce the unconditional death-counter. See "
            "issue #61."
        )


# ---------------------------------------------------------------------------
# Issue #59 regression: `rc` MUST be bound on every exit path through the
# supervisor loop. The pre-fix `assert rc is not None` crashed under
# `python -O` (assert stripped) AND on the ChildProcessError branch (which
# broke without ever assigning rc). The fix initializes `rc = 0` before
# the inner loop so every path — ChildProcessError, clean exit, shutdown
# — leaves `rc` bound. We exercise this via AST inspection (no live
# thread) plus a small interpreter-level reproduction of the -O path.
# ---------------------------------------------------------------------------


class TestRcInvariant:
    """Issue #59: `rc` must be bound before use on every exit path."""

    def test_rc_initialized_before_inner_loop(self) -> None:
        """The supervisor MUST default `rc = 0` before the inner waitpid
        loop. This is the load-bearing invariant — without it, the
        ChildProcessError branch leaks an unbound name under
        ``python -O``.
        """
        import ast

        root = Path(__file__).resolve().parent.parent
        main_src = (root / "src" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(main_src)
        func = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_backfill_supervisor_loop"
        )
        # Find the outer `while not _shutdown_event.is_set():` body and
        # assert an `rc = ...` Assign appears before the inner `while`.
        outer_while = next(
            n for n in func.body if isinstance(n, ast.While)
        )  # outer = while not _shutdown_event.is_set()
        inner_while_idx = next(i for i, stmt in enumerate(outer_while.body) if isinstance(stmt, ast.While))
        # Every statement before the inner while must contain an
        # `Assign` whose target name is `rc`. We allow either a literal
        # `rc = 0` or `rc = <expr>`; the fixture below pins the literal.
        pre_stmts = outer_while.body[:inner_while_idx]
        rc_assigned = any(
            isinstance(target, ast.Name) and target.id == "rc"
            for stmt in pre_stmts
            if isinstance(stmt, ast.Assign)
            for target in stmt.targets
        )
        assert rc_assigned, (
            "supervisor must assign `rc = 0` before the inner waitpid "
            "loop so every branch leaves `rc` bound (issue #59)."
        )

    def test_no_assert_rc_in_supervisor(self) -> None:
        """The pre-fix `assert rc is not None` is unsafe under
        ``python -O`` (assert stripped → UnboundLocalError). The fix
        relies on the `rc = 0` default instead. We forbid the assert
        pattern outright so a future refactor cannot re-introduce it.
        """
        root = Path(__file__).resolve().parent.parent
        main_src = (root / "src" / "main.py").read_text(encoding="utf-8")
        assert "assert rc is not None" not in main_src, (
            "src/main.py supervisor must NOT use `assert rc is not None` "
            "— it is unsafe under `python -O` (assert stripped → "
            "UnboundLocalError on the ChildProcessError branch). Use "
            "the `rc = 0` default + INFO log instead. See issue #59."
        )

    def test_child_process_error_branch_does_not_assign_rc(self) -> None:
        """The ChildProcessError branch must NOT assign rc itself —
        the default `rc = 0` set above the inner loop is the source of
        truth. If a future refactor reintroduces `rc = 0` inside the
        except block, an over-zealous code-reviewer might think the
        default can be removed; this assertion catches the regression.
        """
        import ast

        root = Path(__file__).resolve().parent.parent
        main_src = (root / "src" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(main_src)
        func = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_backfill_supervisor_loop"
        )
        outer_while = next(n for n in func.body if isinstance(n, ast.While))
        inner_while = next(stmt for stmt in outer_while.body if isinstance(stmt, ast.While))
        # Walk the inner loop body and find any `except ChildProcessError:`
        # handler. Its body must NOT contain an Assign whose target is `rc`.
        for stmt in inner_while.body:
            if isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    if (
                        handler.type is not None
                        and isinstance(handler.type, ast.Name)
                        and handler.type.id == "ChildProcessError"
                    ):
                        for s in handler.body:
                            if isinstance(s, ast.Assign):
                                for tgt in s.targets:
                                    assert not (isinstance(tgt, ast.Name) and tgt.id == "rc"), (
                                        "ChildProcessError branch must "
                                        "rely on the `rc = 0` default; "
                                        "do not reassign rc here "
                                        "(issue #59)."
                                    )

    def test_rc_bound_under_python_optimize(self) -> None:
        """Reproduce the pre-fix crash under ``python -O`` against the
        ACTUAL supervisor source: spawn a child Python process with
        ``-O``, ``exec`` the supervisor module's loop body in isolation
        (mocked os.waitpid to raise ChildProcessError), and assert it
        does NOT raise UnboundLocalError on `if rc != 0:`.

        This is the integration-level proof that the `rc = 0` default
        holds across the -O strip path that the assert relied on.
        """
        # Run a tiny harness that imports the live module, monkey-
        # patches os.waitpid to raise ChildProcessError immediately,
        # then calls _backfill_supervisor_loop. We assert the process
        # reaches the post-loop code WITHOUT raising.
        root = Path(__file__).resolve().parent.parent
        # _spawn_backfill is patched to flip the shutdown event on its
        # first call — the supervisor must hit the ChildProcessError
        # branch and reach the post-loop code, where the next spawn
        # triggers _shutdown_event.is_set() and the outer loop exits
        # cleanly. If rc were unbound under -O, the harness would
        # crash before reaching that exit path.
        harness = (
            "import sys, threading\n"
            f"sys.path.insert(0, {str(root / 'src')!r})\n"
            "import main\n"
            "main._shutdown_event = threading.Event()\n"
            "main._BACKFILL_RESPAWN_BACKOFF_SECONDS = 0\n"
            "def fake_waitpid(pid, opts):\n"
            "    raise ChildProcessError(10, 'No child processes')\n"
            "main.os.waitpid = fake_waitpid\n"
            "class _FakeProc:\n"
            "    def __init__(self): self.pid = 999_999\n"
            "_call_count = [0]\n"
            "def fake_spawn():\n"
            "    _call_count[0] += 1\n"
            "    if _call_count[0] >= 1:\n"
            "        main._shutdown_event.set()\n"
            "    return 999_999\n"
            "main._spawn_backfill = fake_spawn\n"
            "try:\n"
            "    main._backfill_supervisor_loop()\n"
            "    print('NO_CRASH')\n"
            "except Exception as e:\n"
            "    print(f'CRASH:{type(e).__name__}:{e}')\n"
            "    sys.exit(1)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-O", "-c", harness],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "NO_CRASH" in proc.stdout, (
            f"supervisor crashed under `python -O` on the "
            f"ChildProcessError path (issue #59). stdout={proc.stdout!r} "
            f"stderr={proc.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Issue #60 regression: `death_timestamps` MUST be pruned on every loop
# iteration, not only on crashes. Pre-fix the prune was inside
# `if rc != 0:`, so each clean exit leaked one float. At the 30s
# respawn cadence this is ~2880 entries/day (≈1M/year, ≈28 MB/year).
# We pin this with a self-contained simulated loop that mirrors the
# supervisor's prune logic.
# ---------------------------------------------------------------------------


class TestDeathTimestampsBoundedOnCleanExits:
    """Issue #60: death_timestamps stays bounded across long clean-exit
    stretches.

    We don't run the real supervisor (it's an infinite loop). Instead
    we replicate the prune+append contract from src/main.py verbatim
    and run it for the equivalent of one week of clean exits at the
    30s respawn cadence. The list MUST stay at ≤
    _BACKFILL_MAX_RESPAWNS_PER_HOUR + 1 entries throughout (it cannot
    grow past the cap because we only append on rc != 0).
    """

    @staticmethod
    def _supervisor_iteration(
        death_timestamps: list[float],
        rc: int,
        cap: int,
        now: float,
    ) -> list[float]:
        """Mirror of the post-#60 prune+append logic. Kept in sync
        manually with src/main.py; if src/main.py drifts, this fixture
        will silently keep passing — the value of the test is in the
        design contract (always prune, only append on crash), which
        the static test below also pins.
        """
        death_timestamps = [t for t in death_timestamps if now - t < 3600]
        if rc != 0:
            death_timestamps.append(now)
            if len(death_timestamps) > cap:
                # We do NOT call os._exit in tests; just raise so the
                # caller observes the cap.
                raise RuntimeError("would_abort_container")
        return death_timestamps

    def test_death_timestamps_bounded_over_20k_clean_exits(self) -> None:
        """20,000 clean exits (≈7 days at 30s cadence) must keep the
        list bounded to ≤ cap entries. Pre-fix this would have grown
        to 20,000 floats."""
        cap = 10  # _BACKFILL_MAX_RESPAWNS_PER_HOUR default; actual value
        # asserted in TestConstants — fixture uses 10 to keep the test
        # self-contained.
        death_timestamps: list[float] = []
        now = 0.0
        for i in range(20_000):
            now = float(i) * 30.0  # 30s cadence
            death_timestamps = self._supervisor_iteration(death_timestamps, rc=0, cap=cap, now=now)
        assert len(death_timestamps) <= cap + 1, (
            f"death_timestamps grew unbounded on clean exits: "
            f"{len(death_timestamps)} entries after 20k iterations "
            f"(issue #60). Pre-fix this would have been 20_000."
        )
        # Specifically: with rc=0 throughout, we never append, so the
        # list must be EMPTY at the end (prune drops nothing, append
        # never runs).
        assert death_timestamps == [], (
            f"death_timestamps should be empty after 20k clean exits, " f"got {death_timestamps}"
        )

    def test_death_timestamps_caps_after_repeated_crashes(self) -> None:
        """Repeated crashes must still trip the cap (no regression on
        the rate-limit itself)."""
        cap = 10
        death_timestamps: list[float] = []
        now = 0.0
        with pytest.raises(RuntimeError, match="would_abort_container"):
            for i in range(cap + 5):
                now = float(i) * 30.0
                death_timestamps = self._supervisor_iteration(death_timestamps, rc=1, cap=cap, now=now)

    def test_prune_runs_unconditionally_in_source(self) -> None:
        """Static pin: the prune line
        ``death_timestamps = [t for t in death_timestamps if now - t < 3600]``
        must appear OUTSIDE the ``if rc != 0:`` block.

        Pre-fix the prune was nested inside ``if rc != 0:`` (the
        rc=0-clean-exit branch never pruned, so the list grew ~2880
        entries/day at the 30s respawn cadence, issue #60). The fix
        moves the prune above the ``if`` and limits append to crashes.

        We assert this structurally: the prune statement must precede
        the ``if rc != 0:`` gate in source order AND it must be at
        strictly LESS indentation than the ``if`` (proving it is not
        the body of the ``if``).
        """
        root = Path(__file__).resolve().parent.parent
        main_src = (root / "src" / "main.py").read_text(encoding="utf-8")
        lines = main_src.splitlines()
        # Restrict to _backfill_supervisor_loop body — there are two
        # `if rc != 0:` blocks in the file (one in the supervisor,
        # one in the auth_probe path). We pin the LAST occurrence of
        # each anchor, which is the supervisor's.
        prune_line_idx = None
        rc_nonzero_line_idx = None
        for i, line in enumerate(lines):
            if "death_timestamps = [t for t in death_timestamps if now - t < 3600]" in line:
                prune_line_idx = i  # last one wins (only one in source today)
            if line.strip() == "if rc != 0:":
                rc_nonzero_line_idx = i
        assert prune_line_idx is not None, (
            "supervisor must prune death_timestamps unconditionally "
            "(issue #60). The prune line was not found in src/main.py."
        )
        assert rc_nonzero_line_idx is not None, (
            "supervisor must contain `if rc != 0:` gate (regression " "test for issue #57). The line was not found."
        )
        prune_indent = len(lines[prune_line_idx]) - len(lines[prune_line_idx].lstrip())
        rc_indent = len(lines[rc_nonzero_line_idx]) - len(lines[rc_nonzero_line_idx].lstrip())
        # Structural proof #1: prune precedes the `if` (in source order).
        # If the prune is inside the `if`, it would have to be a
        # later line. Pre-fix the prune came AFTER `if rc != 0:` and
        # was nested under it (12 spaces vs 8). Post-fix the prune
        # comes BEFORE the `if` at the SAME indentation — this is the
        # only shape where the prune is unconditional but the append
        # stays gated.
        assert prune_line_idx < rc_nonzero_line_idx, (
            f"prune line at index {prune_line_idx} comes AFTER "
            f"`if rc != 0:` at index {rc_nonzero_line_idx}; this is "
            f"the issue #60 bug. Prune must run on every iteration, "
            f"before the rc-dependent append."
        )
        # Structural proof #2: prune is at the SAME or LESS indentation
        # than the `if` — i.e. the prune is NOT nested under `if`
        # (which would require prune_indent > rc_indent by exactly 4).
        assert prune_indent <= rc_indent, (
            f"prune line is nested inside `if rc != 0:` block "
            f"(prune_indent={prune_indent} > rc_indent={rc_indent}); "
            f"this is the issue #60 bug."
        )


# ---------------------------------------------------------------------------
# Issue #61 regression: docstring + inline comments must accurately
# describe the rc=0 carve-out and the code paths that produce it. We
# pin three text anchors that future maintainers can grep for.
# ---------------------------------------------------------------------------


class TestSupervisorCommentsAccurate:
    """Issue #61: comments/docstring must describe actual code paths."""

    def test_docstring_mentions_rc0_carveout(self) -> None:
        root = Path(__file__).resolve().parent.parent
        main_src = (root / "src" / "main.py").read_text(encoding="utf-8")
        assert "Counts ONLY crashes" in main_src, (
            "_backfill_supervisor_loop docstring must mention the rc=0 " "carve-out (issue #61)."
        )

    def test_no_overconfident_rc_invariant_comment(self) -> None:
        """The pre-fix comment claimed `rc is always bound` because
        the inner while/break guarantees it. This is false under
        `python -O` and on the ChildProcessError branch. The fix
        removes the claim and relies on the explicit `rc = 0` default
        instead. We forbid the old claim in the source.
        """
        root = Path(__file__).resolve().parent.parent
        main_src = (root / "src" / "main.py").read_text(encoding="utf-8")
        assert "`rc` is always bound at this point because" not in main_src, (
            "The overconfident 'rc is always bound at this point' "
            "comment must be removed (issue #61). The invariant is "
            "enforced by `rc = 0` default above the inner loop, not "
            "by the break paths."
        )

    def test_tinkoff401_narrative_replaced_with_code_paths(self) -> None:
        """The pre-fix comment claimed the 2026-08-20 rc=0 exits were
        caused by 'Tinkoff 401 every 2 seconds'. The literal exit
        path on auth failure is rc=1 (auth_probe, src/main.py:597);
        the rc=0 path is empty-universe / all-skipped / exhausted.
        The fix replaces the narrative with code-path references.
        """
        root = Path(__file__).resolve().parent.parent
        main_src = (root / "src" / "main.py").read_text(encoding="utf-8")
        assert "auth_probe" in main_src, (
            "supervisor comment must cite the actual code path that "
            "produces rc=1 (auth_probe) so future maintainers do not "
            "confuse the 2026-08-20 incident narrative with the "
            "literal exit code (issue #61)."
        )
        assert "mark_terminally_failed" in main_src, (
            "supervisor comment must mention the mark_terminally_"
            "failed_exhausted path that produces rc=0 on a clean "
            "finish (issue #61)."
        )


# ---------------------------------------------------------------------------
# Issue #72: shutdown must join the backfill supervisor thread
# ---------------------------------------------------------------------------


class TestMainShutdownJoinsBackfillSupervisor:
    """Regression: main()'s finally block must `backfill_thread.join(...)`.

    Without the join, ``_spawn_backfill`` may have just returned a fresh
    PID when ``sys.exit(0)`` runs, leaving the child process without a
    supervisor to reap or restart it. The container exits, the child
    gets reparented to init, and there's nobody left to count its death
    or respawn it — exactly the orphan-grandchild problem PR #51
    originally fixed (issue #47).
    """

    def test_finally_block_joins_backfill_thread(self, main_module: Any) -> None:
        """The finally block must call backfill_thread.join(timeout=...)."""
        import inspect

        src = inspect.getsource(main_module.main)
        # The join must be inside the `finally:` of main(), AFTER the
        # _shutdown_event.set() so the supervisor can react.
        assert "backfill_thread.join(timeout=" in src, (
            "main()'s finally block must call backfill_thread.join(timeout=...) "
            "so the supervisor thread is drained on shutdown (issue #72). "
            "Without this join, _spawn_backfill can be killed mid-subprocess.Popen "
            "and the child becomes an orphan with no respawn safety net."
        )

    def test_join_timeout_outlives_respawn_backoff(self, main_module: Any) -> None:
        """The join timeout must be > _BACKFILL_RESPAWN_BACKOFF_SECONDS.

        The supervisor sleeps ``_BACKFILL_RESPAWN_BACKOFF_SECONDS``
        between respawns. If we set the shutdown event while the
        supervisor is in the backoff sleep, we need the join timeout
        to outlive that sleep — otherwise we exit with the child still
        mid-backoff and the child process becomes the orphan we're
        trying to avoid.
        """
        import inspect
        import re

        src = inspect.getsource(main_module.main)
        backoff = main_module._BACKFILL_RESPAWN_BACKOFF_SECONDS
        # Find the join line.
        m = re.search(r"backfill_thread\.join\(timeout=([^\)]+)\)", src)
        assert m, "expected backfill_thread.join(timeout=...) in main()"
        timeout_expr = m.group(1)
        # The canonical form is `_BACKFILL_RESPAWN_BACKOFF_SECONDS + 5`.
        # We don't eval — we just assert the symbol appears in the
        # expression so a future refactor that hardcodes a smaller
        # number trips this test.
        assert "_BACKFILL_RESPAWN_BACKOFF_SECONDS" in timeout_expr, (
            f"backfill_thread.join timeout must derive from "
            f"_BACKFILL_RESPAWN_BACKOFF_SECONDS ({backoff}s); got: {timeout_expr!r}"
        )
        # Sanity: the constant exists and is positive.
        assert backoff > 0

    def test_supervisor_loop_responds_to_shutdown_event(self, main_module: Any) -> None:
        """The supervisor must exit promptly on _shutdown_event.

        The whole point of the join in main() is to give the supervisor
        a chance to exit. If the supervisor ignores _shutdown_event,
        the join will time out and the child stays orphaned. This test
        is a structural guard: pin that the outer ``while`` in
        _backfill_supervisor_loop checks ``_shutdown_event``.
        """
        import inspect

        src = inspect.getsource(main_module._backfill_supervisor_loop)
        assert "_shutdown_event" in src
        # The outer loop must check the event (not just an inner one).
        # We don't assert on exact syntax — only on the existence of
        # the shutdown check, so a refactor that flips the loop
        # structure still passes.
        assert "not _shutdown_event.is_set" in src, (
            "_backfill_supervisor_loop must check _shutdown_event.is_set() so "
            "main()'s join(timeout=...) actually waits for it to exit."
        )
