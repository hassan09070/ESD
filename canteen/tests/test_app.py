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
    r = client.post("/orders", json={"shop": "sky_dhaba", "item": "pharata"})
    assert r.status_code == 201
    oid = r.json()["order_id"]
    assert r.json()["state"] == "waiting"
    assert client.get("/shops").json() == {"sky_dhaba": 1, "tapal": 0, "cafetogo": 0, "grito": 0}

    assert client.post(f"/orders/{oid}/ready").json()["state"] == "ready"
    assert client.get("/shops").json()["sky_dhaba"] == 0            # no longer waiting
    assert client.post(f"/orders/{oid}/pickup").json()["state"] == "picked_up"
    assert client.get(f"/orders/{oid}").json()["picked_up_at"] is not None


def test_illegal_transitions_are_409(client):
    oid = client.post("/orders", json={"shop": "grito", "item": "corn"}).json()["order_id"]
    assert client.post(f"/orders/{oid}/pickup").status_code == 409        # not ready yet
    client.post(f"/orders/{oid}/cancel")
    assert client.post(f"/orders/{oid}/ready").status_code == 409         # cancelled is final


def test_bad_input(client):
    assert client.post("/orders", json={"shop": "pizza", "item": "x"}).status_code == 400
    assert client.post("/orders", json={"shop": "sky_dhaba"}).status_code == 422             # missing item
    assert client.get("/orders/nope").status_code == 404


# ----------------------------------------------------------------------------- stage 1: metrics
def test_metrics_endpoint_has_all_four_types(client):
    text = client.get("/metrics").text
    types = {m.group(2) for m in re.finditer(r"^# TYPE (canteen_\w+) (\w+)$", text, re.M)}
    assert types == {"counter", "gauge", "histogram", "summary"}
    assert 'canteen_orders_waiting{shop="sky_dhaba"} 0.0' in text            # pre-initialised
    assert "_created" not in text


def test_business_metrics_follow_the_lifecycle(client):
    placed, waiting = sample("canteen_orders_placed_total", shop="grito"), sample("canteen_orders_waiting", shop="grito")
    prep_count = sample("canteen_order_prep_seconds_count", shop="grito")
    oid = client.post("/orders", json={"shop": "grito", "item": "corn"}).json()["order_id"]
    assert sample("canteen_orders_placed_total", shop="grito") == placed + 1
    assert sample("canteen_orders_waiting", shop="grito") == waiting + 1
    client.post(f"/orders/{oid}/ready")
    assert sample("canteen_orders_waiting", shop="grito") == waiting              # back down
    assert sample("canteen_order_prep_seconds_count", shop="grito") == prep_count + 1
    assert sample("canteen_order_prep_summary_seconds_count", shop="grito") >= 1
    client.post(f"/orders/{oid}/pickup")
    assert sample("canteen_pickup_delay_seconds_count", shop="grito") >= 1


def test_cancel_adjusts_gauge_only_if_it_was_waiting(client):
    a = client.post("/orders", json={"shop": "sky_dhaba", "item": "x"}).json()["order_id"]
    b = client.post("/orders", json={"shop": "sky_dhaba", "item": "y"}).json()["order_id"]
    client.post(f"/orders/{b}/ready")
    waiting = sample("canteen_orders_waiting", shop="sky_dhaba")
    client.post(f"/orders/{a}/cancel")                                            # was waiting -> gauge -1
    client.post(f"/orders/{b}/cancel")                                            # was ready   -> gauge unchanged
    assert sample("canteen_orders_waiting", shop="sky_dhaba") == waiting - 1
    assert sample("canteen_orders_cancelled_total", shop="sky_dhaba") >= 2


def test_http_metrics_use_route_templates(client):
    oid = client.post("/orders", json={"shop": "cafetogo", "item": "z"}).json()["order_id"]
    client.get(f"/orders/{oid}")
    client.get("/orders/nope")
    text = client.get("/metrics").text
    assert 'canteen_http_requests_total{method="GET",route="/orders/{order_id}",status="200"}' in text
    assert 'canteen_http_requests_total{method="GET",route="/orders/{order_id}",status="404"}' in text
    assert oid not in text                                                        # never the real path
    assert 'route="/metrics"' not in text                                         # scrapes are not counted


