import io
import json
import logging

import structlog

from api.logging_config import FORBIDDEN_KEYS, drop_forbidden_keys, setup_logging


def test_processor_strips_terminal_content_and_credentials():
    event = {"event": "ws_detach", "Authorization": "Bearer x", "token": "t", "stdin": b"password123", "data": "x",
             "output": "y", "bytes_in": 12}
    out = drop_forbidden_keys(None, "info", dict(event))
    assert set(out) == {"event", "bytes_in"}
    assert not (set(k.lower() for k in out) & FORBIDDEN_KEYS)


def test_rendered_line_is_single_json_with_required_fields():
    buf = io.StringIO()
    setup_logging(stream=buf)
    log = structlog.get_logger()
    structlog.contextvars.bind_contextvars(request_id="abc123", session_id="s1")
    log.info("sandbox_spawn", msg="sandbox ready", source="warm", spawn_ms=42, token="LEAK", stdin="LEAK")
    structlog.contextvars.clear_contextvars()
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1
    doc = json.loads(lines[0])
    for key in ("ts", "level", "service", "event", "msg"):
        assert key in doc, key
    assert doc["event"] == "sandbox_spawn" and doc["level"] == "info" and doc["service"] == "termlab-api"
    assert doc["request_id"] == "abc123" and doc["session_id"] == "s1" and doc["spawn_ms"] == 42
    assert "LEAK" not in lines[0]
    assert doc["ts"].endswith("Z")


def test_stdlib_loggers_are_json_too():
    buf = io.StringIO()
    setup_logging(stream=buf)
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    doc = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert doc["event"] == "Application startup complete." and doc["service"] == "termlab-api"
