"""canteen — order queue for a university's food shops: the HTTP API.

Metrics are defined in app/metrics.py and only *recorded* here, at the point where the
business event happens; GET /metrics exposes them for Prometheus. Logging is configured in
app/logging_config.py; every business event also writes one JSON line to stdout.

The business rules and the data live in app/store.py (a SQLite file shared with the gRPC
service, see grpc_service/server.py). This module is the HTTP face of them:

    POST /orders            POST /orders/{id}/ready        POST /orders/{id}/pickup
    ------------> waiting ----------------------> ready -------------------------> picked_up
                     |                             |
                     +---- POST /orders/{id}/cancel ----> cancelled

Four shops share the campus: sky_dhaba (chai, pharata), tapal (biryani, pulao), cafetogo
(burger, roll) and grito (corn, ice cream). `item` is free text, never validated against a
menu (scripts/load.py orders from the one above).

Assignment 2 Part A, applied to every application endpoint:
  - one error format for every failure: {"error": {"code", "message", "details"}} (see error_response)
  - input validation by the models below; a bad body or an unknown shop is 422, never 400
  - POST /orders takes an optional Idempotency-Key header: a repeat returns the first order
  - POST /orders is rate limited per client (X-Client-ID header, else client IP): 429 + Retry-After
  - the chaos settings are a resource: GET /chaos, PUT /chaos (replace), DELETE /chaos (reset)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import metrics, store
from app.logging_config import setup_logging
from app.metrics import SHOPS   # fixed and small on purpose: it is a metric label (see metrics.py)
from app.ratelimit import FixedWindowLimiter

setup_logging()
log = structlog.get_logger()

RATE_LIMIT = int(os.environ.get("CANTEEN_RATE_LIMIT", "60"))          # calls per client per window
RATE_WINDOW_S = int(os.environ.get("CANTEEN_RATE_WINDOW_S", "60"))
limiter = FixedWindowLimiter(RATE_LIMIT, RATE_WINDOW_S)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init()
    metrics.bind_waiting_gauge(store.waiting_counts)
    log.info("startup", msg="canteen api started", shops=list(SHOPS), db=store.DB_PATH,
             rate_limit=f"{RATE_LIMIT}/{RATE_WINDOW_S}s", demo_cardinality=metrics.DEMO_CARDINALITY)
    yield
    log.info("shutdown", msg="canteen api stopped")


app = FastAPI(
    title="canteen",
    version="0.2.0",
    description="Order queue for four university food shops. Every error has the shape "
                "`{\"error\": {\"code\", \"message\", \"details\"}}`.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- models
Shop = Literal[SHOPS]            # the OpenAPI spec lists the four values; anything else is a 422
State = Literal[store.STATES]


class NewOrder(BaseModel):
    shop: Shop
    item: str = Field(min_length=1, max_length=store.ITEM_MAX_LEN, examples=["chai"])


class OrderOut(BaseModel):
    order_id: str = Field(examples=["5ae388f2"])
    shop: Shop
    item: str
    state: State
    placed_at: float
    ready_at: float | None
    picked_up_at: float | None
    cancelled_at: float | None


class ErrorBody(BaseModel):
    code: str = Field(examples=["conflict"], description="machine-readable: validation_error | not_found | conflict | rate_limited | shop_closed | internal")
    message: str = Field(examples=["order is ready, cannot become ready"])
    details: list = Field(default_factory=list, description="validation errors: one entry per bad field")


class ErrorResponse(BaseModel):
    error: ErrorBody


class Chaos(BaseModel):
    """Faults for A1 Part E, toggled over HTTP so an experiment needs no restart."""
    slow_every_n: int = Field(0, ge=0, description="delay every n-th /orders request (0 = off); the A1 brief's example is 5")
    delay_ms: int = Field(500, ge=0, le=5000, description="how long the delayed requests sleep")
    fail_shop: Shop | None = Field(None, description="this shop answers 503 'closed' to every new order")


ERROR_CODES = {404: "not_found", 409: "conflict", 422: "validation_error", 429: "rate_limited", 503: "shop_closed", 500: "internal"}
ERROR_DESCRIPTIONS = {404: "Unknown order", 409: "The order's current state does not allow this transition",
                      422: "Invalid input (unknown shop, item not 1-40 chars, malformed body)",
                      429: "Rate limit exceeded for this client; wait Retry-After seconds", 503: "The shop is closed (chaos fault)"}


def errors(*codes: int) -> dict:
    """The error responses a route can return, for the OpenAPI spec."""
    return {c: {"model": ErrorResponse, "description": ERROR_DESCRIPTIONS[c]} for c in codes}


def error_response(status: int, code: str, message: str, details: list | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details or []}}, headers=headers)


@app.exception_handler(RequestValidationError)
async def on_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "problem": e["msg"]} for e in exc.errors()]
    return error_response(422, "validation_error", "invalid input", details)


@app.exception_handler(StarletteHTTPException)
async def on_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return error_response(exc.status_code, ERROR_CODES.get(exc.status_code, "error"), str(exc.detail), headers=getattr(exc, "headers", None))


@app.exception_handler(store.DomainError)
async def on_domain_error(_request: Request, exc: store.DomainError) -> JSONResponse:
    status = {"not_found": 404, "conflict": 409}.get(exc.code, 422)
    return error_response(status, ERROR_CODES[status], str(exc))


# --------------------------------------------------------------------------- middleware
CHAOS = Chaos()
REQUEST_COUNTER = 0


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
        await asyncio.sleep(CHAOS.delay_ms / 1000)     # the fault, inside the timed window
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


# --------------------------------------------------------------------------- routes
@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok", "orders": store.count()}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


@app.get("/shops", tags=["orders"], response_model=dict[str, int], summary="Queue length per shop")
def shops() -> dict:
    """How many orders are waiting to be prepared at each shop right now."""
    return store.waiting_counts()


@app.post("/orders", status_code=201, tags=["orders"], response_model=OrderOut, responses=errors(422, 429, 503),
          summary="Place an order")
def place_order(
    body: NewOrder,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=64,
                                         description="Optional. Repeating a request with the same key returns the first order instead of creating another (safe retries)."),
    client_id: str | None = Header(None, alias="X-Client-ID", max_length=64,
                                   description=f"Optional. Rate-limit key ({RATE_LIMIT} orders per {RATE_WINDOW_S} s per client); the client IP is used if absent."),
) -> dict:
    """Create an order in state `waiting`. Rate limited per client; idempotent with `Idempotency-Key`."""
    key = client_id or (request.client.host if request.client else "unknown")
    allowed, remaining, retry_after = limiter.check(key)
    response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    if not allowed:
        raise HTTPException(429, f"rate limit of {RATE_LIMIT} orders per {RATE_WINDOW_S} s exceeded for client {key!r}",
                            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(RATE_LIMIT), "X-RateLimit-Remaining": "0"})
    if body.shop == CHAOS.fail_shop:
        raise HTTPException(503, f"{body.shop} is closed")           # A1's second fault: a shop that is down
    order, replayed = store.place_order(body.shop, body.item, idempotency_key)
    if replayed:
        metrics.IDEMPOTENT_REPLAYS.labels(api="http").inc()
        response.headers["Idempotent-Replayed"] = "true"
        log.info("order_replayed", msg="repeated request answered with the stored order", order_id=order.order_id, shop=order.shop)
        return order.to_dict()
    metrics.ORDERS_PLACED.labels(shop=order.shop).inc()      # Counter: 20 -> 21
    log.info("order_placed", msg=f"order placed at {order.shop}", order_id=order.order_id, shop=order.shop,
             queue_length=store.waiting_counts()[order.shop])
    return order.to_dict()


@app.get("/orders/{order_id}", tags=["orders"], response_model=OrderOut, responses=errors(404), summary="Show an order")
def show_order(order_id: str) -> dict:
    return store.get_order(order_id).to_dict()


@app.post("/orders/{order_id}/ready", tags=["orders"], response_model=OrderOut, responses=errors(404, 409),
          summary="The shop marks the order ready (waiting -> ready)")
def mark_ready(order_id: str) -> dict:
    order = store.transition(order_id, "ready")
    prep = order.ready_at - order.placed_at
    metrics.ORDER_PREP.labels(shop=order.shop).observe(prep)          # Histogram: which bucket
    metrics.ORDER_PREP_SUMMARY.labels(shop=order.shop).observe(prep)  # Summary: sum and count
    log.log(logging.WARNING if prep > 300 else logging.INFO, "order_ready", msg=f"order ready after {prep:.1f} s",
            order_id=order.order_id, shop=order.shop, prep_s=round(prep, 2))
    return order.to_dict()


@app.post("/orders/{order_id}/pickup", tags=["orders"], response_model=OrderOut, responses=errors(404, 409),
          summary="The customer picks the order up (ready -> picked_up)")
def pick_up(order_id: str) -> dict:
    order = store.transition(order_id, "picked_up")
    delay = order.picked_up_at - order.ready_at
    metrics.PICKUP_DELAY.labels(shop=order.shop).observe(delay)
    log.info("order_picked_up", msg=f"order picked up after waiting {delay:.1f} s at the counter",
             order_id=order.order_id, shop=order.shop, pickup_delay_s=round(delay, 2))
    return order.to_dict()


@app.post("/orders/{order_id}/cancel", tags=["orders"], response_model=OrderOut, responses=errors(404, 409),
          summary="Cancel the order (waiting or ready -> cancelled)")
def cancel(order_id: str) -> dict:
    order = store.transition(order_id, "cancelled")
    metrics.ORDERS_CANCELLED.labels(shop=order.shop).inc()
    log.warning("order_cancelled", msg="order cancelled", order_id=order.order_id, shop=order.shop,
                was_ready=order.ready_at is not None)
    return order.to_dict()


# --------------------------------------------------------------------------- chaos (a resource: GET / PUT / DELETE)
@app.get("/chaos", tags=["chaos"], response_model=Chaos, summary="Current fault settings")
def chaos_status() -> dict:
    return CHAOS.model_dump()


@app.put("/chaos", tags=["chaos"], response_model=Chaos, responses=errors(422), summary="Replace the fault settings")
def chaos_set(body: Chaos) -> dict:
    """Replace the whole settings object. `{"slow_every_n": 5, "delay_ms": 500}` is the A1 brief's example fault."""
    global CHAOS
    CHAOS = body
    log.warning("chaos_changed", msg="chaos settings changed", **CHAOS.model_dump())
    return CHAOS.model_dump()


@app.delete("/chaos", status_code=204, tags=["chaos"], summary="Reset the fault settings to defaults")
def chaos_reset() -> Response:
    global CHAOS
    CHAOS = Chaos()
    log.warning("chaos_changed", msg="chaos reset", **CHAOS.model_dump())
    return Response(status_code=204)
