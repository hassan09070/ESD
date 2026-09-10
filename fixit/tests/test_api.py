import pytest
from fastapi.testclient import TestClient

import agent.main as main
from agent.llm import LLMError, MockLLM


@pytest.fixture
def client(broken_repo, monkeypatch):
    monkeypatch.setattr(main, "LLM", MockLLM(latency=0))
    monkeypatch.setattr(main, "WORKSPACE", broken_repo)
    return TestClient(main.app)


def test_post_task_success_and_status_roundtrip(client, broken_repo):
    r = client.post("/tasks", json={"task": "fix tests", "repo": str(broken_repo)}, headers={"X-Request-ID": "req-test-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "success" and body["iterations"] == 5
    assert r.headers["X-Request-ID"] == "req-test-1" and r.headers["X-Run-ID"] == body["run_id"]
    assert client.get(f"/tasks/{body['run_id']}").json() == body
    assert client.get("/tasks").json()[0]["run_id"] == body["run_id"]


def test_bad_input_is_4xx(client):
    assert client.post("/tasks", json={"task": ""}).status_code == 422
    assert client.post("/tasks", json={"task": "x", "repo": "/definitely/not/here"}).status_code == 400
    assert client.get("/tasks/unknown").status_code == 404


class DeadLLM:
    provider = "mock"
    model = "claude-sonnet-4-6"

    def complete(self, messages, tools):
        raise LLMError("provider down")


def test_llm_outage_is_503(client, broken_repo, monkeypatch):
    monkeypatch.setattr("agent.llm.BACKOFF_BASE_S", 0.0)
    monkeypatch.setattr(main, "LLM", DeadLLM())
    r = client.post("/tasks", json={"task": "x", "repo": str(broken_repo)})
    assert r.status_code == 503 and r.json()["outcome"] == "error" and r.json()["error_type"] == "LLMError"
