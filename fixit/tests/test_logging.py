import io
import json
import logging

import structlog

from agent.logging_config import FORBIDDEN_KEYS, drop_forbidden_keys, setup_logging


def test_processor_strips_forbidden_keys():
    event = {"event": "llm_call", "api_key": "sk-secret", "Authorization": "Bearer x", "content": "file body",
             "prompt": "p", "response_text": "r", "duration_ms": 12}
    out = drop_forbidden_keys(None, "info", dict(event))
    assert set(out) == {"event", "duration_ms"}
    assert not (set(k.lower() for k in out) & FORBIDDEN_KEYS)


def test_rendered_line_is_single_json_with_required_fields():
    buf = io.StringIO()
    setup_logging(stream=buf)
    log = structlog.get_logger()
    structlog.contextvars.bind_contextvars(run_id="abc123", iteration=2)
    log.info("tool_call", msg="tool executed", tool="run_tests", duration_ms=5, api_key="LEAK", content="LEAK")
    structlog.contextvars.clear_contextvars()
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1
    doc = json.loads(lines[0])
    for key in ("ts", "level", "service", "event", "msg"):
        assert key in doc, key
    assert doc["event"] == "tool_call" and doc["level"] == "info" and doc["service"] == "fixit-agent"
    assert doc["run_id"] == "abc123" and doc["iteration"] == 2
    assert "LEAK" not in lines[0]
    assert doc["ts"].endswith("Z")


def test_stdlib_loggers_are_json_too():
    buf = io.StringIO()
    setup_logging(stream=buf)
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    doc = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert doc["event"] == "Application startup complete." and doc["service"] == "fixit-agent"
