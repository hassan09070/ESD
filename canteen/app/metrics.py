"""Every Prometheus metric of the project is defined here and nowhere else; both processes
(the HTTP app and the gRPC service) import this module and record into their own registry,
and Prometheus scrapes each. Counters and histograms are summed across the two jobs in the
dashboards; the waiting gauge is derived from the shared store at scrape time (see
bind_waiting_gauge), so both processes report the same true value.

Naming follows the Prometheus conventions: a `canteen_` prefix, base units in the name
(`_seconds`), `_total` on counters. Labels are bounded: `shop` (4 values), `route` (templates),
`status`/`grpc_code` (status codes), `grpc_method` (RPC names), `api` (http|grpc), `replica` (A|B|C). A label whose
values can grow without limit (order id, customer, request id) would create a new time series
per value - the "cardinality explosion" of A1 Part E.2, demonstrated on purpose by
`canteen_demo_requests_total`.

Four metric types, and what each one is good for here:
  Counter    only goes up; ask Prometheus for its rate     -> orders placed, HTTP/gRPC calls
  Gauge      goes up and down; read as-is                  -> orders waiting right now, in-flight RPCs
  Histogram  counts observations into buckets (<= le)      -> latencies; p50/p95 via histogram_quantile
  Summary    sum + count only in the Python client         -> the mean of the same latency
"""
from __future__ import annotations

import os
from collections.abc import Callable

# The client also emits a `<name>_created` gauge per series by default; that doubles the
# series count for no benefit. Must be set before prometheus_client is imported.
os.environ.setdefault("PROMETHEUS_DISABLE_CREATED_SERIES", "True")

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, Summary, generate_latest  # noqa: E402

SHOPS = ("sky_dhaba", "tapal", "cafetogo", "grito")

# ---------------------------------------------------------------- business metrics
ORDERS_PLACED = Counter("canteen_orders_placed_total", "Orders placed, by shop", ["shop"])
ORDERS_CANCELLED = Counter("canteen_orders_cancelled_total", "Orders cancelled before pickup, by shop", ["shop"])
# A2 Part C fix: two processes now change orders, so the queue length is read from the shared
# store at scrape time (bind_waiting_gauge) instead of being inc/dec'd in one process's memory.
ORDERS_WAITING = Gauge("canteen_orders_waiting", "Orders placed but not yet ready (the queue at each shop), read from the store", ["shop"])
# prep time = placed -> ready. Load tests take seconds; a real canteen takes minutes; buckets cover both.
PREP_BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)
ORDER_PREP = Histogram("canteen_order_prep_seconds", "Time from order placed to order ready, by shop", ["shop"], buckets=PREP_BUCKETS)
ORDER_PREP_SUMMARY = Summary("canteen_order_prep_summary_seconds", "Same quantity as canteen_order_prep_seconds as a Summary (sum/count only: Python summaries have no quantiles)", ["shop"])
# self-explored (A1): how long ready food waits at the counter before someone picks it up
PICKUP_DELAY = Histogram("canteen_pickup_delay_seconds", "Time from order ready to picked up (food going cold), by shop", ["shop"], buckets=PREP_BUCKETS)
# A2 Part A/C: a repeated request with a known Idempotency-Key returned the first result instead of a second order
IDEMPOTENT_REPLAYS = Counter("canteen_idempotent_replays_total", "Place-order requests answered from a stored idempotency key instead of creating a new order", ["api"])

# ---------------------------------------------------------------- application metrics: HTTP
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5)
HTTP_REQUESTS = Counter("canteen_http_requests_total", "HTTP requests handled, by method, route template and status code", ["method", "route", "status"])
HTTP_DURATION = Histogram("canteen_http_request_duration_seconds", "HTTP request latency, by method and route template", ["method", "route"], buckets=HTTP_BUCKETS)
# A3 Part B: "active requests" per replica. The gRPC twin is GRPC_IN_FLIGHT below.
HTTP_IN_FLIGHT = Gauge("canteen_http_in_flight", "HTTP requests currently being handled (excluding /metrics)")

# A3 Part A: which replica this process is. The value is the label; the series is always 1.
# Prometheus also attaches replica=A|B|C to every series of a target (prometheus.yml), which is
# what the dashboards group by; this metric exists so the identity is visible in /metrics itself.
REPLICA_INFO = Gauge("canteen_replica_info", "Replica identity of this process (always 1)", ["replica"])

# ---------------------------------------------------------------- application metrics: gRPC (A2 Part C)
# Measured at the server, inside the interceptor, from request arrival to response/abort.
GRPC_HANDLED = Counter("canteen_grpc_server_handled_total", "gRPC calls completed, by method and status code", ["grpc_method", "grpc_code"])
GRPC_DURATION = Histogram("canteen_grpc_server_handling_seconds", "gRPC server-side handling time, by method", ["grpc_method"], buckets=HTTP_BUCKETS)
GRPC_IN_FLIGHT = Gauge("canteen_grpc_server_in_flight", "gRPC calls currently being handled, by method", ["grpc_method"])

# ---------------------------------------------------------------- A1 Part E.2: the deliberate mistake
# With CANTEEN_DEMO_CARDINALITY=1 this counter gets a `request_id` label: one series per request.
DEMO_CARDINALITY = os.environ.get("CANTEEN_DEMO_CARDINALITY", "0") == "1"
if DEMO_CARDINALITY:
    DEMO_REQUESTS = Counter("canteen_demo_requests_total", "DEMO ONLY: requests labelled by request_id (cardinality experiment)", ["request_id"])
else:
    DEMO_REQUESTS = Counter("canteen_demo_requests_total", "DEMO ONLY: the same counter without the request_id label")


def record_demo_request(request_id: str) -> None:
    if DEMO_CARDINALITY:
        DEMO_REQUESTS.labels(request_id=request_id).inc()
    else:
        DEMO_REQUESTS.inc()


# Pre-create the series that panels divide or compare, so a shop/method with no events yet
# shows 0 (and rate() = 0) instead of "no data" in Grafana.
REPLICA_INFO.labels(replica=os.environ.get("CANTEEN_REPLICA") or os.uname().nodename).set(1)
HTTP_IN_FLIGHT.set(0)
for _shop in SHOPS:
    ORDERS_PLACED.labels(shop=_shop)
    ORDERS_CANCELLED.labels(shop=_shop)
for _api in ("http", "grpc"):
    IDEMPOTENT_REPLAYS.labels(api=_api)
for _code in ("OK", "INVALID_ARGUMENT", "INTERNAL"):
    GRPC_HANDLED.labels(grpc_method="PlaceOrder", grpc_code=_code)
GRPC_IN_FLIGHT.labels(grpc_method="PlaceOrder")


def bind_waiting_gauge(counts: Callable[[], dict[str, int]]) -> None:
    """Make canteen_orders_waiting read the queue length from `counts()` (the store) on every scrape."""
    for shop in SHOPS:
        ORDERS_WAITING.labels(shop=shop).set_function(lambda s=shop: counts().get(s, 0))


def render() -> tuple[bytes, str]:
    """The text Prometheus scrapes from GET /metrics."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
