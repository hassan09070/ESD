"""Keeps N sandboxes pre-created so a user normally gets a *warm* one in ~50 ms instead of
paying Docker's create+start (~0.5-1.5 s). Warm containers carry `termlab.warm=1` and no
session id (labels are immutable); the SessionManager owns the mapping in memory.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Callable

import structlog

from api import metrics
from api.docker_client import DockerBackend, Sandbox, new_name

log = structlog.get_logger()


class WarmPool:
    def __init__(self, size: int, backend: DockerBackend, spawn: Callable[..., Sandbox] | None = None):
        self.size = size
        self.backend = backend
        self._spawn = spawn or (lambda name, labels, warm: backend.spawn(name, labels, warm=warm))
        self.ready: deque[Sandbox] = deque()
        self._filling = asyncio.Lock()
        self._in_flight = 0
        self._publish()

    def _publish(self) -> None:
        metrics.WARM_POOL_SIZE.set(len(self.ready))

    async def fill(self) -> int:
        """Top up to `size`. Serialised so a burst of claims does not over-fill."""
        created = 0
        async with self._filling:
            while len(self.ready) + self._in_flight < self.size:
                self._in_flight += 1
                try:
                    sb = await asyncio.to_thread(self._spawn, new_name("sbx-warm"), {}, True)
                except Exception as e:  # noqa: BLE001
                    log.error("error", msg="warm pool spawn failed", exc_type=type(e).__name__, exc_message=str(e)[:200])
                    break
                finally:
                    self._in_flight -= 1
                self.ready.append(sb)
                created += 1
                self._publish()
        if created:
            log.info("warm_pool", msg=f"warm pool filled (+{created})", action="fill", size=len(self.ready))
        return created

    def claim(self) -> Sandbox | None:
        if not self.ready:
            return None
        sb = self.ready.popleft()
        self._publish()
        log.info("warm_pool", msg="warm sandbox claimed", action="claim", size=len(self.ready), sandbox_id=sb.short_id)
        if self.size:
            asyncio.get_running_loop().create_task(self.fill())
        return sb

    async def drain(self) -> int:
        n = 0
        while self.ready:
            sb = self.ready.popleft()
            await asyncio.to_thread(self.backend.remove, sb)
            n += 1
        self._publish()
        if n:
            log.info("warm_pool", msg=f"warm pool drained ({n})", action="drain", size=0)
        return n
