"""FastAPI agent server: POST /tasks runs one agent task synchronously and returns the result.

Endpoints are plain `def` (not async) so FastAPI runs them in a thread pool and several
tasks can run concurrently (scripts/load.py --concurrency). Results live in an in-memory
dict; restart the container and history is gone (logs and metrics are the durable record).
"""
from __future__ import annotations

import os
import time
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from agent import metrics
from agent.llm import build_llm
from agent.logging_config import setup_logging
from agent.loop import TaskResult, run_task

setup_logging()
log = structlog.get_logger()

VERSION = "0.1.0"
WORKSPACE = Path(os.environ.get("FIXIT_WORKSPACE", "/workspace"))

app = FastAPI(title="fixit agent", version=VERSION)
LLM = build_llm()
metrics.set_build_info(VERSION, LLM.provider, LLM.mode)
log.info("startup", msg="agent configured", version=VERSION, llm_backend=LLM.provider, fault_mode=LLM.mode,
         demo_cardinality=metrics.DEMO_CARDINALITY_ENABLED)
TASKS: "OrderedDict[str, dict]" = OrderedDict()
MAX_HISTORY = 500


def route_template(path: str) -> str:
    """Collapse /tasks/<id> to /tasks/{run_id} so `path` stays low-cardinality."""
    if path.startswith("/tasks/"):
        return "/tasks/{run_id}"
    if path in ("/tasks", "/health", "/metrics"):
        return path
    return "other"


@app.middleware("http")
async def observe_http(request: Request, call_next):
    """Binds request_id (X-Request-ID header or generated), records
    fixit_http_requests_total / fixit_http_request_duration_seconds and logs one
    `http_request` line per request. /metrics is skipped (Prometheus' own scrapes would
    drown the request panels)."""
    request_id = request.headers.get("x-request-id") or uuid4().hex[:12]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter()
    status = 500
    run_id = None
    try:
        response = await call_next(request)
        status = response.status_code
        run_id = response.headers.get("x-run-id")
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
            log.info("http_request", msg=f"{request.method} {request.url.path} -> {status}", method=request.method,
                     path=request.url.path, status_code=status, status="ok" if status < 500 else "error",
                     duration_ms=int(duration * 1000), request_id=request_id, **({"run_id": run_id} if run_id else {}))
        structlog.contextvars.clear_contextvars()


@app.get("/metrics")
def prometheus_metrics() -> Response:
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    repo: str = Field(default=str(WORKSPACE), description="Path to the repo as seen by the agent container")


def resolve_repo(repo: str) -> Path:
    p = Path(repo)
    if p.is_dir():
        return p
    # The CLI is on the host; the container only sees ./sample_repo mounted at /workspace.
    if p.name == WORKSPACE.name or p.name == "sample_repo":
        if WORKSPACE.is_dir():
            return WORKSPACE
    raise HTTPException(status_code=400, detail=f"repo path not visible to the agent: {repo!r} (docker compose mounts ./sample_repo at {WORKSPACE})")


@app.post("/tasks")
def create_task(req: TaskRequest, response: Response) -> dict:
    repo = resolve_repo(req.repo)
    run_id = uuid4().hex[:12]
    result: TaskResult = run_task(run_id, req.task, str(repo), LLM)
    body = result.to_dict()
    TASKS[run_id] = body
    while len(TASKS) > MAX_HISTORY:
        TASKS.popitem(last=False)
    response.headers["X-Run-ID"] = run_id
    if result.outcome == "error":
        response.status_code = 503 if result.error_type == "LLMError" else 500
    return body


@app.get("/tasks")
def list_tasks(limit: int = 20) -> list[dict]:
    return list(TASKS.values())[-limit:][::-1]


@app.get("/tasks/{run_id}")
def get_task(run_id: str) -> dict:
    if run_id not in TASKS:
        raise HTTPException(status_code=404, detail=f"unknown run_id {run_id}")
    return TASKS[run_id]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm": LLM.provider, "fault": LLM.mode, "version": VERSION}
