"""Rate limiting for one endpoint (A2 Part A): a fixed window per client key, in memory.

Single-instance by design: the counters live in this process, so two app replicas would each
allow the full limit. The key is the caller's `X-Client-ID` header if it sent one, else its IP
(see main.py), so the limit is shared by everything that presents the same client id.
"""
from __future__ import annotations

import math
import threading
import time


class FixedWindowLimiter:
    def __init__(self, limit: int, window_s: int):
        self.limit, self.window_s = limit, window_s
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}      # key -> (window start, calls in it)

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        """Returns (allowed, remaining in this window, seconds until the window resets)."""
        now = time.time() if now is None else now
        window_start = int(now // self.window_s) * self.window_s
        retry_after = max(1, math.ceil(window_start + self.window_s - now))
        with self._lock:
            start, n = self._windows.get(key, (window_start, 0))
            if start != window_start:                           # a new window: forget the old count
                n = 0
            if n >= self.limit:
                self._windows[key] = (window_start, n)
                return False, 0, retry_after
            self._windows[key] = (window_start, n + 1)
            if len(self._windows) > 10_000:                     # bounded memory: drop expired windows
                self._windows = {k: v for k, v in self._windows.items() if v[0] == window_start}
            return True, self.limit - n - 1, retry_after

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()
