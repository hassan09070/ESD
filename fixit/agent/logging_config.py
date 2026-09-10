"""structlog setup: one JSON object per line on stdout.

Every line carries: ts (UTC ISO-8601), level, service, event, msg. run_id / iteration /
request_id are bound with structlog.contextvars so every line emitted inside a task or
request carries them without being passed explicitly.

Safety net: `drop_forbidden_keys` removes any key that could carry a secret or bulky
content (api_key, authorization, content, prompt, response_text) before rendering.
uvicorn's own loggers are routed through the same formatter so *all* container output
is JSON (Filebeat's decode_json_fields never sees a plain-text line).
"""
from __future__ import annotations

import logging
import sys

import structlog

SERVICE = "fixit-agent"
FORBIDDEN_KEYS = frozenset({"api_key", "anthropic_api_key", "authorization", "content", "prompt", "response_text", "messages"})


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

_configured = False


def setup_logging(level: int = logging.INFO, stream=None) -> None:
    global _configured
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
    _configured = True


def get_logger(**initial) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(**initial)
