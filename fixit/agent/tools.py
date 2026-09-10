"""The agent's three tools: read_file, write_file, run_tests.

Every tool returns a string (the LLM only ever sees strings). Paths are resolved
relative to the task's sandbox copy; absolute paths and `..` are refused. `dispatch()`
is what the loop calls and is where per-tool metrics/logging hang (Phase 2/3).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import structlog

from agent import metrics

log = structlog.get_logger()
from agent.sandbox import Sandbox, TestRunResult

TAIL_LINES = 60

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "read_file",
        "description": "Read a text file from the repository. Path is relative to the repo root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path, e.g. src/app.py"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Overwrite a file in the repository with new content. Path is relative to the repo root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "Full new file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run `python -m pytest -q` in the repository (30 s timeout). Returns the tail of the output and a PASSED/FAILED/TIMEOUT marker.",
        "input_schema": {"type": "object", "properties": {}},
    },
]
TOOL_NAMES = [t["name"] for t in TOOL_SCHEMAS]


class ToolError(Exception):
    pass


class Toolbox:
    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.root = sandbox.workdir.resolve()
        self.last_test_result: TestRunResult | None = None

    # -- path safety ---------------------------------------------------------
    def _resolve(self, path: str) -> Path:
        if not path or not isinstance(path, str):
            raise ToolError("path must be a non-empty string")
        if os.path.isabs(path) or ".." in Path(path).parts:
            raise ToolError(f"refused: path must be relative and inside the repo: {path!r}")
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root):
            raise ToolError(f"refused: path escapes the repo: {path!r}")
        return p

    # -- tools ---------------------------------------------------------------
    def read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"file not found: {path}")
        return p.read_text()

    def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode()
        p.write_bytes(data)
        return f"OK: wrote {len(data)} bytes to {path}"

    def run_tests(self) -> str:
        r = self.sandbox.run_pytest()
        self.last_test_result = r
        tail = "\n".join(r.output.strip().splitlines()[-TAIL_LINES:])
        return f"{tail}\n\n=== {r.marker} ({r.passed} passed, {r.failed} failed, {r.duration_s:.1f}s) ==="

    # -- dispatch ------------------------------------------------------------
    def dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, str]:
        """Run a tool by name. Returns (result_text, status) where status is ok|error.
        Errors are returned to the LLM as text, never raised.
        Records fixit_tool_exec_seconds{tool} (Summary) and logs a `tool_call` event. For
        write_file only the path and byte count are logged, never the content."""
        start = time.perf_counter()
        text, status = self._dispatch(name, args)
        duration = time.perf_counter() - start
        metrics.TOOL_EXEC_SECONDS.labels(tool=name).observe(duration)
        extra: dict[str, Any] = {}
        if name in ("read_file", "write_file"):
            extra["path"] = str(args.get("path", ""))[:200]
        if name == "write_file":
            extra["bytes"] = len(str(args.get("content", "")).encode())
        if name == "run_tests" and self.last_test_result is not None:
            extra["test_result"] = self.last_test_result.result
        log.info("tool_call", msg="tool executed", tool=name, duration_ms=int(duration * 1000), status=status, **extra)
        return text, status

    def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, str]:
        try:
            if name == "read_file":
                return self.read_file(args.get("path", "")), "ok"
            if name == "write_file":
                return self.write_file(args.get("path", ""), args.get("content", "")), "ok"
            if name == "run_tests":
                return self.run_tests(), "ok"
            return f"ERROR: unknown tool {name!r}", "error"
        except ToolError as e:
            return f"ERROR: {e}", "error"
        except Exception as e:  # noqa: BLE001 - tool failures must not kill the loop
            return f"ERROR: {type(e).__name__}: {e}", "error"
