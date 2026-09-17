"""Stage 0: the order lifecycle, no Docker needed (TestClient runs the app in-process)."""
import pytest
from fastapi.testclient import TestClient

from app.main import ORDERS, app


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
