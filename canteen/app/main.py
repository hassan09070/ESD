"""canteen — a university canteen order queue.

Stage 0: the application alone. No metrics, no logging config yet; those are added one
stage at a time so each tool can be understood on its own.

The whole "business" is this state machine, one per order:

    POST /orders            POST /orders/{id}/ready        POST /orders/{id}/pickup
    ------------> waiting ----------------------> ready -------------------------> picked_up
                     |                             |
                     +---- POST /orders/{id}/cancel ----> cancelled

Four stalls share the canteen; each order belongs to exactly one stall. Everything lives
in one in-memory dict, so restarting the process forgets every order. That is deliberate:
this app exists to be observed, not to be a real ordering system.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

STALLS = ("chai", "biryani", "shawarma", "juice")   # fixed and small on purpose (stage 1 will explain why)

app = FastAPI(title="canteen", version="0.1.0")


@dataclass
class Order:
    order_id: str
    stall: str
    item: str
    state: str = "waiting"                 # waiting | ready | picked_up | cancelled
    placed_at: float = 0.0                 # time.time() when each transition happened
    ready_at: float | None = None
    picked_up_at: float | None = None
    cancelled_at: float | None = None


ORDERS: dict[str, Order] = {}              # the entire database


class NewOrder(BaseModel):
    stall: str
    item: str = Field(min_length=1, max_length=40)


def get_order(order_id: str) -> Order:
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(404, "unknown order")
    return order


def transition(order: Order, allowed_from: tuple[str, ...], new_state: str) -> Order:
    """Move an order to `new_state` if its current state permits it, else 409 Conflict.
    Every route below is this function plus a timestamp."""
    if order.state not in allowed_from:
        raise HTTPException(409, f"order is {order.state}, cannot become {new_state}")
    order.state = new_state
    return order


# --------------------------------------------------------------------------- routes
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "orders": len(ORDERS)}


@app.get("/stalls")
def stalls() -> dict:
    """Queue length per stall: how many orders are waiting to be prepared right now."""
    return {stall: sum(1 for o in ORDERS.values() if o.stall == stall and o.state == "waiting") for stall in STALLS}


@app.post("/orders", status_code=201)
def place_order(body: NewOrder) -> dict:
    if body.stall not in STALLS:
        raise HTTPException(400, f"unknown stall; choose one of {STALLS}")
    order = Order(order_id=uuid.uuid4().hex[:8], stall=body.stall, item=body.item, placed_at=time.time())
    ORDERS[order.order_id] = order
    return asdict(order)


@app.get("/orders/{order_id}")
def show_order(order_id: str) -> dict:
    return asdict(get_order(order_id))


@app.post("/orders/{order_id}/ready")
def mark_ready(order_id: str) -> dict:
    order = transition(get_order(order_id), ("waiting",), "ready")
    order.ready_at = time.time()
    return asdict(order)


@app.post("/orders/{order_id}/pickup")
def pick_up(order_id: str) -> dict:
    order = transition(get_order(order_id), ("ready",), "picked_up")
    order.picked_up_at = time.time()
    return asdict(order)


@app.post("/orders/{order_id}/cancel")
def cancel(order_id: str) -> dict:
    order = transition(get_order(order_id), ("waiting", "ready"), "cancelled")
    order.cancelled_at = time.time()
    return asdict(order)
