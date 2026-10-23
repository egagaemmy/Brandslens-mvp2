"""services/ratelimit.py. A small in-memory rate limiter for the public endpoints (tracking, demo login, code checks).

It is deliberately simple: a sliding window per key (usually the visitor's IP address, which is used only to count and
is never stored). It lives in one server process, which is enough to stop a script hammering an endpoint; it is not a
replacement for a CDN's protection."""
from __future__ import annotations
import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float):
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque] = {}
        self._last_sweep = time.monotonic()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if now - self._last_sweep > self.window:      # drop idle keys so the table cannot grow forever
            self._last_sweep = now
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                self._hits.pop(k, None)
        return True
