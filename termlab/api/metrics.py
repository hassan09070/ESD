"""ALL Prometheus metric definitions for termlab live here. No other module defines metrics.

Naming follows Prometheus conventions: `termlab_` prefix, base units (seconds, bytes),
`_total` on counters. Labels are deliberately low-cardinality (method/path/status/source/
outcome/reason/direction). session_id, container ids and user tokens are NEVER labels -
they belong in logs. The one exception is the throwaway `termlab_demo_requests_total` used
by the Part E.2 cardinality experiment, which only gets a `request_id` label when
TERMLAB_DEMO_CARDINALITY=1.

Per-sandbox resource usage is aggregated (sum/max across running sandboxes, plus a ratio
histogram) for the same reason: one series per container would grow without bound.
"""
from __future__ import annotations

import os

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, Summary, generate_latest

HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
SPAWN_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10)
QUEUE_BUCKETS = (0.0, 0.1, 0.5, 1, 2, 5, 10, 30, 60)
ROUNDTRIP_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2)
RATIO_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)

# ---------------------------------------------------------------- application metrics
HTTP_REQUESTS = Counter("termlab_http_requests_total", "HTTP requests handled by the control plane", ["method", "path", "status"])
HTTP_DURATION = Histogram("termlab_http_request_duration_seconds", "HTTP request latency", ["method", "path"], buckets=HTTP_BUCKETS)
WS_MESSAGES = Counter("termlab_ws_messages_total", "WebSocket frames relayed between browser and PTY", ["direction"])
TERMINAL_BYTES = Counter("termlab_terminal_bytes_total", "Bytes relayed through terminals (counts only, never content)", ["direction"])
TERMINAL_ROUNDTRIP = Histogram(
    "termlab_terminal_roundtrip_seconds",
    "Self-explored: time from forwarding a keystroke chunk to the PTY until the next output chunk (typing lag as the server sees it)",
    buckets=ROUNDTRIP_BUCKETS,
)
SANDBOX_SPAWN = Histogram("termlab_sandbox_spawn_seconds", "Time to hand a user a running sandbox, by source (warm pool vs cold create)", ["source"], buckets=SPAWN_BUCKETS)
QUEUE_WAIT = Histogram("termlab_queue_wait_seconds", "Time a sandbox request waited for a free pool slot", buckets=QUEUE_BUCKETS)
STATS_SAMPLE_SECONDS = Summary("termlab_stats_sample_seconds", "Duration of one resource-sampling pass over all sandboxes (Summary: sum/count only)")
POOL_FREE = Gauge("termlab_pool_free", "Free sandbox slots in the pool")
QUEUE_LENGTH = Gauge("termlab_queue_length", "Sandbox requests currently waiting for a slot")
WARM_POOL_SIZE = Gauge("termlab_warm_pool_size", "Pre-created sandboxes ready to be claimed")
SANDBOX_CPU_SUM = Gauge("termlab_sandbox_cpu_cores_sum", "CPU cores consumed by all running user sandboxes (sampled)")
SANDBOX_CPU_MAX = Gauge("termlab_sandbox_cpu_cores_max", "CPU cores consumed by the busiest user sandbox (sampled)")
SANDBOX_MEM_SUM = Gauge("termlab_sandbox_memory_bytes_sum", "Memory used by all running user sandboxes (sampled)")
SANDBOX_MEM_MAX = Gauge("termlab_sandbox_memory_bytes_max", "Memory used by the hungriest user sandbox (sampled)")
SANDBOX_MEM_RATIO = Histogram("termlab_sandbox_memory_ratio", "memory used / memory limit per sandbox per sample", buckets=RATIO_BUCKETS)
BUILD_INFO = Gauge("termlab_build_info", "Static build/config info (always 1)", ["version", "fault_mode"])

# ---------------------------------------------------------------- business metrics
SESSIONS_STARTED = Counter("termlab_sessions_started_total", "Sandbox requests by outcome", ["outcome"])
SANDBOXES_REAPED = Counter("termlab_sandboxes_reaped_total", "Sandboxes destroyed, by reason", ["reason"])
SANDBOX_SECONDS = Counter("termlab_sandbox_seconds_total", "Billable sandbox-seconds (lifetime of every reaped sandbox)")
SESSION_DURATION = Summary("termlab_session_duration_seconds", "Lifetime of a sandbox from spawn to reap (Summary: sum/count only)")
COMMANDS = Counter("termlab_commands_total", "Enter presses in terminals (command count without command content)")
SANDBOXES_ACTIVE = Gauge("termlab_sandboxes_active", "User sandboxes currently running")
USERS_CONNECTED = Gauge("termlab_users_connected", "Open terminal WebSocket connections")

OUTCOMES = ("ok", "queued_timeout", "pool_full", "error")
REAP_REASONS = ("idle", "user_exit", "oom", "admin", "orphan", "error")
for _o in OUTCOMES:
    SESSIONS_STARTED.labels(outcome=_o)
for _r in REAP_REASONS:
    SANDBOXES_REAPED.labels(reason=_r)
for _d in ("in", "out"):
    WS_MESSAGES.labels(direction=_d)
    TERMINAL_BYTES.labels(direction=_d)
for _s in ("warm", "cold"):
    SANDBOX_SPAWN.labels(source=_s)

# ---------------------------------------------------------------- Part E.2 demo counter
DEMO_CARDINALITY_ENABLED = os.environ.get("TERMLAB_DEMO_CARDINALITY", "0") == "1"
if DEMO_CARDINALITY_ENABLED:
    DEMO_REQUESTS = Counter("termlab_demo_requests_total", "DEMO ONLY: counter labelled with request_id (cardinality experiment)", ["request_id"])
else:
    DEMO_REQUESTS = Counter("termlab_demo_requests_total", "DEMO ONLY: same counter without the request_id label")


def record_demo_request(request_id: str) -> None:
    if DEMO_CARDINALITY_ENABLED:
        DEMO_REQUESTS.labels(request_id=request_id).inc()
    else:
        DEMO_REQUESTS.inc()


def set_build_info(version: str, fault_mode: str) -> None:
    BUILD_INFO.labels(version=version, fault_mode=fault_mode).set(1)


def render() -> tuple[bytes, str]:
    """Prometheus exposition text for GET /metrics."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
