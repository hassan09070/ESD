"""WebSocket terminal over the FakeDocker echo shell."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from api.config import Settings
from api.docker_client import FakeDocker
from api.main import create_app


@pytest.fixture
def client():
    fake = FakeDocker()
    with TestClient(create_app(Settings(pool_size=2, warm_pool_size=0, queue_timeout_s=0.2), fake)) as c:
        c.fake = fake
        yield c


def session_with_sandbox(client):
    s = client.post("/sessions").json()
    r = client.post(f"/sessions/{s['session_id']}/sandbox", headers={"Authorization": "Bearer " + s["token"]})
    assert r.status_code == 200
    return s


def recv_until(ws, needle: bytes, timeout=3.0) -> bytes:
    buf = b""
    deadline = time.time() + timeout
    while needle not in buf and time.time() < deadline:
        msg = ws.receive()
        if "bytes" in msg and msg["bytes"] is not None:
            buf += msg["bytes"]
        elif "text" in msg and msg["text"]:
            buf += msg["text"].encode()
    assert needle in buf, buf
    return buf


def counter(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


def test_terminal_echo_roundtrip_and_metrics(client):
    s = session_with_sandbox(client)
    bytes_in_before = counter("termlab_terminal_bytes_total", direction="in")
    rt_before = counter("termlab_terminal_roundtrip_seconds_count")
    with client.websocket_connect(f"/ws/{s['session_id']}?token={s['token']}&cols=100&rows=30") as ws:
        recv_until(ws, b"user@termlab:~$ ")
        ws.send_bytes(b"echo hi\r")
        recv_until(ws, b"echo hi")
        ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        time.sleep(0.15)
    assert counter("termlab_terminal_bytes_total", direction="in") == bytes_in_before + len(b"echo hi\r")
    assert counter("termlab_terminal_roundtrip_seconds_count") >= rt_before + 1
    assert counter("termlab_commands_total") >= 1
    assert (client.fake.resizes[-1][1], client.fake.resizes[-1][2]) == (120, 40)
    assert client.get("/pool").json()["active"] == 1           # closing the tab keeps the sandbox
    info = client.get(f"/sessions/{s['session_id']}", headers={"Authorization": "Bearer " + s["token"]}).json()
    assert info["state"] == "detached" and info["bytes_in"] == 8 and info["commands"] == 1


def test_exit_in_shell_reaps_sandbox(client):
    s = session_with_sandbox(client)
    before = counter("termlab_sandboxes_reaped_total", reason="user_exit")
    with client.websocket_connect(f"/ws/{s['session_id']}?token={s['token']}") as ws:
        recv_until(ws, b"$ ")
        ws.send_bytes(b"exit\r")
        buf = recv_until(ws, b'"exit"')
        assert b"logout" in buf
    assert counter("termlab_sandboxes_reaped_total", reason="user_exit") == before + 1
    assert client.get("/pool").json()["active"] == 0
    assert not client.fake.containers


def test_ws_refused_without_sandbox_or_bad_token(client):
    s = client.post("/sessions").json()
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/ws/{s['session_id']}?token={s['token']}"):
            pass
    assert e.value.code == 4409
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/ws/{s['session_id']}?token=bad"):
            pass
    assert e.value.code == 4401


def test_reattach_after_detach(client):
    s = session_with_sandbox(client)
    for _ in range(2):
        with client.websocket_connect(f"/ws/{s['session_id']}?token={s['token']}") as ws:
            recv_until(ws, b"$ ")
    assert REGISTRY.get_sample_value("termlab_users_connected") == 0
    assert client.get("/pool").json()["active"] == 1
