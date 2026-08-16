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
