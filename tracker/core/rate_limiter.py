"""
rate_limiter.py — Sliding-window rate limiter (token bucket)

Enforces MAX_REQUESTS_PER_MINUTE as a hard ceiling.
This is a failsafe that operates independently of jitter logic —
even if a bug causes a tight loop, this prevents flooding Blinkit.

Usage:
    limiter = RateLimiter(max_per_minute=20)
    await limiter.acquire()   # blocks until a slot is available
    # ... make your request
"""

import asyncio
import time
from collections import deque
from typing import Deque


class RateLimiter:
    """
    Sliding-window rate limiter.

    Tracks timestamps of the last N requests within a 60-second window.
    If the window is full, blocks until the oldest request falls out.
    """

    def __init__(self, max_per_minute: int = 20):
        self.max_per_minute = max_per_minute
        self._window: Deque[float] = deque()
        self._lock = asyncio.Lock()
        self._total_requests = 0
        self._total_sleeps = 0

    async def acquire(self) -> None:
        """Block until a request slot is available within the rate limit window."""
        async with self._lock:
            now = time.monotonic()
            window_start = now - 60.0

            # Drop timestamps older than the 60-second window
            while self._window and self._window[0] < window_start:
                self._window.popleft()

            if len(self._window) >= self.max_per_minute:
                # Window is full — wait until the oldest slot expires
                oldest = self._window[0]
                sleep_for = (oldest + 60.0) - time.monotonic()
                if sleep_for > 0:
                    self._total_sleeps += 1
                    await asyncio.sleep(sleep_for + 0.05)  # small buffer

            self._window.append(time.monotonic())
            self._total_requests += 1

    @property
    def current_rate(self) -> int:
        """Number of requests in the current 60-second window."""
        now = time.monotonic()
        window_start = now - 60.0
        return sum(1 for ts in self._window if ts >= window_start)

    @property
    def stats(self) -> dict:
        return {
            "total_requests": self._total_requests,
            "total_rate_limit_sleeps": self._total_sleeps,
            "current_window_count": self.current_rate,
            "max_per_minute": self.max_per_minute,
        }
