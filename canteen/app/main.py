"""canteen — a university canteen order queue.

Stage 1: the app plus Prometheus metrics. Every metric is defined in app/metrics.py; this
file only *records* them at the point where the business event happens, and exposes them
on GET /metrics for Prometheus to scrape.

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

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import metrics
from app.metrics import STALLS   # fixed and small on purpose: it is a metric label (see metrics.py)

app = FastAPI(title="canteen", version="0.1.0")


@app.middleware("http")
async def observe_http(request: Request, call_next):
    """The two application metrics, recorded for every request in one place.
    `route` is the *template* (/orders/{order_id}/ready), never the real path: a label
    per order id would be one time series per order."""
    start = time.perf_counter()
    status = 500                                   # if call_next raises, that is what the client gets
    try:
        response: Response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route = route.path if route is not None else "unmatched"
        if route != "/metrics":                    # Prometheus' own scrapes would drown the request panels
            metrics.HTTP_REQUESTS.labels(method=request.method, route=route, status=str(status)).inc()
            metrics.HTTP_DURATION.labels(method=request.method, route=route).observe(time.perf_counter() - start)


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


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


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
    metrics.ORDERS_PLACED.labels(stall=order.stall).inc()      # Counter: 20 -> 21
    metrics.ORDERS_WAITING.labels(stall=order.stall).inc()     # Gauge: one more in the queue
    return asdict(order)


@app.get("/orders/{order_id}")
def show_order(order_id: str) -> dict:
    return asdict(get_order(order_id))


@app.post("/orders/{order_id}/ready")
def mark_ready(order_id: str) -> dict:
    order = transition(get_order(order_id), ("waiting",), "ready")
    order.ready_at = time.time()
    metrics.ORDERS_WAITING.labels(stall=order.stall).dec()     # Gauge: one fewer in the queue
    prep = order.ready_at - order.placed_at
    metrics.ORDER_PREP.labels(stall=order.stall).observe(prep)          # Histogram: which bucket
    metrics.ORDER_PREP_SUMMARY.labels(stall=order.stall).observe(prep)  # Summary: sum and count
    return asdict(order)


@app.post("/orders/{order_id}/pickup")
def pick_up(order_id: str) -> dict:
    order = transition(get_order(order_id), ("ready",), "picked_up")
    order.picked_up_at = time.time()
    metrics.PICKUP_DELAY.labels(stall=order.stall).observe(order.picked_up_at - order.ready_at)
    return asdict(order)


@app.post("/orders/{order_id}/cancel")
def cancel(order_id: str) -> dict:
    was_waiting = get_order(order_id).state == "waiting"
    order = transition(get_order(order_id), ("waiting", "ready"), "cancelled")
    order.cancelled_at = time.time()
    if was_waiting:
        metrics.ORDERS_WAITING.labels(stall=order.stall).dec()
    metrics.ORDERS_CANCELLED.labels(stall=order.stall).inc()
    return asdict(order)