# ----------------------------------------------------------------------------- stage 5: logs
def captured_lines(client, do):
    """Run `do(client)` with a tap on the root logger; return the JSON lines it produced."""
    import io
    import json
    import logging

    buf = io.StringIO()
    tap = logging.StreamHandler(buf)
    tap.setFormatter(logging.getLogger().handlers[0].formatter)
    logging.getLogger().addHandler(tap)
    try:
        do(client)
    finally:
        logging.getLogger().removeHandler(tap)
    lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    return [l for l in lines if l["event"] in APP_EVENTS]        # httpx (the test client) logs at INFO too


APP_EVENTS = {"startup", "http_request", "order_placed", "order_ready", "order_picked_up", "order_cancelled", "chaos_changed", "error"}


def test_every_log_line_is_json_with_required_fields(client):
    def flow(c):
        oid = c.post("/orders", json={"shop": "sky_dhaba", "item": "SECRET-NOTE"}, headers={"X-Request-ID": "req-42"}).json()["order_id"]
        c.post(f"/orders/{oid}/ready")
        c.get("/health")
    lines = captured_lines(client, flow)
    assert lines and all({"ts", "level", "service", "event", "msg"} <= set(l) for l in lines)
    events = [l["event"] for l in lines]
    assert events == ["order_placed", "http_request", "order_ready", "http_request"]   # /health is not logged
    placed = lines[0]
    assert placed["request_id"] == "req-42" and placed["shop"] == "sky_dhaba" and placed["service"] == "canteen"
    assert lines[1]["request_id"] == "req-42" and lines[1]["route"] == "/orders" and lines[1]["status_code"] == 201
    assert "SECRET-NOTE" not in "".join(map(str, lines))                     # item text never reaches the logs


def test_request_id_header_is_generated_and_bounded(client):
    r = client.get("/shops")
    assert len(r.headers["X-Request-ID"]) == 12
    r = client.get("/shops", headers={"X-Request-ID": "x" * 500})
    assert r.headers["X-Request-ID"] == "x" * 64


# ----------------------------------------------------------------------------- stage 8: chaos
def test_slow_every_nth_request(client):
    import time

    client.post("/chaos/reset")
    assert client.post("/chaos", json={"slow_every_n": 2, "delay_ms": 120}).json()["slow_every_n"] == 2
    durations = []
    for _ in range(6):
        t0 = time.perf_counter()
        client.get("/orders/nope")
        durations.append(time.perf_counter() - t0)
    client.post("/chaos/reset")
    assert sum(1 for d in durations if d >= 0.12) == 3                       # exactly every second /orders call
    slow_server_side = (sample("canteen_http_request_duration_seconds_bucket", method="GET", route="/orders/{order_id}", le="+Inf")
                        - sample("canteen_http_request_duration_seconds_bucket", method="GET", route="/orders/{order_id}", le="0.1"))
    assert slow_server_side >= 3                                             # the delay is inside the timed window


def test_closed_shop_returns_503_and_counts_as_error(client):
    client.post("/chaos", json={"fail_shop": "grito"})
    before = sample("canteen_http_requests_total", method="POST", route="/orders", status="503")
    assert client.post("/orders", json={"shop": "grito", "item": "corn"}).status_code == 503
    assert client.post("/orders", json={"shop": "sky_dhaba", "item": "chai"}).status_code == 201
    client.post("/chaos/reset")
    assert sample("canteen_http_requests_total", method="POST", route="/orders", status="503") == before + 1
    assert client.post("/chaos", json={"fail_shop": "pizza"}).status_code == 400


def test_demo_counter_only_counts_client_chosen_request_ids(client):
    before = sample("canteen_demo_requests_total")
    client.get("/health")                                                    # no header: not counted (healthchecks!)
    client.get("/shops", headers={"X-Request-ID": "card-001"})
    assert sample("canteen_demo_requests_total") == before + 1
