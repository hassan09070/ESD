"""ALL Prometheus metric definitions for fixit live here. No other module defines metrics.

Naming follows Prometheus conventions: `fixit_` prefix, base units (seconds), `_total` on
counters. Labels are deliberately low-cardinality (method/path/status/outcome/tool/provider);
run_id, file paths and task text are NEVER labels - they belong in logs. The one exception is
the throwaway `fixit_demo_requests_total` used by the Part E.2 cardinality experiment, which
only gets a `run_id` label when FIXIT_DEMO_CARDINALITY=1.
"""
from __future__ import annotations

import os

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, Summary, generate_latest

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
TASK_DURATION_BUCKETS = (1, 2, 5, 10, 20, 30, 60, 120)
ITERATION_BUCKETS = (1, 2, 3, 5, 8, 10)

# ---------------------------------------------------------------- application metrics
HTTP_REQUESTS = Counter(
    "fixit_http_requests_total", "HTTP requests handled by the agent server", ["method", "path", "status"]
)
HTTP_DURATION = Histogram(
    "fixit_http_request_duration_seconds", "HTTP request latency", ["method", "path"], buckets=LATENCY_BUCKETS
)
LLM_REQUESTS = Counter("fixit_llm_requests_total", "LLM calls by provider and status (ok/error/retry)", ["provider", "status"])
LLM_DURATION = Histogram(
    "fixit_llm_request_duration_seconds", "LLM call latency (per attempt)", ["provider"], buckets=LATENCY_BUCKETS
)
TOOL_CALLS = Counter("fixit_tool_calls_total", "Tool invocations by the agent", ["tool", "status"])
TOOL_EXEC_SECONDS = Summary("fixit_tool_exec_seconds", "Tool execution time (Summary: sum/count only)", ["tool"])

# ---------------------------------------------------------------- business metrics
TASKS_TOTAL = Counter("fixit_tasks_total", "Agent tasks finished, by outcome", ["outcome"])
TASKS_IN_PROGRESS = Gauge("fixit_tasks_in_progress", "Agent tasks currently running")
TASK_DURATION = Histogram(
    "fixit_task_duration_seconds", "End-to-end task duration", ["outcome"], buckets=TASK_DURATION_BUCKETS
)
TASK_ITERATIONS = Histogram(
    "fixit_task_iterations", "Agent loop iterations per task (self-explored: shows thrashing)", ["outcome"], buckets=ITERATION_BUCKETS
)
LLM_TOKENS = Counter("fixit_llm_tokens_total", "LLM tokens consumed", ["direction"])
LLM_COST = Counter("fixit_llm_cost_usd_total", "Estimated LLM spend in USD", ["provider"])
SANDBOX_TEST_RUNS = Counter("fixit_sandbox_test_runs_total", "pytest runs in the sandbox by result", ["result"])
BUILD_INFO = Gauge("fixit_build_info", "Static build/config info (always 1)", ["version", "llm_backend", "fault_mode"])

# ---------------------------------------------------------------- Part E.2 demo counter
DEMO_CARDINALITY_ENABLED = os.environ.get("FIXIT_DEMO_CARDINALITY", "0") == "1"
if DEMO_CARDINALITY_ENABLED:
    DEMO_REQUESTS = Counter("fixit_demo_requests_total", "DEMO ONLY: counter labelled with run_id (cardinality experiment)", ["run_id"])
else:
    DEMO_REQUESTS = Counter("fixit_demo_requests_total", "DEMO ONLY: same counter without the run_id label")


def record_demo_request(run_id: str) -> None:
    if DEMO_CARDINALITY_ENABLED:
        DEMO_REQUESTS.labels(run_id=run_id).inc()
    else:
        DEMO_REQUESTS.inc()


def set_build_info(version: str, llm_backend: str, fault_mode: str) -> None:
    BUILD_INFO.labels(version=version, llm_backend=llm_backend, fault_mode=fault_mode).set(1)


def render() -> tuple[bytes, str]:
    """Prometheus exposition text for GET /metrics."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
