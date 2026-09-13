"""WebSocket <-> `docker exec` PTY bridge.

Wire protocol (browser side is api/static/index.html):
  browser -> server   binary frame  = stdin bytes
                      text frame    = {"type":"resize","cols":C,"rows":R}
  server  -> browser  binary frame  = PTY output bytes
                      text frame    = {"type":"exit"} when the shell ends

Everything runs on the event loop: the exec socket is non-blocking and read/written with
loop.sock_recv / loop.sock_sendall, so 10 attached terminals cost 10 sockets, not 10
threads. Backpressure is natural: the next PTY chunk is not read until the previous
WebSocket send has completed. The only Docker API calls (exec_create, resize, HUP) go
through asyncio.to_thread.

The self-explored metric lives here: termlab_terminal_roundtrip_seconds is the time from
forwarding a stdin chunk until the *next* output chunk arrives - the server's view of
"typing lag". One probe is outstanding at a time and a probe older than 2 s is discarded
(the user may be inside a program that does not echo).
"""
from __future__ import annotations

import asyncio
import json
import socket
import time

import structlog
from starlette.websockets import WebSocket, WebSocketDisconnect

from api import metrics
from api.sessions import Session, SessionManager

log = structlog.get_logger()
READ_CHUNK = 32768
ROUNDTRIP_MAX_S = 2.0


class Bridge:
    def __init__(self, ws: WebSocket, manager: SessionManager, session: Session, cols: int, rows: int):
        self.ws, self.manager, self.session = ws, manager, session
        self.cols, self.rows = cols, rows
        self.bytes_in = self.bytes_out = self.commands = 0
        self.rt_pending: float | None = None
        self.exec_id: str | None = None
        self.raw: socket.socket | None = None
        self.reason = "client_close"
        self.shell_exited = False
        self._resize_task: asyncio.Task | None = None

    async def run(self) -> None:
        backend = self.manager.backend
        started = time.perf_counter()
        self.exec_id, self.raw = await asyncio.to_thread(backend.exec_create, self.session.sandbox, self.cols, self.rows)
        self.raw.setblocking(False)
        self.manager.on_attach(self.session, self.cols, self.rows)
        tasks = [asyncio.create_task(self._pty_to_ws(), name="pty->ws"), asyncio.create_task(self._ws_to_pty(), name="ws->pty")]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError, OSError)):
                    self.reason = "error"
                    log.error("error", msg="bridge task failed", exc_type=type(exc).__name__, exc_message=str(exc)[:200])
        finally:
            try:
                self.raw.close()
            except OSError:
                pass
            self.session.bytes_in += self.bytes_in
            self.session.bytes_out += self.bytes_out
            self.session.commands += self.commands
            self.manager.on_detach(self.session, self.reason, int((time.perf_counter() - started) * 1000),
                                   self.bytes_in, self.bytes_out, self.commands)
            if self.shell_exited:
                await self.manager.reap(self.session, "user_exit")
            elif self.session.sandbox is not None:
                await asyncio.to_thread(backend.signal_shells, self.session.sandbox)   # a closed tab must not leave a bash behind

    async def _pty_to_ws(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.sock_recv(self.raw, READ_CHUNK)
            if not chunk:
                self.shell_exited = True
                self.reason = "shell_exit"
                try:
                    await self.ws.send_text(json.dumps({"type": "exit"}))
                except Exception:  # noqa: BLE001
                    pass
                return
            n = len(chunk)
            self.bytes_out += n
            metrics.TERMINAL_BYTES.labels(direction="out").inc(n)
            metrics.WS_MESSAGES.labels(direction="out").inc()
            if self.rt_pending is not None:
                elapsed = time.perf_counter() - self.rt_pending
                self.rt_pending = None
                if elapsed <= ROUNDTRIP_MAX_S:
                    metrics.TERMINAL_ROUNDTRIP.observe(elapsed)
            await self.ws.send_bytes(chunk)

    async def _ws_to_pty(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                self.reason = "client_close"
                return
            data = msg.get("bytes")
            if data:
                n = len(data)
                self.bytes_in += n
                self.commands += data.count(b"\r")
                metrics.TERMINAL_BYTES.labels(direction="in").inc(n)
                metrics.WS_MESSAGES.labels(direction="in").inc()
                metrics.COMMANDS.inc(data.count(b"\r"))
                self.manager.touch(self.session)
                if self.rt_pending is None or time.perf_counter() - self.rt_pending > ROUNDTRIP_MAX_S:
                    self.rt_pending = time.perf_counter()
                await loop.sock_sendall(self.raw, data)
                continue
            text = msg.get("text")
            if text:
                try:
                    ctl = json.loads(text)
                except ValueError:
                    continue
                if ctl.get("type") == "resize":
                    self._schedule_resize(int(ctl.get("cols", 80)), int(ctl.get("rows", 24)))

    def _schedule_resize(self, cols: int, rows: int) -> None:
        """Coalesce bursts of resize events: the latest size wins, one Docker call in flight."""
        self.cols, self.rows = max(2, min(cols, 500)), max(2, min(rows, 200))
        if self._resize_task is None or self._resize_task.done():
            self._resize_task = asyncio.create_task(self._apply_resize())

    async def _apply_resize(self) -> None:
        await asyncio.sleep(0.05)
        try:
            await asyncio.to_thread(self.manager.backend.exec_resize, self.exec_id, self.cols, self.rows)
        except Exception as e:  # noqa: BLE001
            log.warning("resize_failed", msg="exec resize failed", exc_type=type(e).__name__, exc_message=str(e)[:100])
