"""canteen gRPC service (Assignment 2 Part B): PlaceOrder as a separate process.

Same rules, same data as the HTTP API: the handler calls app.store.place_order, exactly like
POST /orders in app/main.py does, against the SQLite file both containers mount. Nothing here
talks to the HTTP server.

Observability (Part B/C) lives in one interceptor, the gRPC twin of main.py's middleware:
  - request id: taken from the `x-request-id` call metadata if the client sent one, else
    generated; bound as a structlog context variable so every log line of the call carries
    it, and echoed back as trailing metadata. This is what keeps A1's trace correlation
    working across the gRPC boundary (search request_id in Kibana: HTTP and gRPC lines match).
  - metrics (server-side, request arrival -> response or abort): canteen_grpc_server_handled_total
    {grpc_method, grpc_code}, canteen_grpc_server_handling_seconds{grpc_method},
    canteen_grpc_server_in_flight{grpc_method}; exposed on GRPC_METRICS_PORT for Prometheus.
  - one `grpc_request` log line per call, like `http_request`.

Assignment 3: the server normally runs inside a replica process together with the HTTP API
(app/replica.py), so its metrics are exposed by that process's GET /metrics. Each call's
initial metadata carries `x-replica` (Envoy logs it), the standard grpc.health.v1 service is
registered for Envoy's active health checks, and the replica's chaos `fixed_delay_ms`
(app/chaos.py, set over PUT /chaos) delays every RPC.

Run alone:  python -m grpc_service.server    (GRPC_PORT=50051, GRPC_METRICS_PORT=9095, CANTEEN_DB)
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from concurrent import futures

import grpc
import structlog
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from prometheus_client import start_http_server

from app import chaos, metrics, store
from app.logging_config import REPLICA, setup_logging
from grpc_service import canteen_pb2, canteen_pb2_grpc

setup_logging()
log = structlog.get_logger()

GRPC_PORT = int(os.environ.get("GRPC_PORT", "50051"))
METRICS_PORT = int(os.environ.get("GRPC_METRICS_PORT", "9095"))
_HEALTH: health.HealthServicer | None = None


def set_not_serving() -> None:
    """Shutdown (app/replica.py): make Envoy's next grpc_health_check fail before the port closes."""
    if _HEALTH is not None:
        _HEALTH.enter_graceful_shutdown()


class OrderService(canteen_pb2_grpc.OrderServiceServicer):
    def PlaceOrder(self, request: canteen_pb2.PlaceOrderRequest, context: grpc.ServicerContext) -> canteen_pb2.Order:
        try:
            order, replayed = store.place_order(request.shop, request.item, request.idempotency_key or None)
        except store.DomainError as e:                      # unknown shop / bad item: the caller's fault
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        if replayed:
            metrics.IDEMPOTENT_REPLAYS.labels(api="grpc").inc()
            log.info("order_replayed", msg="repeated request answered with the stored order", order_id=order.order_id, shop=order.shop)
        else:
            metrics.ORDERS_PLACED.labels(shop=order.shop).inc()
            log.info("order_placed", msg=f"order placed at {order.shop}", order_id=order.order_id, shop=order.shop,
                     queue_length=store.waiting_counts()[order.shop])
        return canteen_pb2.Order(order_id=order.order_id, shop=order.shop, item=order.item, state=order.state,
                                 placed_at=order.placed_at, replayed=replayed)


class Observe(grpc.ServerInterceptor):
    """Request id, metrics and one log line per call (see module docstring)."""

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None or handler_call_details.method.startswith("/grpc.health."):
            return handler                                                  # health checks are not application traffic
        method = handler_call_details.method.rsplit("/", 1)[-1]           # "PlaceOrder"
        incoming = dict(handler_call_details.invocation_metadata)
        request_id = (incoming.get("x-request-id") or uuid.uuid4().hex[:12])[:64]
        inner = handler.unary_unary

        def observed(request, context: grpc.ServicerContext):
            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(request_id=request_id)
            context.send_initial_metadata((("x-replica", REPLICA),))        # A3: Envoy logs %RESP(x-replica)%
            context.set_trailing_metadata((("x-request-id", request_id),))
            metrics.GRPC_IN_FLIGHT.labels(grpc_method=method).inc()
            start = time.perf_counter()
            try:
                delay_ms = chaos.get().fixed_delay_ms
                if delay_ms:
                    time.sleep(delay_ms / 1000)                             # A3 Part D: the slow replica, inside the timed window
                return inner(request, context)
            except Exception as e:  # noqa: BLE001
                if context.code() is None:                                  # not an abort(): an unexpected failure
                    log.error("error", msg="unhandled exception in RPC", exc_type=type(e).__name__, exc_message=str(e)[:200], exc_info=True)
                    context.abort(grpc.StatusCode.INTERNAL, "internal error")
                raise
            finally:
                duration = time.perf_counter() - start
                code = context.code()
                code_name = code.name if code is not None else "OK"
                metrics.GRPC_IN_FLIGHT.labels(grpc_method=method).dec()
                metrics.GRPC_HANDLED.labels(grpc_method=method, grpc_code=code_name).inc()
                metrics.GRPC_DURATION.labels(grpc_method=method).observe(duration)
                log.log(logging.ERROR if code_name in ("INTERNAL", "UNKNOWN") else logging.INFO, "grpc_request",
                        msg=f"{method} -> {code_name}", grpc_method=method, grpc_code=code_name, duration_ms=round(duration * 1000, 1))
                structlog.contextvars.clear_contextvars()

        return grpc.unary_unary_rpc_method_handler(observed, request_deserializer=handler.request_deserializer,
                                                   response_serializer=handler.response_serializer)


def serve(port: int = GRPC_PORT, metrics_port: int | None = METRICS_PORT) -> tuple[grpc.Server, int]:
    """Start the server; returns it and the bound port (port=0 picks a free one, for tests)."""
    store.init()
    metrics.bind_waiting_gauge(store.waiting_counts)
    if metrics_port:
        start_http_server(metrics_port)                       # GET /metrics for Prometheus
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=int(os.environ.get("GRPC_WORKERS", "32"))), interceptors=[Observe()])
    canteen_pb2_grpc.add_OrderServiceServicer_to_server(OrderService(), server)
    # grpc.health.v1.Health/Check: what Envoy's grpc_health_check calls (A3 Part D). SERVING for
    # the whole server ("") and for the one service by name.
    global _HEALTH
    health_servicer = health.HealthServicer(experimental_non_blocking=True, experimental_thread_pool=futures.ThreadPoolExecutor(max_workers=2))
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    for name in ("", canteen_pb2.DESCRIPTOR.services_by_name["OrderService"].full_name):
        health_servicer.set(name, health_pb2.HealthCheckResponse.SERVING)
    _HEALTH = health_servicer
    bound = server.add_insecure_port(f"[::]:{port}")
    server.start()
    log.info("startup", msg="canteen grpc started", grpc_port=bound, metrics_port=metrics_port, db=store.DB_PATH, replica=REPLICA)
    return server, bound


if __name__ == "__main__":
    srv, _ = serve()
    srv.wait_for_termination()
