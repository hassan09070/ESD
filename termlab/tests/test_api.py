"""HTTP surface over the FakeDocker backend (TestClient runs the lifespan)."""
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from api.config import Settings
from api.docker_client import FakeDocker
from api.main import create_app, route_template


@pytest.fixture
def client():
    fake = FakeDocker()
    app = create_app(Settings(pool_size=2, warm_pool_size=1, queue_timeout_s=0.2, idle_timeout_s=60), fake)
    with TestClient(app) as c:
        c.fake = fake
        yield c


def test_route_template_bounds_path_label():
    assert route_template("/sessions/abc123") == "/sessions/{id}"
    assert route_template("/sessions/abc123/sandbox") == "/sessions/{id}/sandbox"
    assert route_template("/sessions") == "/sessions"
    assert route_template("/docs") == "other"


def test_health_and_pool(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["docker"] and h["image"] and h["fault"] == "none" and h["capacity"] == 2
    p = client.get("/pool").json()
    assert p == {"capacity": 2, "active": 0, "free": 2, "queue": 0, "warm": 1, "fault": "none"}


def test_index_serves_terminal_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "xterm" in r.text and "New sandbox" in r.text


def test_session_flow_with_bearer_token(client):
    r = client.post("/sessions", headers={"X-Request-ID": "req-1"})
    assert r.status_code == 201 and r.headers["X-Request-ID"] == "req-1" and r.headers["X-Session-ID"]
    s = r.json()
    hdr = {"Authorization": "Bearer " + s["token"]}
    assert client.get(f"/sessions/{s['session_id']}").status_code == 200        # cookie was set by POST /sessions
    client.cookies.clear()
    assert client.get(f"/sessions/{s['session_id']}").status_code == 401
    assert client.get(f"/sessions/{s['session_id']}", headers={"Authorization": "Bearer nope"}).status_code == 403
    r = client.post(f"/sessions/{s['session_id']}/sandbox", headers=hdr)
    assert r.status_code == 200 and r.json()["source"] == "warm" and r.json()["state"] == "running"
    assert client.post(f"/sessions/{s['session_id']}/sandbox", headers=hdr).status_code == 409
    info = client.get(f"/sessions/{s['session_id']}", headers=hdr).json()
    assert info["state"] == "running" and info["sandbox_id"]
    r = client.delete(f"/sessions/{s['session_id']}", headers=hdr)
    assert r.json() == {"session_id": s["session_id"], "state": "reaped", "reaped": True}
    assert client.get("/pool").json()["active"] == 0


def test_pool_full_returns_503_with_wait(client):
    toks = []
    for _ in range(3):
        s = client.post("/sessions").json()
        toks.append(s)
    for s in toks[:2]:
        assert client.post(f"/sessions/{s['session_id']}/sandbox", headers={"Authorization": "Bearer " + s["token"]}).status_code == 200
    r = client.post(f"/sessions/{toks[2]['session_id']}/sandbox", headers={"Authorization": "Bearer " + toks[2]["token"]})
    assert r.status_code == 503 and r.json()["error"] == "queued_timeout" and r.json()["waited_ms"] >= 200


def test_metrics_endpoint_and_http_metrics(client):
    client.post("/sessions")
    client.get("/health")
    body = client.get("/metrics").text
    assert 'termlab_http_requests_total{method="POST",path="/sessions",status="201"}' in body
    assert 'termlab_http_requests_total{method="GET",path="/health",status="200"}' in body      # counted ...
    assert "termlab_build_info" in body
    assert "_created" not in body
    assert REGISTRY.get_sample_value("termlab_http_requests_total", {"method": "GET", "path": "/metrics", "status": "200"}) is None


def test_health_is_not_logged_and_request_id_is_bounded(client):
    import io
    import json
    import logging

    buf = io.StringIO()
    tap = logging.StreamHandler(buf)
    tap.setFormatter(logging.getLogger().handlers[0].formatter)     # same JSON formatter as stdout
    logging.getLogger().addHandler(tap)
    try:
        client.get("/health")
        r = client.post("/sessions", headers={"X-Request-ID": "x" * 500})
    finally:
        logging.getLogger().removeHandler(tap)
    assert r.headers["X-Request-ID"] == "x" * 64
    events = [(l["event"], l.get("path")) for l in (json.loads(x) for x in buf.getvalue().splitlines() if x.startswith("{"))]
    assert ("http_request", "/health") not in events and ("http_request", "/sessions") in events    # ... but not logged


def test_shutdown_reaps_everything(client):
    s = client.post("/sessions").json()
    client.post(f"/sessions/{s['session_id']}/sandbox", headers={"Authorization": "Bearer " + s["token"]})
    fake = client.fake
    assert fake.containers
    client.__exit__(None, None, None)
    assert not fake.containers
