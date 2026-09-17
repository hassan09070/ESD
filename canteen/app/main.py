"""canteen — a university canteen order queue.

Metrics are defined in app/metrics.py and only *recorded* here, at the point where the
business event happens; GET /metrics exposes them for Prometheus. Logging is configured in
app/logging_config.py; every business event also writes one JSON line to stdout.

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

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import metrics
from app.logging_config import setup_logging
from app.metrics import STALLS   # fixed and small on purpose: it is a metric label (see metrics.py)

setup_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("startup", msg="canteen api started", stalls=list(STALLS), demo_cardinality=metrics.DEMO_CARDINALITY)
    yield
    log.info("shutdown", msg="canteen api stopped")


app = FastAPI(title="canteen", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def observe_http(request: Request, call_next):
    """The two application metrics, recorded for every request in one place.
    `route` is the *template* (/orders/{order_id}/ready), never the real path: a label
    per order id would be one time series per order."""
    # request_id: taken from the client's X-Request-ID header if it sent one (the load
    # generator and the E.2 script do), else generated. Bound as a context variable so every
    # log line written while handling this request carries it, and echoed back in the response.
    request_id = (request.headers.get("x-request-id") or uuid.uuid4().hex[:12])[:64]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    if "x-request-id" in request.headers:              # E.2: only ids a client chose become label values;
        metrics.record_demo_request(request_id)        # generated ones (healthchecks, browsers) never do
    start = time.perf_counter()
    global REQUEST_COUNTER
    REQUEST_COUNTER += 1
    if CHAOS.slow_every_n and REQUEST_COUNTER % CHAOS.slow_every_n == 0 and request.url.path.startswith("/orders"):
        # The fault: a slow "database" on every n-th order call. It sits *inside* the timed
        # window - my first version slept before `start` and the server-side histogram never
        # saw it (REPORT D.4).
        await asyncio.sleep(CHAOS.delay_ms / 1000)
    status = 500                                   # if call_next raises, that is what the client gets
    try:
        response: Response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception as e:  # noqa: BLE001
        log.error("error", msg="unhandled exception", exc_type=type(e).__name__, exc_message=str(e)[:200], exc_info=True)
        raise
    finally:
        duration = time.perf_counter() - start
        route = request.scope.get("route")
        route = route.path if route is not None else "unmatched"
        if route != "/metrics":                    # Prometheus' own scrapes would drown the request panels
            metrics.HTTP_REQUESTS.labels(method=request.method, route=route, status=str(status)).inc()
            metrics.HTTP_DURATION.labels(method=request.method, route=route).observe(duration)
        if route not in ("/metrics", "/health"):   # ... and the healthcheck would fill Kibana with noise
            log.log(logging.ERROR if status >= 500 else logging.INFO, "http_request",
                    msg=f"{request.method} {request.url.path} -> {status}", method=request.method, path=request.url.path,
                    route=route, status_code=status, duration_ms=round(duration * 1000, 1))
        structlog.contextvars.clear_contextvars()


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


# ------------------------------------------------------------------ chaos (Part E.1)
# Faults are toggled over HTTP, like Lab 1's chaos endpoints, so an experiment needs no
# restart and the "remove the problem" step is one request.
class Chaos(BaseModel):
    slow_every_n: int = Field(0, ge=0, description="delay every n-th request (0 = off); the brief's example is 5")
    delay_ms: int = Field(500, ge=0, le=5000, description="how long the delayed requests sleep")
    fail_stall: str | None = Field(None, description="this stall answers 503 'closed' to every new order")


CHAOS = Chaos()
REQUEST_COUNTER = 0


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
    if body.stall == CHAOS.fail_stall:
        raise HTTPException(503, f"{body.stall} is closed")           # the second fault: a stall that is down
    order = Order(order_id=uuid.uuid4().hex[:8], stall=body.stall, item=body.item, placed_at=time.time())
    ORDERS[order.order_id] = order
    metrics.ORDERS_PLACED.labels(stall=order.stall).inc()      # Counter: 20 -> 21
    metrics.ORDERS_WAITING.labels(stall=order.stall).inc()     # Gauge: one more in the queue
    log.info("order_placed", msg=f"order placed at {order.stall}", order_id=order.order_id, stall=order.stall,
             queue_length=stalls()[order.stall])
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
    log.log(logging.WARNING if prep > 300 else logging.INFO, "order_ready", msg=f"order ready after {prep:.1f} s",
            order_id=order.order_id, stall=order.stall, prep_s=round(prep, 2))
    return asdict(order)


@app.post("/orders/{order_id}/pickup")
def pick_up(order_id: str) -> dict:
    order = transition(get_order(order_id), ("ready",), "picked_up")
    order.picked_up_at = time.time()
    delay = order.picked_up_at - order.ready_at
    metrics.PICKUP_DELAY.labels(stall=order.stall).observe(delay)
    log.info("order_picked_up", msg=f"order picked up after waiting {delay:.1f} s at the counter",
             order_id=order.order_id, stall=order.stall, pickup_delay_s=round(delay, 2))
    return asdict(order)


@app.post("/orders/{order_id}/cancel")
def cancel(order_id: str) -> dict:
    was_waiting = get_order(order_id).state == "waiting"
    order = transition(get_order(order_id), ("waiting", "ready"), "cancelled")
    order.cancelled_at = time.time()
    if was_waiting:
        metrics.ORDERS_WAITING.labels(stall=order.stall).dec()
    metrics.ORDERS_CANCELLED.labels(stall=order.stall).inc()
    log.warning("order_cancelled", msg="order cancelled", order_id=order.order_id, stall=order.stall,
                was_ready=not was_waiting)
    return asdict(order)


# --------------------------------------------------------------------------- chaos routes
@app.get("/chaos")
def chaos_status() -> dict:
    return CHAOS.model_dump()


@app.post("/chaos")
def chaos_set(body: Chaos) -> dict:
    """Replace the chaos settings. `{"slow_every_n": 5, "delay_ms": 500}` is the assignment's example fault."""
    global CHAOS
    if body.fail_stall is not None and body.fail_stall not in STALLS:
        raise HTTPException(400, f"unknown stall; choose one of {STALLS}")
    CHAOS = body
    log.warning("chaos_changed", msg="chaos settings changed", **CHAOS.model_dump())
    return CHAOS.model_dump()


@app.post("/chaos/reset")
def chaos_reset() -> dict:
    global CHAOS
    CHAOS = Chaos()
    log.warning("chaos_changed", msg="chaos reset", **CHAOS.model_dump())
    return CHAOS.model_dump()
