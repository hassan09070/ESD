"""The order lifecycle and its metrics, no Docker needed (TestClient runs the app in-process)."""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import ORDERS, app          # must come before prometheus_client: metrics.py sets an env var it reads at import
from prometheus_client import REGISTRY   # noqa: E402


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


@pytest.fixture
def client():
    ORDERS.clear()                      # every test starts with an empty canteen
    return TestClient(app)


def test_full_lifecycle(client):
    r = client.post("/orders", json={"stall": "chai", "item": "doodh patti"})
    assert r.status_code == 201
    oid = r.json()["order_id"]
    assert r.json()["state"] == "waiting"
    assert client.get("/stalls").json() == {"chai": 1, "biryani": 0, "shawarma": 0, "juice": 0}

    assert client.post(f"/orders/{oid}/ready").json()["state"] == "ready"
    assert client.get("/stalls").json()["chai"] == 0            # no longer waiting
    assert client.post(f"/orders/{oid}/pickup").json()["state"] == "picked_up"
    assert client.get(f"/orders/{oid}").json()["picked_up_at"] is not None


def test_illegal_transitions_are_409(client):
    oid = client.post("/orders", json={"stall": "juice", "item": "mango"}).json()["order_id"]
    assert client.post(f"/orders/{oid}/pickup").status_code == 409        # not ready yet
    client.post(f"/orders/{oid}/cancel")
    assert client.post(f"/orders/{oid}/ready").status_code == 409         # cancelled is final


def test_bad_input(client):
    assert client.post("/orders", json={"stall": "pizza", "item": "x"}).status_code == 400
    assert client.post("/orders", json={"stall": "chai"}).status_code == 422             # missing item
    assert client.get("/orders/nope").status_code == 404


# ----------------------------------------------------------------------------- stage 1: metrics
def test_metrics_endpoint_has_all_four_types(client):
    text = client.get("/metrics").text
    types = {m.group(2) for m in re.finditer(r"^# TYPE (canteen_\w+) (\w+)$", text, re.M)}
    assert types == {"counter", "gauge", "histogram", "summary"}
    assert 'canteen_orders_waiting{stall="chai"} 0.0' in text            # pre-initialised
    assert "_created" not in text


def test_business_metrics_follow_the_lifecycle(client):
    placed, waiting = sample("canteen_orders_placed_total", stall="juice"), sample("canteen_orders_waiting", stall="juice")
    prep_count = sample("canteen_order_prep_seconds_count", stall="juice")
    oid = client.post("/orders", json={"stall": "juice", "item": "mango"}).json()["order_id"]
    assert sample("canteen_orders_placed_total", stall="juice") == placed + 1
    assert sample("canteen_orders_waiting", stall="juice") == waiting + 1
    client.post(f"/orders/{oid}/ready")
    assert sample("canteen_orders_waiting", stall="juice") == waiting              # back down
    assert sample("canteen_order_prep_seconds_count", stall="juice") == prep_count + 1
    assert sample("canteen_order_prep_summary_seconds_count", stall="juice") >= 1
    client.post(f"/orders/{oid}/pickup")
    assert sample("canteen_pickup_delay_seconds_count", stall="juice") >= 1


def test_cancel_adjusts_gauge_only_if_it_was_waiting(client):
    a = client.post("/orders", json={"stall": "chai", "item": "x"}).json()["order_id"]
    b = client.post("/orders", json={"stall": "chai", "item": "y"}).json()["order_id"]
    client.post(f"/orders/{b}/ready")
    waiting = sample("canteen_orders_waiting", stall="chai")
    client.post(f"/orders/{a}/cancel")                                            # was waiting -> gauge -1
    client.post(f"/orders/{b}/cancel")                                            # was ready   -> gauge unchanged
    assert sample("canteen_orders_waiting", stall="chai") == waiting - 1
    assert sample("canteen_orders_cancelled_total", stall="chai") >= 2


def test_http_metrics_use_route_templates(client):
    oid = client.post("/orders", json={"stall": "shawarma", "item": "z"}).json()["order_id"]
    client.get(f"/orders/{oid}")
    client.get("/orders/nope")
    text = client.get("/metrics").text
    assert 'canteen_http_requests_total{method="GET",route="/orders/{order_id}",status="200"}' in text
    assert 'canteen_http_requests_total{method="GET",route="/orders/{order_id}",status="404"}' in text
    assert oid not in text                                                        # never the real path
    assert 'route="/metrics"' not in text                                         # scrapes are not counted
