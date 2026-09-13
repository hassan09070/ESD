"""Session lifecycle: the state machine, the idle reaper, orphan cleanup and the resource
sampler. This is the only module that changes session state; the HTTP layer and the
WebSocket bridge call into it.

    created --request--> queued --slot--> spawning --ok--> running <--attach/detach--> detached
       |                   | timeout          | error            | idle / `exit` / DELETE / OOM
       +-> (dropped)       +-> created        +-> created        +-------> reaped

A session is an anonymous user: one token, at most one sandbox, ever (after `reaped` the
browser creates a fresh session). That keeps the state machine small and the metrics honest.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable
from uuid import uuid4

import structlog

from api import metrics
from api.config import Settings
from api.docker_client import SESSION_LABEL, DockerBackend, Sandbox, StatsSample, new_name
from api.faults import Fault, start_hogs, stop_hogs
from api.pool import Pool, QueueCancelled, QueueTimeout
from api.warm_pool import WarmPool

log = structlog.get_logger()


class State(str, Enum):
    created = "created"
    queued = "queued"
    spawning = "spawning"
    running = "running"
    detached = "detached"
    reaped = "reaped"


ALLOWED = {
    State.created: {State.queued, State.reaped},
    State.queued: {State.spawning, State.created, State.reaped},
    State.spawning: {State.running, State.created, State.reaped},
    State.running: {State.detached, State.running, State.reaped},
    State.detached: {State.running, State.reaped},
    State.reaped: set(),
}


class IllegalTransition(Exception):
    pass


class SessionError(Exception):
    """Raised by request_sandbox; `status` is the HTTP status the API should return."""

    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.status = status
        self.error = error
        self.extra = extra


@dataclass
class Session:
    session_id: str
    token: str
    created_at: float                     # monotonic
    last_activity: float
    state: State = State.created
    sandbox: Sandbox | None = None
    spawn_source: str | None = None
    sandbox_started: float | None = None
    ws_count: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    commands: int = 0
    reap_reason: str | None = None
    _reaping: bool = field(default=False, repr=False)
    _waiter: asyncio.Future | None = field(default=None, repr=False)

    @property
    def user_id(self) -> str:             # anonymous: the session *is* the user
        return self.session_id

    def public(self, now: float, queue_position: int | None = None) -> dict:
        return {
            "queue_position": queue_position,
            "session_id": self.session_id, "state": self.state.value, "source": self.spawn_source,
            "sandbox_id": self.sandbox.short_id if self.sandbox else None,
            "uptime_s": round(now - self.sandbox_started, 1) if self.sandbox_started else None,
            "idle_s": round(now - self.last_activity, 1),
            "bytes_in": self.bytes_in, "bytes_out": self.bytes_out, "commands": self.commands,
            "attached": self.ws_count > 0, "reap_reason": self.reap_reason,
        }


@dataclass
class SpawnResult:
    source: str
    spawn_ms: int
    queue_ms: int


class SessionManager:
    def __init__(self, settings: Settings, backend: DockerBackend, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.backend = backend
        self.clock = clock
        self.fault = Fault.from_settings(settings)
        self.pool = Pool(settings.pool_size)
        self.warm_pool = WarmPool(self.fault.warm_pool_size(settings.warm_pool_size), backend, spawn=self._spawn_blocking)
        self.sessions: dict[str, Session] = {}
        self.hogs: list[Sandbox] = []
        self._tasks: list[asyncio.Task] = []
        self._prev_stats: dict[str, StatsSample] = {}

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> dict:
        orphans = await self.cleanup_orphans(reason="orphan")
        self.hogs = await start_hogs(self.fault, self.backend)
        await self.warm_pool.fill()
        self._tasks = [
            asyncio.create_task(self._loop(self.reaper_tick, self.settings.reaper_interval_s), name="reaper"),
            asyncio.create_task(self._loop(self.stats_tick, self.settings.stats_interval_s), name="stats"),
        ]
        return {"orphans_removed": orphans, "hogs": len(self.hogs), "warm": len(self.warm_pool.ready)}

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        for s in list(self.sessions.values()):
            if s.sandbox:
                await self.reap(s, "admin")
        await self.warm_pool.drain()
        await stop_hogs(self.hogs, self.backend)
        await self.cleanup_orphans(reason="admin")

    async def _loop(self, tick, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("error", msg=f"{tick.__name__} failed", exc_type=type(e).__name__, exc_message=str(e)[:200], exc_info=True)

    async def cleanup_orphans(self, reason: str) -> int:
        """Remove every container labelled termlab.sandbox=1 that no live session owns.
        Answers 'what if the api restarts': nothing leaks, at the cost of killing sessions."""
        owned = {s.sandbox.container_id for s in self.sessions.values() if s.sandbox}
        owned |= {sb.container_id for sb in self.warm_pool.ready} | {sb.container_id for sb in self.hogs}
        leftovers = [sb for sb in await asyncio.to_thread(self.backend.list_sandboxes) if sb.container_id not in owned]
        for sb in leftovers:
            await asyncio.to_thread(self.backend.remove, sb)
            metrics.SANDBOXES_REAPED.labels(reason=reason).inc()
        if leftovers:
            log.warning("orphan_cleanup", msg=f"removed {len(leftovers)} leftover sandbox containers", removed=len(leftovers),
                        names=[sb.name for sb in leftovers][:20], reason=reason)
        return len(leftovers)

    # ------------------------------------------------------------------ sessions
    def create_session(self, request_id: str | None = None) -> Session:
        now = self.clock()
        s = Session(session_id=uuid4().hex[:12], token=secrets.token_urlsafe(24), created_at=now, last_activity=now)
        self.sessions[s.session_id] = s
        metrics.record_demo_request(request_id or s.session_id)
        log.info("session_created", msg="anonymous session created", session_id=s.session_id, user_id=s.user_id)
        return s

    def get(self, session_id: str, token: str | None = None) -> Session:
        s = self.sessions.get(session_id)
        if s is None:
            raise SessionError(404, "unknown_session")
        if token is not None and not secrets.compare_digest(token, s.token):
            raise SessionError(403, "bad_token")
        return s

    def _transition(self, s: Session, new: State) -> None:
        if new not in ALLOWED[s.state]:
            raise IllegalTransition(f"{s.session_id}: {s.state.value} -> {new.value}")
        s.state = new

    def touch(self, s: Session) -> None:
        s.last_activity = self.clock()

    def _spawn_blocking(self, name: str, labels: dict, warm: bool) -> Sandbox:
        self.fault.before_spawn()
        return self.backend.spawn(name, labels, warm=warm)

    async def request_sandbox(self, s: Session) -> SpawnResult:
        if s.sandbox is not None:
            raise SessionError(409, "already_has_sandbox")
        if s.state != State.created:
            raise SessionError(409, "session_not_reusable", state=s.state.value)
        self._transition(s, State.queued)
        t_queue = time.perf_counter()
        try:
            waited = await self.pool.acquire(self.settings.queue_timeout_s, register=lambda f: setattr(s, "_waiter", f))
        except QueueCancelled:
            s._waiter = None
            log.info("queue_cancelled", msg="sandbox request cancelled while queued", session_id=s.session_id, user_id=s.user_id)
            raise SessionError(409, "cancelled", state=s.state.value) from None
        except QueueTimeout as e:
            s._waiter = None
            outcome = "pool_full" if e.immediate else "queued_timeout"
            metrics.SESSIONS_STARTED.labels(outcome=outcome).inc()
            self._transition(s, State.created)
            log.warning("limit_hit", msg=f"no sandbox slot ({outcome})", session_id=s.session_id, user_id=s.user_id, outcome=outcome,
                        waited_ms=int(e.waited_s * 1000), pool_capacity=self.pool.capacity, queue_length=len(self.pool.waiters))
            raise SessionError(503, outcome, waited_ms=int(e.waited_s * 1000)) from None
        s._waiter = None
        queue_ms = int(waited * 1000)
        log.log(30 if waited > 5 else 20, "queue_wait", msg=f"slot acquired after {queue_ms} ms", session_id=s.session_id,
                queue_ms=queue_ms, queue_length=len(self.pool.waiters), pool_free=self.pool.free)
        self._transition(s, State.spawning)
        t0 = time.perf_counter()
        try:
            sb = self.warm_pool.claim()
            source = "warm" if sb else "cold"
            if sb is None:
                sb = await asyncio.to_thread(self._spawn_blocking, new_name("sbx"), {SESSION_LABEL: s.session_id}, False)
        except Exception as e:  # noqa: BLE001
            self.pool.release()
            metrics.SESSIONS_STARTED.labels(outcome="error").inc()
            self._transition(s, State.created)
            log.error("sandbox_spawn", msg="sandbox spawn failed", session_id=s.session_id, user_id=s.user_id, source="cold",
                      exc_type=type(e).__name__, exc_message=str(e)[:200], exc_info=True)
            raise SessionError(500, "spawn_failed", exc_type=type(e).__name__) from e
        spawn_s = time.perf_counter() - t0
        metrics.SANDBOX_SPAWN.labels(source=source).observe(spawn_s)
        s.sandbox, s.spawn_source, s.sandbox_started = sb, source, self.clock()
        self.touch(s)
        self._transition(s, State.running)
        metrics.SANDBOXES_ACTIVE.set(self._active_count())
        metrics.SESSIONS_STARTED.labels(outcome="ok").inc()
        log.info("sandbox_spawn", msg=f"sandbox ready from {source} pool in {int(spawn_s * 1000)} ms", session_id=s.session_id,
                 user_id=s.user_id, sandbox_id=sb.short_id, source=source, spawn_ms=int(spawn_s * 1000), queue_ms=queue_ms,
                 image=self.settings.sandbox_image, cold_delay_ms=int(self.fault.spawn_delay_s * 1000) if source == "cold" else 0)
        return SpawnResult(source=source, spawn_ms=int(spawn_s * 1000), queue_ms=queue_ms)

    def _active_count(self) -> int:
        return sum(1 for s in self.sessions.values() if s.sandbox is not None)

    def on_attach(self, s: Session, cols: int, rows: int) -> None:
        s.ws_count += 1
        self.touch(s)
        if s.state == State.detached:
            self._transition(s, State.running)
        metrics.USERS_CONNECTED.inc()
        log.info("ws_attach", msg="terminal attached", session_id=s.session_id, user_id=s.user_id,
                 sandbox_id=s.sandbox.short_id if s.sandbox else None, cols=cols, rows=rows, ws_count=s.ws_count)

    def on_detach(self, s: Session, reason: str, duration_ms: int, bytes_in: int, bytes_out: int, commands: int) -> None:
        s.ws_count = max(0, s.ws_count - 1)
        metrics.USERS_CONNECTED.dec()
        if s.state == State.running and s.ws_count == 0:
            self._transition(s, State.detached)
        log.info("ws_detach", msg=f"terminal detached ({reason})", session_id=s.session_id, user_id=s.user_id, reason=reason,
                 duration_ms=duration_ms, bytes_in=bytes_in, bytes_out=bytes_out, commands=commands, ws_count=s.ws_count)

    async def reap(self, s: Session, reason: str) -> bool:
        """Destroy the sandbox and close the session. Idempotent and safe under concurrency."""
        if s._reaping or s.state == State.reaped:
            return False
        s._reaping = True
        if s.state == State.queued:
            self.pool.cancel(s._waiter)          # the waiting request_sandbox() raises QueueCancelled
        sb = s.sandbox
        try:
            if sb is not None:
                await asyncio.to_thread(self.backend.remove, sb)
        finally:
            lifetime = (self.clock() - s.sandbox_started) if s.sandbox_started else 0.0
            s.sandbox = None
            s.reap_reason = reason
            self._transition(s, State.reaped)
            if sb is not None:
                self.pool.release()
                metrics.SANDBOXES_REAPED.labels(reason=reason).inc()
                metrics.SANDBOX_SECONDS.inc(lifetime)
                metrics.SESSION_DURATION.observe(lifetime)
                metrics.SANDBOXES_ACTIVE.set(self._active_count())
                self._prev_stats.pop(sb.container_id, None)
                log.info("sandbox_reaped", msg=f"sandbox destroyed ({reason}) after {lifetime:.0f} s", session_id=s.session_id,
                         user_id=s.user_id, sandbox_id=sb.short_id, reason=reason, lifetime_s=round(lifetime, 1),
                         bytes_in=s.bytes_in, bytes_out=s.bytes_out, commands=s.commands, source=s.spawn_source)
        return True

    # ------------------------------------------------------------------ background
    async def reaper_tick(self) -> None:
        now = self.clock()
        for s in list(self.sessions.values()):
            idle = now - s.last_activity
            if s.sandbox is not None:
                if idle > self.settings.idle_timeout_s:
                    await self.reap(s, "idle")
                    continue
                state = await asyncio.to_thread(self.backend.inspect, s.sandbox)
                if not state.running:
                    await self.reap(s, "oom" if state.oom_killed else "user_exit")
            elif idle > self.settings.idle_timeout_s and s.state in (State.created, State.reaped):
                self.sessions.pop(s.session_id, None)     # forget sandbox-less sessions (E.2 creates hundreds)

    async def stats_tick(self) -> None:
        sandboxes = [s.sandbox for s in self.sessions.values() if s.sandbox is not None]
        t0 = time.perf_counter()
        samples = await asyncio.gather(*(asyncio.to_thread(self.backend.stats, sb) for sb in sandboxes), return_exceptions=True)
        cores: list[float] = []
        mems: list[int] = []
        for sb, sample in zip(sandboxes, samples):
            if isinstance(sample, BaseException):
                continue
            prev = self._prev_stats.get(sb.container_id)
            self._prev_stats[sb.container_id] = sample
            if prev and sample.system_ns > prev.system_ns:
                cores.append(max(0.0, (sample.cpu_total_ns - prev.cpu_total_ns) / (sample.system_ns - prev.system_ns) * sample.online_cpus))
            mems.append(max(0, sample.mem_usage))
            metrics.SANDBOX_MEM_RATIO.observe(min(1.0, max(0, sample.mem_usage) / max(1, sample.mem_limit)))
        metrics.SANDBOX_CPU_SUM.set(sum(cores))
        metrics.SANDBOX_CPU_MAX.set(max(cores, default=0.0))
        metrics.SANDBOX_MEM_SUM.set(sum(mems))
        metrics.SANDBOX_MEM_MAX.set(max(mems, default=0))
        metrics.STATS_SAMPLE_SECONDS.observe(time.perf_counter() - t0)
        log.debug("stats_sample", msg="resource sample", n=len(sandboxes), duration_ms=int((time.perf_counter() - t0) * 1000))

    def describe(self, s: Session) -> dict:
        return s.public(self.clock(), queue_position=self.pool.position(s._waiter))

    def pool_status(self) -> dict:
        return {"capacity": self.pool.capacity, "active": self.pool.active, "free": self.pool.free,
                "queue": len(self.pool.waiters), "warm": len(self.warm_pool.ready), "fault": self.fault.mode}
