"""Assignment 3: what one replica must do so that Envoy, Prometheus and Kibana can tell the
replicas apart and the experiments can be run: the replica identifier on every response and
log line, the active-requests gauge, the fixed delay on both interfaces, and the gRPC health
service Envoy's active health checks call. No Docker: the HTTP app runs in TestClient and the
gRPC server in-process, exactly as app/replica.py combines them."""
import json
import logging
import time

import grpc
import pytest
from fastapi.testclient import TestClient
from grpc_health.v1 import health_pb2, health_pb2_grpc

from app import chaos, store
from app.logging_config import REPLICA, setup_logging
from app.main import app, limiter
from grpc_service import canteen_pb2, canteen_pb2_grpc
from grpc_service import server as grpc_server
from prometheus_client import REGISTRY  # noqa: E402


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


@pytest.fixture
def client():
    store.init()
    store.reset()
    limiter.reset()
    chaos.reset()
    with TestClient(app) as c:
        yield c
    chaos.reset()


@pytest.fixture(scope="module")
def grpc_port():
    server, port = grpc_server.serve(port=0, metrics_port=None)
    yield port
    server.stop(0)


@pytest.fixture
def stub(grpc_port):
    with grpc.insecure_channel(f"localhost:{grpc_port}") as channel:
        yield canteen_pb2_grpc.OrderServiceStub(channel)


def test_every_response_names_the_replica(client):
    assert client.get("/shops").headers["X-Replica"] == REPLICA
    assert client.get("/orders/nope").headers["X-Replica"] == REPLICA          # errors too: Envoy logs the header on every response
    assert f'canteen_replica_info{{replica="{REPLICA}"}} 1.0' in client.get("/metrics").text


def test_every_log_line_names_the_replica(client, capsys):
    import io
    buf = io.StringIO()
    setup_logging(stream=buf)
    try:
        client.get("/shops", headers={"X-Request-ID": "rep-1"})
    finally:
        setup_logging()
    lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    line = next(l for l in lines if l.get("event") == "http_request")
    assert line["replica"] == REPLICA and line["request_id"] == "rep-1"


def test_in_flight_gauge_counts_active_requests(client):
    assert sample("canteen_http_in_flight") == 0
    client.get("/shops")
    assert sample("canteen_http_in_flight") == 0                              # back to 0 once answered
    assert "canteen_http_in_flight 0.0" in client.get("/metrics").text


def test_fixed_delay_applies_to_http_but_not_health(client):
    client.put("/chaos", json={"fixed_delay_ms": 150})
    assert client.get("/chaos").json()["fixed_delay_ms"] == 150
    t0 = time.perf_counter(); client.get("/shops"); slow = time.perf_counter() - t0
    t0 = time.perf_counter(); client.get("/health"); health = time.perf_counter() - t0
    assert slow >= 0.15 and health < 0.1                                      # Envoy's health probe must stay fast
    client.delete("/chaos")
    t0 = time.perf_counter(); client.get("/shops"); assert time.perf_counter() - t0 < 0.1


def test_fixed_delay_applies_to_grpc_too(client, stub):
    client.put("/chaos", json={"fixed_delay_ms": 150})                        # same process, same chaos object
    t0 = time.perf_counter()
    _, call = stub.PlaceOrder.with_call(canteen_pb2.PlaceOrderRequest(shop="tapal", item="pulao"), timeout=2)
    assert time.perf_counter() - t0 >= 0.15
    assert dict(call.initial_metadata())["x-replica"] == REPLICA              # Envoy logs %RESP(x-replica)%
    client.delete("/chaos")
    slow_server_side = (sample("canteen_grpc_server_handling_seconds_bucket", grpc_method="PlaceOrder", le="+Inf")
                        - sample("canteen_grpc_server_handling_seconds_bucket", grpc_method="PlaceOrder", le="0.1"))
    assert slow_server_side >= 1                                              # the delay is inside the timed window


def test_cancel_waiting_clears_only_that_item(client):
    client.post("/orders", json={"shop": "tapal", "item": "load-test"})
    client.post("/orders", json={"shop": "tapal", "item": "load-test"})
    keep = client.post("/orders", json={"shop": "tapal", "item": "biryani"}).json()["order_id"]
    assert store.cancel_waiting("load-test") == 2
    assert client.get("/shops").json()["tapal"] == 1 and store.get_order(keep).state == "waiting"
    assert store.cancel_waiting("load-test") == 0


def test_order_id_collision_is_retried(client, monkeypatch):
    import uuid
    ids = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])          # the second order first draws the id the first one took

    class FakeUUID:
        hex = property(lambda self: next(ids) + "0" * 24)
    monkeypatch.setattr(uuid, "uuid4", lambda: FakeUUID())
    h = {"X-Request-ID": "fixed"}                            # so the middleware does not draw request ids from the fake
    first = client.post("/orders", json={"shop": "tapal", "item": "x"}, headers=h).json()["order_id"]
    second = client.post("/orders", json={"shop": "tapal", "item": "y"}, headers=h).json()["order_id"]
    assert (first, second) == ("aaaaaaaa", "bbbbbbbb") and store.count() == 2


def test_grpc_health_service_is_serving_and_not_counted(client, grpc_port):
    before = sample("canteen_grpc_server_handled_total", grpc_method="PlaceOrder", grpc_code="OK")
    with grpc.insecure_channel(f"localhost:{grpc_port}") as channel:
        health = health_pb2_grpc.HealthStub(channel)
        assert health.Check(health_pb2.HealthCheckRequest(service=""), timeout=2).status == health_pb2.HealthCheckResponse.SERVING
        assert health.Check(health_pb2.HealthCheckRequest(service="canteen.v1.OrderService"), timeout=2).status == health_pb2.HealthCheckResponse.SERVING
        with pytest.raises(grpc.RpcError) as exc:
            health.Check(health_pb2.HealthCheckRequest(service="nope"), timeout=2)
        assert exc.value.code() == grpc.StatusCode.NOT_FOUND
    assert sample("canteen_grpc_server_handled_total", grpc_method="PlaceOrder", grpc_code="OK") == before
    assert not any(s.labels.get("grpc_method", "").startswith("Check") for m in REGISTRY.collect() if m.name == "canteen_grpc_server_handled" for s in m.samples)
