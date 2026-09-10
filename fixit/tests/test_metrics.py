from prometheus_client import REGISTRY

from agent import metrics
from agent.llm import MockLLM
from agent.loop import run_task


def _val(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_task_increments_business_metrics(broken_repo):
    before_total = _val("fixit_tasks_total", {"outcome": "success"})
    before_iter = _val("fixit_task_iterations_count", {"outcome": "success"})
    before_tests = _val("fixit_sandbox_test_runs_total", {"result": "passed"})
    before_tools = _val("fixit_tool_calls_total", {"tool": "write_file", "status": "ok"})
    before_cost = _val("fixit_llm_cost_usd_total", {"provider": "mock"})

    result = run_task("metric0000001", "fix", str(broken_repo), MockLLM(latency=0))

    assert result.outcome == "success"
    assert _val("fixit_tasks_total", {"outcome": "success"}) == before_total + 1
    assert _val("fixit_tasks_in_progress") == 0
    assert _val("fixit_task_iterations_count", {"outcome": "success"}) == before_iter + 1
    assert _val("fixit_task_iterations_bucket", {"outcome": "success", "le": "5.0"}) >= 1
    assert _val("fixit_sandbox_test_runs_total", {"result": "passed"}) == before_tests + 1
    assert _val("fixit_tool_calls_total", {"tool": "write_file", "status": "ok"}) == before_tools + 1
    assert _val("fixit_llm_cost_usd_total", {"provider": "mock"}) > before_cost
    assert _val("fixit_llm_tokens_total", {"direction": "input"}) > 0


def test_exposition_contains_every_metric():
    body, _ = metrics.render()
    text = body.decode()
    for name in [
        "fixit_http_requests_total", "fixit_http_request_duration_seconds_bucket",
        "fixit_llm_requests_total", "fixit_llm_request_duration_seconds_bucket",
        "fixit_tool_calls_total", "fixit_tool_exec_seconds_sum", "fixit_tool_exec_seconds_count",
        "fixit_tasks_total", "fixit_tasks_in_progress", "fixit_task_duration_seconds_bucket",
        "fixit_task_iterations_bucket", "fixit_llm_tokens_total", "fixit_llm_cost_usd_total",
        "fixit_sandbox_test_runs_total", "fixit_build_info",
    ]:
        assert name.rsplit("_", 1)[0] in text or name in text, name


def test_http_middleware_records_requests():
    from fastapi.testclient import TestClient
    from agent.main import app

    client = TestClient(app)
    before = _val("fixit_http_requests_total", {"method": "GET", "path": "/health", "status": "200"})
    assert client.get("/health").status_code == 200
    assert _val("fixit_http_requests_total", {"method": "GET", "path": "/health", "status": "200"}) == before + 1
    assert client.get("/tasks/nope").status_code == 404
    assert _val("fixit_http_requests_total", {"method": "GET", "path": "/tasks/{run_id}", "status": "404"}) >= 1
    assert b"fixit_build_info" in client.get("/metrics").content
