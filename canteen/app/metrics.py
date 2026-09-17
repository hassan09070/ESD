"""Every Prometheus metric of the app is defined here and nowhere else.

Naming follows the Prometheus conventions: a `canteen_` prefix, base units in the name
(`_seconds`), `_total` on counters. The only label with more than a handful of values is
`stall`, and it is bounded by STALLS (4 values): a label value that can grow without limit
(order id, customer, request id) would create a new time series per value - that is the
"cardinality explosion" of Part E.2, demonstrated on purpose by `canteen_demo_requests_total`.

Four metric types, and what each one is good for here:
  Counter    only goes up; ask Prometheus for its rate     -> orders placed, HTTP requests
  Gauge      goes up and down; read as-is                  -> orders waiting right now
  Histogram  counts observations into buckets (<= le)      -> latencies; p95/p99 via histogram_quantile
  Summary    sum + count only in the Python client         -> the mean of the same latency
"""
from __future__ import annotations

import os

# The client also emits a `<name>_created` gauge per series by default; that doubles the
# series count for no benefit. Must be set before prometheus_client is imported.
os.environ.setdefault("PROMETHEUS_DISABLE_CREATED_SERIES", "True")

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, Summary, generate_latest  # noqa: E402

STALLS = ("chai", "biryani", "shawarma", "juice")

# ---------------------------------------------------------------- business metrics
ORDERS_PLACED = Counter("canteen_orders_placed_total", "Orders placed, by stall", ["stall"])
ORDERS_CANCELLED = Counter("canteen_orders_cancelled_total", "Orders cancelled before pickup, by stall", ["stall"])
ORDERS_WAITING = Gauge("canteen_orders_waiting", "Orders placed but not yet ready (the queue at each stall)", ["stall"])
# prep time = placed -> ready. Load tests take seconds; a real canteen takes minutes; buckets cover both.
PREP_BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)
ORDER_PREP = Histogram("canteen_order_prep_seconds", "Time from order placed to order ready, by stall", ["stall"], buckets=PREP_BUCKETS)
ORDER_PREP_SUMMARY = Summary("canteen_order_prep_summary_seconds", "Same quantity as canteen_order_prep_seconds as a Summary (sum/count only: Python summaries have no quantiles)", ["stall"])
# self-explored: how long ready food waits at the counter before someone picks it up
PICKUP_DELAY = Histogram("canteen_pickup_delay_seconds", "Time from order ready to picked up (food going cold), by stall", ["stall"], buckets=PREP_BUCKETS)

# ---------------------------------------------------------------- application metrics
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5)
HTTP_REQUESTS = Counter("canteen_http_requests_total", "HTTP requests handled, by method, route template and status code", ["method", "route", "status"])
HTTP_DURATION = Histogram("canteen_http_request_duration_seconds", "HTTP request latency, by method and route template", ["method", "route"], buckets=HTTP_BUCKETS)

# ---------------------------------------------------------------- Part E.2: the deliberate mistake
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


# Pre-create every stall series so that a stall with no orders yet shows 0 (and rate() = 0)
# instead of "no data" in Grafana.
for _stall in STALLS:
    ORDERS_PLACED.labels(stall=_stall)
    ORDERS_CANCELLED.labels(stall=_stall)
    ORDERS_WAITING.labels(stall=_stall)


def render() -> tuple[bytes, str]:
    """The text Prometheus scrapes from GET /metrics."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
