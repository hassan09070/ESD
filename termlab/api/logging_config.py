"""structlog setup: one JSON object per line on stdout.

Every line carries: ts (UTC ISO-8601), level, service, event, msg. request_id / session_id
are bound with structlog.contextvars inside HTTP requests and WebSocket sessions so every
line emitted there carries them without being passed explicitly; background loops (reaper,
stats sampler, warm pool) pass session_id as an explicit field.

Privacy rule for a terminal product: the bytes that flow through a PTY are never logged -
users type passwords into shells. Only byte counts and newline counts are recorded.
`drop_forbidden_keys` is the safety net: any key that could carry terminal content or a
credential is removed before rendering. uvicorn's own loggers are routed through the same
formatter so *all* container output is JSON (Filebeat's decode_json_fields never sees a
plain-text line).
"""
from __future__ import annotations

import logging
import sys

import structlog

SERVICE = "termlab-api"
FORBIDDEN_KEYS = frozenset({
    "authorization", "token", "cookie", "api_key",
    "data", "stdin", "stdout", "output", "keystrokes", "payload", "chunk", "content",
})


def drop_forbidden_keys(_logger, _method, event_dict: dict) -> dict:
    for key in list(event_dict):
        if key.lower() in FORBIDDEN_KEYS:
            event_dict.pop(key)
    return event_dict


def add_service(_logger, _method, event_dict: dict) -> dict:
    event_dict.setdefault("service", SERVICE)
    return event_dict


def ensure_msg(_logger, _method, event_dict: dict) -> dict:
    """`event` is the machine-readable event name; `msg` is the human sentence."""
    event_dict.setdefault("msg", event_dict.get("event", ""))
    return event_dict


SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    add_service,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    drop_forbidden_keys,
    ensure_msg,
    structlog.processors.format_exc_info,
]


def setup_logging(level: int = logging.INFO, stream=None) -> None:
    stream = stream or sys.stdout
    structlog.configure(
        processors=[*SHARED_PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=SHARED_PROCESSORS,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, structlog.processors.JSONRenderer()],
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # uvicorn's access log duplicates our http_request event (and lacks request_id): silence it.
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False
    # docker-py and urllib3 are chatty at DEBUG; keep them at WARNING.
    for name in ("docker", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(**initial) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(**initial)
