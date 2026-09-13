"""Sandbox capacity with an observable FIFO wait queue.

A plain asyncio.Semaphore would work but hides the queue; here `free`, the waiters deque
and the wait time are explicit so termlab_pool_free / termlab_queue_length /
termlab_queue_wait_seconds describe exactly what a user experiences when the pool is full.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

from api import metrics


class QueueTimeout(Exception):
    def __init__(self, waited_s: float, immediate: bool):
        super().__init__(f"no sandbox slot after {waited_s:.1f}s")
        self.waited_s = waited_s
        self.immediate = immediate     # True when queue_timeout_s == 0 (outcome pool_full)


class Pool:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.free = capacity
        self.waiters: deque[asyncio.Future] = deque()
        self._publish()

    @property
    def active(self) -> int:
        return self.capacity - self.free

    def _publish(self) -> None:
        metrics.POOL_FREE.set(self.free)
        metrics.QUEUE_LENGTH.set(len(self.waiters))

    async def acquire(self, timeout: float) -> float:
        """Take a slot; returns seconds waited. Raises QueueTimeout."""
        if self.free > 0 and not self.waiters:
            self.free -= 1
            self._publish()
            metrics.QUEUE_WAIT.observe(0.0)
            return 0.0
        if timeout <= 0:
            raise QueueTimeout(0.0, immediate=True)
        fut = asyncio.get_running_loop().create_future()
        self.waiters.append(fut)
        self._publish()
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            if fut.done() and not fut.cancelled():
                pass                                    # slot was handed over in the same tick
            else:
                try:
                    self.waiters.remove(fut)
                except ValueError:
                    pass
                self._publish()
                raise QueueTimeout(time.monotonic() - t0, immediate=False) from None
        waited = time.monotonic() - t0
        metrics.QUEUE_WAIT.observe(waited)
        self._publish()
        return waited

    def release(self) -> None:
        """Hand the slot straight to the oldest live waiter, else return it to `free`."""
        while self.waiters:
            fut = self.waiters.popleft()
            if not fut.done():
                fut.set_result(True)
                self._publish()
                return
        self.free += 1
        self._publish()
