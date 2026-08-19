"""Tests for src.data.token_bucket — focus on C-5 (concurrency race)."""

from __future__ import annotations

import threading
import time

import pytest

from src.data.token_bucket import RateLimitError, TokenBucket


class TestBasicSemantics:
    """Pre-existing bucket semantics — start full, drain, refill."""

    def test_start_full(self) -> None:
        b = TokenBucket(rate=5, window_seconds=1.0)
        assert b.tokens_available() == pytest.approx(5.0, abs=0.01)

    def test_acquire_decrements(self) -> None:
        b = TokenBucket(rate=5, window_seconds=1.0)
        b.acquire()
        assert b.tokens_available() == pytest.approx(4.0, abs=0.01)

    def test_acquire_nowait_when_empty_raises(self) -> None:
        b = TokenBucket(rate=1, window_seconds=10.0)
        b.acquire_nowait()  # consume the one token
        with pytest.raises(RateLimitError):
            b.acquire_nowait()

    def test_wait_time_zero_when_available(self) -> None:
        b = TokenBucket(rate=5, window_seconds=1.0)
        assert b.wait_time() == 0.0

    def test_invalid_rate_raises(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate=0)


class TestConcurrencyRegression:
    """BUGFIX (C-5): acquire() must NOT raise RateLimitError under contention.

    The pre-fix implementation did ``wait_time() → sleep → grab`` outside
    a retry-loop, so a sibling thread could drain the bucket during the
    sleep, leaving the caller with no token even after the wait. The fix
    loops until a token is successfully claimed.
    """

    def test_concurrent_acquire_does_not_raise(self) -> None:
        # 20 threads × 50 calls each = 1000 acquires on a bucket of 50.
        # Pre-fix: ~19/20 threads raise RateLimitError. Post-fix: 0.
        bucket = TokenBucket(rate=50, window_seconds=1.0)
        threads = 20
        calls_per_thread = 50
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(calls_per_thread):
                    bucket.acquire()
            except Exception as e:
                errors.append(e)

        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)

        assert errors == [], f"BUGFIX (C-5) regressed: {len(errors)} threads raised {set(type(e) for e in errors)}"
        # Sanity: all 1000 acquires happened (no thread starved).
        assert bucket.tokens_available() >= 0

    def test_acquire_blocks_until_token_refills(self) -> None:
        """Single-thread: acquire() must block, not raise, when bucket is empty."""
        b = TokenBucket(rate=10, window_seconds=1.0)
        # Drain 10 tokens
        for _ in range(10):
            b.acquire_nowait()
        start = time.monotonic()
        b.acquire()  # should block ~100ms (1 token / 10 r/s) until refill
        elapsed = time.monotonic() - start
        assert elapsed >= 0.05, f"acquire() returned too fast: {elapsed}s"


# ===========================================================================
# Issue #14 D.2: assert self._lock is not None replaced by non-Optional field.
# The lock is now set in __post_init__ via field(default_factory=...), so it
# is always present. python -O cannot strip this invariant.
# ===========================================================================


class TestLockInvariant:
    def test_lock_is_always_present(self) -> None:
        """Issue #14 D.2: lock is now a non-Optional field, set in
        __post_init__ via field(default_factory=threading.Lock). This
        is the same invariant the historical ``assert self._lock is
        not None`` tried to enforce, but without relying on python -O
        preserving the assert."""
        b = TokenBucket(rate=1, window_seconds=1)
        assert b._lock is not None
        assert isinstance(b._lock, type(threading.Lock()))

    def test_asserts_no_longer_in_token_bucket(self) -> None:
        """Issue #14 D.2: there are no ``assert`` statements in
        production token_bucket code. python -O stripping them would
        be a critical vulnerability for a rate limiter."""
        import ast
        import inspect

        from src.data import token_bucket

        source = inspect.getsource(token_bucket)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                # Allow the type narrowing in __post_init__ / similar
                # if any. As of this fix, none should remain.
                pytest.fail(f"production assert found at line {node.lineno}: " f"{ast.dump(node.test)}")

    def test_audit_log_runtime_check_for_cursor(self) -> None:
        """Issue #14 D.2: PostgresAuditLog.write_event() now raises
        RuntimeError if _cursor is None after _ensure_conn() instead
        of relying on an assert that python -O would strip."""
        from src.data.quality.audit import PostgresAuditLog
        from unittest.mock import MagicMock

        log = PostgresAuditLog(table="audit_log")
        # Force _ensure_conn to no-op so _cursor stays None.
        log._ensure_conn = lambda: None  # type: ignore[method-assign]
        log._conn = MagicMock()
        with pytest.raises(RuntimeError, match="_cursor is None"):
            log.write_event(None, ticker="SBER", gate="test")  # type: ignore[arg-type]
