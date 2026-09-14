import io
import json
import logging

import structlog

from api.logging_config import FORBIDDEN_KEYS, drop_forbidden_keys, redact_secrets_in_text, setup_logging


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
    logging.getLogger("uvicorn.error").warning("ASGI callable returned without sending handshake.")
    doc = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert doc["event"] == "ASGI callable returned without sending handshake." and doc["service"] == "termlab-api"
    assert doc["level"] == "warning"


def test_uvicorn_connection_chatter_is_not_logged():
    buf = io.StringIO()
    setup_logging(stream=buf)
    logging.getLogger("uvicorn.error").info("connection open")
    logging.getLogger("uvicorn.error").info('1.2.3.4:5 - "WebSocket /ws/abc?token=SECRET&cols=80" [accepted]')
    assert buf.getvalue() == ""


def test_token_in_text_is_redacted_even_if_logged():
    line = '1.2.3.4:5 - "WebSocket /ws/abc?token=SECRET-x_y&cols=80" [accepted]'
    out = redact_secrets_in_text(None, "info", {"event": line, "msg": line, "n": 1})
    assert "SECRET" not in json.dumps(out) and "token=[redacted]&cols=80" in out["event"]
    buf = io.StringIO()
    setup_logging(stream=buf)
    logging.getLogger("uvicorn.error").warning(line)          # a WARNING would pass the level filter
    assert "SECRET" not in buf.getvalue() and "[redacted]" in buf.getvalue()
