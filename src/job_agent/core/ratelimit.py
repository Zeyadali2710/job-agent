from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Simple async rate limiter: `rate` requests per second, burst `capacity`.

    Lived in sources/base.py until Phase 10 needed the same limiter for the
    Discord webhook. sources.base still re-exports it, so adapter imports are
    unchanged.
    """

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate, self.capacity = rate, capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self.rate)
                self._tokens = 0
            else:
                self._tokens -= 1
