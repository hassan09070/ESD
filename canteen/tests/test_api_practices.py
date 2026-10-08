"""Assignment 2 Part A: consistent errors, idempotent repeats, rate limiting, and the OpenAPI
spec used to check an endpoint. In-process, no Docker."""
import json
import pathlib

import jsonschema
import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import RATE_LIMIT, RATE_WINDOW_S, app, limiter
from prometheus_client import REGISTRY  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


@pytest.fixture
def client():
    store.init()
    store.reset()
    limiter.reset()
    with TestClient(app) as c:
        yield c


# ----------------------------------------------------------------------------- one error format
def test_every_error_has_the_same_shape(client):
    oid = client.post("/orders", json={"shop": "tapal", "item": "pulao"}).json()["order_id"]
    cases = {
        404: client.get("/orders/nope"),
        409: client.post(f"/orders/{oid}/pickup"),                            # waiting -> picked_up is not allowed
        422: client.post("/orders", json={"shop": "pizza", "item": "x"}),      # unknown shop
    }
    expected_code = {404: "not_found", 409: "conflict", 422: "validation_error"}
    for status, r in cases.items():
        assert r.status_code == status
        body = r.json()
        assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
        assert body["error"]["code"] == expected_code[status]
        assert isinstance(body["error"]["details"], list)
    missing_item = client.post("/orders", json={"shop": "tapal"}).json()["error"]
    assert missing_item["details"] == [{"field": "item", "problem": "Field required"}]


# ----------------------------------------------------------------------------- idempotency
def test_repeating_a_post_with_the_same_idempotency_key_creates_one_order(client):
    headers = {"Idempotency-Key": "cust-7-order-1"}
    first = client.post("/orders", json={"shop": "cafetogo", "item": "roll"}, headers=headers)
    repeat = client.post("/orders", json={"shop": "cafetogo", "item": "roll"}, headers=headers)
    assert first.status_code == repeat.status_code == 201
    assert first.json() == repeat.json()                                      # the stored order, not a new one
    assert repeat.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in first.headers
    assert store.count() == 1
    assert sample("canteen_idempotent_replays_total", api="http") >= 1
    other = client.post("/orders", json={"shop": "cafetogo", "item": "roll"}, headers={"Idempotency-Key": "cust-7-order-2"})
    assert other.json()["order_id"] != first.json()["order_id"] and store.count() == 2


def test_repeating_a_transition_is_safe(client):
    """ready/pickup/cancel have no idempotency key: a repeat is a 409 and changes nothing."""
    oid = client.post("/orders", json={"shop": "grito", "item": "corn"}).json()["order_id"]
    first = client.post(f"/orders/{oid}/ready")
    repeat = client.post(f"/orders/{oid}/ready")
    assert first.status_code == 200 and repeat.status_code == 409
    assert client.get(f"/orders/{oid}").json()["ready_at"] == first.json()["ready_at"]   # the timestamp did not move


# ----------------------------------------------------------------------------- rate limit
def test_rate_limit_accepts_then_returns_429_with_retry_after(client):
    headers = {"X-Client-ID": "rl-test"}
    for i in range(RATE_LIMIT):
        r = client.post("/orders", json={"shop": "tapal", "item": "biryani"}, headers=headers)
        assert r.status_code == 201
        assert r.headers["X-RateLimit-Remaining"] == str(RATE_LIMIT - i - 1)
    r = client.post("/orders", json={"shop": "tapal", "item": "biryani"}, headers=headers)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= RATE_WINDOW_S
    assert r.headers["X-RateLimit-Remaining"] == "0"
    assert r.json()["error"]["code"] == "rate_limited"
    # the limit is per client: another client id is not affected
    assert client.post("/orders", json={"shop": "tapal", "item": "biryani"}, headers={"X-Client-ID": "someone-else"}).status_code == 201
    # a rejected call is counted like any other response
    assert sample("canteen_http_requests_total", method="POST", route="/orders", status="429") >= 1


# ----------------------------------------------------------------------------- OpenAPI
def test_openapi_spec_matches_routes_and_the_committed_file(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert set(paths) >= {"/orders", "/orders/{order_id}", "/orders/{order_id}/ready", "/orders/{order_id}/pickup",
                          "/orders/{order_id}/cancel", "/shops", "/chaos", "/health"}
    assert set(paths["/orders"]["post"]["responses"]) == {"201", "422", "429", "503"}
    assert set(paths["/orders/{order_id}/ready"]["post"]["responses"]) == {"200", "404", "409", "422"}
    assert set(paths["/chaos"]) == {"get", "put", "delete"}
    assert spec["components"]["schemas"]["NewOrder"]["properties"]["shop"]["enum"] == ["sky_dhaba", "tapal", "cafetogo", "grito"]
    assert json.loads((ROOT / "openapi.json").read_text()) == spec        # scripts/export_openapi.py keeps the file current


def test_live_responses_validate_against_the_spec(client):
    """Use the spec to test one endpoint: the success and an error body of POST /orders."""
    spec = client.get("/openapi.json").json()

    def schema(name):
        return {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}

    ok = client.post("/orders", json={"shop": "sky_dhaba", "item": "chai"})
    assert ok.status_code == 201
    jsonschema.validate(ok.json(), schema("OrderOut"))
    bad = client.post("/orders", json={"shop": "pizza", "item": "chai"})
    assert bad.status_code == 422
    jsonschema.validate(bad.json(), schema("ErrorResponse"))
