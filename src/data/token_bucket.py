"""Token-bucket rate limiter — shared by all DataLoader subclasses.

Why a hand-rolled bucket, not ``ratelimit`` or ``aiolimiter``?
------------------------------------------------------------
1. Zero new dependencies. Phase 1.1 budget is stdlib + requests + pydantic.
2. The bucket is synchronous (loaders are sync), thread-safe (one bucket
   per provider so multiple threads can share a provider), and deterministic
   in tests (we can pre-fill or skip with ``_now``).
3. ``pydantic`` is overkill for a 6-field struct; the ABC is a plain class.

Semantics
---------
- Capacity = ``rate`` tokens. Tokens regenerate at ``rate / window_seconds``
  per second. A burst of ``rate`` calls is allowed, then calls are spaced
  out to one every ``window_seconds / rate`` seconds.
- ``acquire()`` blocks the calling thread until a token is available.
- ``acquire_nowait()`` raises ``RateLimitError`` instead of blocking —
  useful for tests and for the orchestrator's "fail-fast on cold start"
  path.
- ``wait_time()`` returns how long the caller would block — used by the
  CLI to print "rate-limited, retrying in 0.4s".
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class RateLimitError(Exception):
    """Raised by ``acquire_nowait`` when no token is available."""


@dataclass
class TokenBucket:
    """Coarse-grained synchronous token bucket.

    Parameters
    ----------
    rate:
        Maximum sustained operations per ``window_seconds``. Must be > 0.
    window_seconds:
        Window length for ``rate``. Must be > 0.
    capacity:
        Maximum burst size. Defaults to ``rate`` (one full window of burst).
    """

    rate: float
    window_seconds: float = 1.0
    capacity: float | None = None
    _tokens: float = 0.0
    _last_refill: float = 0.0
    _lock: threading.Lock | None = None

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError(f"rate must be > 0, got {self.rate}")
        if self.window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {self.window_seconds}")
        if self.capacity is None:
            self.capacity = float(self.rate)
        if self.capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {self.capacity}")
        # Start with a full bucket so the first burst is allowed.
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    # --- core API ---------------------------------------------------------

    def _refill_locked(self, now: float) -> None:
        """Add tokens proportional to elapsed time since last refill."""
        assert self._lock is not None
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        rate_per_sec = self.rate / self.window_seconds
        cap = float(self.capacity) if self.capacity is not None else float(self.rate)
        self._tokens = min(cap, self._tokens + elapsed * rate_per_sec)
        self._last_refill = now

    def wait_time(self, now: float | None = None) -> float:
        """Seconds until at least one token is available. 0 if available now."""
        assert self._lock is not None
        with self._lock:
            t = now if now is not None else time.monotonic()
            self._refill_locked(t)
            if self._tokens >= 1.0:
                return 0.0
            rate_per_sec = self.rate / self.window_seconds
            return (1.0 - self._tokens) / rate_per_sec

    def acquire(self, now: float | None = None) -> None:
        """Block until one token is available."""
        delay = self.wait_time(now=now)
        if delay > 0:
            # Sleep on the wall clock, not monotonic, since we already
            # measured in monotonic.
            time.sleep(delay)
        assert self._lock is not None
        with self._lock:
            t = now if now is not None else time.monotonic()
            self._refill_locked(t)
            if self._tokens < 1.0:
                # Defensive — should be impossible after the sleep.
                raise RateLimitError("token bucket drained during acquire")
            self._tokens -= 1.0

    def acquire_nowait(self, now: float | None = None) -> None:
        """Take a token or raise ``RateLimitError`` immediately."""
        assert self._lock is not None
        with self._lock:
            t = now if now is not None else time.monotonic()
            self._refill_locked(t)
            if self._tokens < 1.0:
                raise RateLimitError(f"no token available (rate={self.rate}/{self.window_seconds}s)")  # noqa: E501
            self._tokens -= 1.0

    # --- introspection (testing only) ------------------------------------

    def tokens_available(self, now: float | None = None) -> float:
        """How many tokens are currently in the bucket. Tests use this."""
        assert self._lock is not None
        with self._lock:
            t = now if now is not None else time.monotonic()
            self._refill_locked(t)
            return self._tokens


__all__ = ["TokenBucket", "RateLimitError"]
