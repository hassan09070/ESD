"""Assignment 2 Part B: the gRPC PlaceOrder service, started in-process on a free port, against
the same SQLite file as the HTTP app, so the test can prove they share one source of truth."""
import grpc
import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import app, limiter
from grpc_service import canteen_pb2, canteen_pb2_grpc
from grpc_service import server as grpc_server
from prometheus_client import REGISTRY  # noqa: E402


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


@pytest.fixture(scope="module")
def stub():
    server, port = grpc_server.serve(port=0, metrics_port=None)
    channel = grpc.insecure_channel(f"localhost:{port}")
    yield canteen_pb2_grpc.OrderServiceStub(channel)
    channel.close()
    server.stop(0)


@pytest.fixture
def clean():
    store.init()
    store.reset()
    limiter.reset()


def test_place_order_over_grpc_is_visible_over_http(stub, clean):
    resp, call = stub.PlaceOrder.with_call(canteen_pb2.PlaceOrderRequest(shop="tapal", item="biryani"),
                                           timeout=2, metadata=(("x-request-id", "grpc-test-1"),))
    assert resp.state == "waiting" and resp.shop == "tapal" and not resp.replayed
    assert call.code() == grpc.StatusCode.OK
    assert dict(call.trailing_metadata())["x-request-id"] == "grpc-test-1"    # correlation id echoed back
    with TestClient(app) as http:
        assert http.get(f"/orders/{resp.order_id}").json()["shop"] == "tapal"       # same store as the HTTP API
        assert http.post(f"/orders/{resp.order_id}/ready").status_code == 200       # the HTTP side can move it on
    assert store.get_order(resp.order_id).state == "ready"


def test_unknown_shop_is_invalid_argument(stub, clean):
    with pytest.raises(grpc.RpcError) as exc:
        stub.PlaceOrder(canteen_pb2.PlaceOrderRequest(shop="pizza", item="x"), timeout=2)
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert "unknown shop" in exc.value.details()
    assert store.count() == 0


def test_idempotency_key_works_over_grpc_too(stub, clean):
    req = canteen_pb2.PlaceOrderRequest(shop="grito", item="ice cream", idempotency_key="grpc-cust-1")
    first, repeat = stub.PlaceOrder(req, timeout=2), stub.PlaceOrder(req, timeout=2)
    assert first.order_id == repeat.order_id and not first.replayed and repeat.replayed
    assert store.count() == 1


def test_grpc_metrics_are_recorded_server_side(stub, clean):
    ok_before = sample("canteen_grpc_server_handled_total", grpc_method="PlaceOrder", grpc_code="OK")
    bad_before = sample("canteen_grpc_server_handled_total", grpc_method="PlaceOrder", grpc_code="INVALID_ARGUMENT")
    stub.PlaceOrder(canteen_pb2.PlaceOrderRequest(shop="cafetogo", item="burger"), timeout=2)
    with pytest.raises(grpc.RpcError):
        stub.PlaceOrder(canteen_pb2.PlaceOrderRequest(shop="nowhere", item="x"), timeout=2)
    assert sample("canteen_grpc_server_handled_total", grpc_method="PlaceOrder", grpc_code="OK") == ok_before + 1
    assert sample("canteen_grpc_server_handled_total", grpc_method="PlaceOrder", grpc_code="INVALID_ARGUMENT") == bad_before + 1
    assert sample("canteen_grpc_server_handling_seconds_count", grpc_method="PlaceOrder") >= 2
    assert sample("canteen_grpc_server_in_flight", grpc_method="PlaceOrder") == 0
