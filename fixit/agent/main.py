"""FastAPI agent server: POST /tasks runs one agent task synchronously and returns the result.

Endpoints are plain `def` (not async) so FastAPI runs them in a thread pool and several
tasks can run concurrently (scripts/load.py --concurrency). Results live in an in-memory
dict; restart the container and history is gone (logs and metrics are the durable record).
"""
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from agent.llm import build_llm
from agent.loop import TaskResult, run_task

VERSION = "0.1.0"
WORKSPACE = Path(os.environ.get("FIXIT_WORKSPACE", "/workspace"))

app = FastAPI(title="fixit agent", version=VERSION)
LLM = build_llm()
TASKS: "OrderedDict[str, dict]" = OrderedDict()
MAX_HISTORY = 500


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
