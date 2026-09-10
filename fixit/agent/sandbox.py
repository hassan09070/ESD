"""Sandbox: isolated working copy of the target repo + pytest runner with a hard timeout.

Each task gets /tmp/fixit/<run_id>/ (override root with FIXIT_SANDBOX_ROOT). The agent's
tools only ever touch that copy. On success `commit()` copies changed files back to the
original repo; `cleanup()` removes the copy. No Docker-in-Docker: pytest runs as a plain
subprocess inside the agent container.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agent import metrics

TEST_TIMEOUT_S = 30
IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc", ".venv", "node_modules")
_IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}


def sandbox_root() -> Path:
    return Path(os.environ.get("FIXIT_SANDBOX_ROOT", "/tmp/fixit"))


@dataclass
class TestRunResult:
    result: str  # passed | failed | timeout
    output: str
    passed: int
    failed: int
    duration_s: float

    @property
    def marker(self) -> str:
        return {"passed": "PASSED", "failed": "FAILED", "timeout": "TIMEOUT"}[self.result]


def _count(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


class Sandbox:
    def __init__(self, run_id: str, repo_path: str | os.PathLike):
        self.run_id = run_id
        self.source = Path(repo_path).resolve()
        self.workdir = sandbox_root() / run_id

    def create(self) -> Path:
        if not self.source.is_dir():
            raise FileNotFoundError(f"repo not found: {self.source}")
        shutil.copytree(self.source, self.workdir, ignore=IGNORE, dirs_exist_ok=True)
        return self.workdir

    def run_pytest(self, timeout_s: float = TEST_TIMEOUT_S) -> TestRunResult:
        start = time.perf_counter()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PY_COLORS": "0"}
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"],
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired:
            metrics.SANDBOX_TEST_RUNS.labels(result="timeout").inc()
            return TestRunResult("timeout", f"TIMEOUT: tests exceeded {timeout_s:.0f}s", 0, 0, time.perf_counter() - start)
        output = (proc.stdout or "") + (proc.stderr or "")
        passed = _count(r"(\d+) passed", output)
        failed = _count(r"(\d+) failed", output) + _count(r"(\d+) error", output)
        result = "passed" if proc.returncode == 0 else "failed"
        metrics.SANDBOX_TEST_RUNS.labels(result=result).inc()
        return TestRunResult(result, output, passed, failed, time.perf_counter() - start)

    def _files(self) -> list[Path]:
        out = []
        for p in self.workdir.rglob("*"):
            if p.is_file() and not (_IGNORED_PARTS & set(p.relative_to(self.workdir).parts)) and p.suffix != ".pyc":
                out.append(p)
        return out

    def commit(self) -> list[str]:
        """Copy new/changed files from the working copy back into the original repo."""
        changed: list[str] = []
        for p in self._files():
            rel = p.relative_to(self.workdir)
            dest = self.source / rel
            if dest.exists() and dest.read_bytes() == p.read_bytes():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            changed.append(str(rel))
        return changed

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)
