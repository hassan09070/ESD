"""Structured logging: one JSON object per line on stdout.

Why JSON on stdout and nothing else: Docker's json-file log driver captures stdout into a
file per container; Filebeat tails that file and, because the line is already JSON, turns
every key into a searchable field in Elasticsearch with no parsing rules of our own.

Every line carries: ts (UTC, ISO-8601), level, service, event (machine name), msg (human
sentence). Inside an HTTP request, `request_id` is bound as a context variable so every
line emitted while handling that request carries it without being passed around.

What is never logged: anything a customer typed. `item` is free text and could hold a
name or a phone number, so order events log order_id and stall only.
"""
from __future__ import annotations

import logging
import sys

import structlog

SERVICE = "canteen"
FORBIDDEN_KEYS = frozenset({"item", "authorization", "cookie", "token", "password"})


def add_service(_logger, _method, event_dict: dict) -> dict:
    event_dict.setdefault("service", SERVICE)
    return event_dict


def ensure_msg(_logger, _method, event_dict: dict) -> dict:
    """`event` is the machine-readable name (order_placed); `msg` the human sentence."""
    event_dict.setdefault("msg", event_dict.get("event", ""))
    return event_dict


def drop_forbidden_keys(_logger, _method, event_dict: dict) -> dict:
    for key in list(event_dict):
        if key.lower() in FORBIDDEN_KEYS:
            event_dict.pop(key)
    return event_dict


PROCESSORS = [
    structlog.contextvars.merge_contextvars,                     # request_id bound by the middleware
    structlog.stdlib.add_log_level,                              # "level": "info"
    add_service,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    drop_forbidden_keys,
    ensure_msg,
    structlog.processors.format_exc_info,                        # tracebacks as a string field
]


def setup_logging(stream=None, level: int = logging.INFO) -> None:
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=PROCESSORS,                            # stdlib records (uvicorn) get the same fields
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, structlog.processors.JSONRenderer()],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    structlog.configure(
        processors=[*PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    # uvicorn: route its loggers through the handler above (so its lines are JSON too), keep
    # only warnings and errors - its INFO lines duplicate our http_request event and, for
    # WebSocket apps, would include query strings.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
