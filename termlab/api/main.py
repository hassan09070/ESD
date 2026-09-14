"""FastAPI control plane: anonymous sessions, sandbox requests through the pool, and the
WebSocket terminal. `create_app()` builds an app around any DockerBackend (tests pass the
FakeDocker); the module-level `app` is what uvicorn / the container run.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import structlog
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect

from api import metrics
from api.bridge import Bridge
from api.config import Settings
from api.docker_client import DockerBackend, RealDocker
from api.logging_config import setup_logging
from api.sessions import Session, SessionError, SessionManager

VERSION = "0.1.0"
STATIC = Path(__file__).parent / "static"
COOKIE = "termlab_token"

setup_logging()
log = structlog.get_logger()


def route_template(path: str) -> str:
    """Collapse /sessions/<id>[/sandbox] so the `path` label stays low-cardinality."""
    parts = path.strip("/").split("/")
    if parts[0] == "sessions" and len(parts) == 2:
        return "/sessions/{id}"
    if parts[0] == "sessions" and len(parts) == 3 and parts[2] == "sandbox":
        return "/sessions/{id}/sandbox"
    if path in ("/sessions", "/pool", "/health", "/metrics", "/"):
        return path
    return "other"


def create_app(settings: Settings | None = None, backend: DockerBackend | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        be = backend or RealDocker(settings)
        manager = SessionManager(settings, be)
        app.state.settings, app.state.manager = settings, manager
        metrics.set_build_info(VERSION, settings.fault)
        image_ok = await asyncio.to_thread(be.image_ready)
        if not image_ok:
            log.error("image_missing", msg=f"sandbox image {settings.sandbox_image} not found; build it (docker compose up --build)",
                      image=settings.sandbox_image)
        info = await manager.startup()
        log.info("startup", msg="termlab api configured", version=VERSION, fault_mode=settings.fault, pool_capacity=settings.pool_size,
                 warm_pool_size=manager.warm_pool.size, idle_timeout_s=settings.idle_timeout_s, queue_timeout_s=settings.queue_timeout_s,
                 demo_cardinality=metrics.DEMO_CARDINALITY_ENABLED, image=settings.sandbox_image, image_present=image_ok, **info)
        try:
            yield
        finally:
            await manager.shutdown()
            log.info("shutdown", msg="termlab api stopped")

    app = FastAPI(title="termlab", version=VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def observe_http(request: Request, call_next):
        """Binds request_id (X-Request-ID header or generated), records
        termlab_http_requests_total / termlab_http_request_duration_seconds and logs one
        `http_request` line per request. /metrics is skipped (Prometheus' own scrapes would
        drown the request panels); /health is counted in the metrics but not logged (the
        container healthcheck hits it every 10 s). WebSocket connections never pass through here."""
        request_id = (request.headers.get("x-request-id") or uuid4().hex[:12])[:64]   # bounded: it becomes a log field (and a label in E.2)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        status = 500
        session_id = None
        try:
            response = await call_next(request)
            status = response.status_code
            session_id = response.headers.get("x-session-id")
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception as e:  # noqa: BLE001
            log.error("error", msg="unhandled exception in request", exc_type=type(e).__name__, exc_message=str(e)[:200],
                      method=request.method, path=request.url.path, exc_info=True)
            raise
        finally:
            duration = time.perf_counter() - start
            path = route_template(request.url.path)
            if path != "/metrics":
                metrics.HTTP_REQUESTS.labels(method=request.method, path=path, status=str(status)).inc()
                metrics.HTTP_DURATION.labels(method=request.method, path=path).observe(duration)
            if path not in ("/metrics", "/health"):
                log.info("http_request", msg=f"{request.method} {request.url.path} -> {status}", method=request.method,
                         path=request.url.path, route=path, status_code=status, status="ok" if status < 500 else "error",
                         duration_ms=int(duration * 1000), request_id=request_id, **({"session_id": session_id} if session_id else {}))
            structlog.contextvars.clear_contextvars()

    @app.exception_handler(SessionError)
    async def session_error(_request: Request, e: SessionError):
        return JSONResponse(status_code=e.status, content={"error": e.error, **e.extra})

    def token_of(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.query_params.get("token") or request.cookies.get(COOKIE)

    def authed(request: Request, session_id: str) -> Session:
        manager: SessionManager = request.app.state.manager
        token = token_of(request)
        if token is None:
            raise SessionError(401, "missing_token")
        s = manager.get(session_id, token)
        structlog.contextvars.bind_contextvars(session_id=s.session_id)
        return s

    # ------------------------------------------------------------------ routes
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    @app.get("/health")
    async def health(request: Request) -> dict:
        manager: SessionManager = request.app.state.manager
        try:
            image = await asyncio.to_thread(manager.backend.image_ready)
            docker_ok = True
        except Exception:  # noqa: BLE001
            image, docker_ok = False, False
        return {"status": "ok" if docker_ok else "degraded", "fault": manager.fault.mode, "version": VERSION,
                "docker": docker_ok, "image": image, **manager.pool_status()}

    @app.get("/pool")
    def pool(request: Request) -> dict:
        return request.app.state.manager.pool_status()

    @app.post("/sessions", status_code=201)
    def create_session(request: Request, response: Response) -> dict:
        manager: SessionManager = request.app.state.manager
        s = manager.create_session(request_id=request.headers.get("x-request-id"))
        response.headers["X-Session-ID"] = s.session_id
        response.set_cookie(COOKIE, s.token, httponly=True, samesite="strict")
        return {"session_id": s.session_id, "token": s.token, "state": s.state.value}

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str, request: Request, response: Response) -> dict:
        s = authed(request, session_id)
        response.headers["X-Session-ID"] = s.session_id
        return request.app.state.manager.describe(s)

    @app.post("/sessions/{session_id}/sandbox")
    async def request_sandbox(session_id: str, request: Request, response: Response) -> dict:
        s = authed(request, session_id)
        response.headers["X-Session-ID"] = s.session_id
        r = await request.app.state.manager.request_sandbox(s)
        return {"session_id": s.session_id, "state": s.state.value, "source": r.source, "spawn_ms": r.spawn_ms, "queue_ms": r.queue_ms}

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request, response: Response) -> dict:
        s = authed(request, session_id)
        response.headers["X-Session-ID"] = s.session_id
        reaped = await request.app.state.manager.reap(s, "user_exit")
        return {"session_id": s.session_id, "state": s.state.value, "reaped": reaped}

    @app.websocket("/ws/{session_id}")
    async def terminal(ws: WebSocket, session_id: str, token: str | None = None, cols: int = 80, rows: int = 24):
        manager: SessionManager = ws.app.state.manager
        try:
            s = manager.get(session_id, token or ws.cookies.get(COOKIE) or "")
        except SessionError as e:
            await ws.close(code=4401 if e.status in (401, 403) else 4404)
            return
        if s.sandbox is None:
            await ws.close(code=4409)
            return
        await ws.accept()
        structlog.contextvars.bind_contextvars(session_id=s.session_id, request_id=uuid4().hex[:12])
        try:
            await Bridge(ws, manager, s, cols, rows).run()
        except WebSocketDisconnect:
            pass
        finally:
            structlog.contextvars.clear_contextvars()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    return app


app = create_app()
