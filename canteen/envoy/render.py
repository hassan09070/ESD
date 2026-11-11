#!/usr/bin/env python3
"""Generate the Envoy configurations for Assignment 3 (one file per experiment mode).

    python3 envoy/render.py            # writes envoy/<mode>.yaml for every mode below

The four files differ only in which replicas are destinations, their weights and the
balancing policy, so they are rendered from one template instead of being kept in sync by
hand. docker-compose.yml starts Envoy with `-c /etc/envoy/${ENVOY_MODE}.yaml`;
scripts/envoy_mode.sh switches modes by re-creating the container.

    a_only         Part A/B : HTTP and gRPC -> A only; B and C running but not destinations
    round_robin    Part C   : A, B, C, equal weights, ROUND_ROBIN           (the Part D starting point)
    weighted       Part D.1 : A:B:C = 2:1:1, ROUND_ROBIN
    least_request  Part D.2 : A, B, C, equal weights, LEAST_REQUEST (power of two choices)

Everything else is identical in all modes:

  listeners   10000 HTTP/1.1 and 10001 HTTP/2 (gRPC); both are HTTP connection managers, so
              Envoy sees every gRPC *call* and balances per call, not per TCP connection
  clusters    STATIC endpoints (fixed IPs from docker-compose.yml) so only Envoy's own active
              health checks decide membership, never DNS; HTTP/2 to the gRPC cluster
  health      every 2 s, 1 s timeout, 2 failures -> removed, 2 successes -> back
              (HTTP: GET /health; gRPC: grpc.health.v1.Health/Check); events to stdout
  keepalive   HTTP/2 PING every 2 s (1 s timeout) on the gRPC upstream connections, so a
              connection whose peer vanished (stopped container: packets are dropped, no RST)
              is closed within ~3 s and its in-flight calls fail instead of hanging until the
              client's deadline - and, since the client shares ONE HTTP/2 connection to Envoy
              for every replica, instead of stalling calls to the healthy replicas too
  no retries, no outlier detection, circuit breakers raised far above the workload
  access log  one JSON line per request on stdout with the chosen replica (X-Replica response
              header) and the request id (x-request-id, kept if the client sent one, else
              generated) -> Filebeat -> Elasticsearch, same index as the replicas' logs
  admin       9901; Prometheus scrapes /stats/prometheus
"""
from __future__ import annotations

import pathlib

import yaml

HERE = pathlib.Path(__file__).resolve().parent
REPLICAS = {"A": "172.28.0.11", "B": "172.28.0.12", "C": "172.28.0.13"}   # ipv4_address in docker-compose.yml
HTTP_PORT, GRPC_PORT = 8000, 50051

HEALTH = {"timeout": "1s", "interval": "2s", "unhealthy_threshold": 2, "healthy_threshold": 2,
          "no_traffic_interval": "2s",        # default is 60 s: keep probing idle replicas at the same pace
          "unhealthy_interval": "2s", "event_logger": [{
              "name": "envoy.health_check.event_sinks.file",
              "typed_config": {"@type": "type.googleapis.com/envoy.extensions.health_check.event_sinks.file.v3.HealthCheckEventFileSink",
                               "event_log_path": "/dev/stdout"}}]}

MODES = {
    "a_only":        {"members": {"A": 1},                 "policy": "ROUND_ROBIN"},
    "round_robin":   {"members": {"A": 1, "B": 1, "C": 1}, "policy": "ROUND_ROBIN"},
    "weighted":      {"members": {"A": 2, "B": 1, "C": 1}, "policy": "ROUND_ROBIN"},
    "least_request": {"members": {"A": 1, "B": 1, "C": 1}, "policy": "LEAST_REQUEST"},
}


def access_log(listener: str) -> list:
    """JSON access log. Field names match the replicas' log lines (request_id, method, path,
    status_code, duration_ms, grpc_code, replica) so one Kibana search returns both."""
    fmt = {
        "ts": "%START_TIME(%Y-%m-%dT%H:%M:%S.%3fZ)%",
        "level": "info", "service": "envoy", "event": "envoy_access", "listener": listener,
        "replica": "%RESP(X-REPLICA)%",
        "request_id": "%REQ(X-REQUEST-ID)%",
        "method": "%REQ(:METHOD)%", "path": "%REQ(X-ENVOY-ORIGINAL-PATH?:PATH)%", "authority": "%REQ(:AUTHORITY)%",
        "protocol": "%PROTOCOL%", "status_code": "%RESPONSE_CODE%", "grpc_code": "%GRPC_STATUS(SNAKE_STRING)%",
        "response_flags": "%RESPONSE_FLAGS%", "duration_ms": "%DURATION%",
        "upstream_ms": "%RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)%", "upstream_host": "%UPSTREAM_HOST%",
        "upstream_cluster": "%UPSTREAM_CLUSTER%", "bytes_received": "%BYTES_RECEIVED%", "bytes_sent": "%BYTES_SENT%",
        "msg": "%REQ(:METHOD)% %REQ(:PATH)% -> %RESPONSE_CODE% via %RESP(X-REPLICA)%",
    }
    return [{"name": "envoy.access_loggers.stdout",
             "typed_config": {"@type": "type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog",
                              "log_format": {"json_format": fmt}}}]


def listener(name: str, port: int, cluster: str) -> dict:
    return {"name": name, "address": {"socket_address": {"address": "0.0.0.0", "port_value": port}},
            "filter_chains": [{"filters": [{
                "name": "envoy.filters.network.http_connection_manager",
                "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                    "stat_prefix": name, "codec_type": "AUTO",      # HTTP/1.1 from curl, HTTP/2 from gRPC clients
                    "generate_request_id": True, "preserve_external_request_id": True,
                    "access_log": access_log(name),
                    "route_config": {"name": name, "virtual_hosts": [{"name": "canteen", "domains": ["*"], "routes": [
                        {"match": {"prefix": "/"}, "route": {"cluster": cluster, "timeout": "15s"}}]}]},   # no retry_policy
                    "http_filters": [{"name": "envoy.filters.http.router",
                                      "typed_config": {"@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"}}],
                }}]}]}


def cluster(name: str, port: int, members: dict[str, int], policy: str, grpc: bool) -> dict:
    c = {
        "name": name, "type": "STATIC", "connect_timeout": "1s", "lb_policy": policy,
        "circuit_breakers": {"thresholds": [{"priority": "DEFAULT", "max_connections": 10000, "max_pending_requests": 10000,
                                             "max_requests": 10000, "max_retries": 0}]},   # never the limiting factor in Part D
        "health_checks": [dict(HEALTH, **({"grpc_health_check": {}} if grpc else
                                          {"http_health_check": {"path": "/health", "codec_client_type": "HTTP1"}}))],
        "load_assignment": {"cluster_name": name, "endpoints": [{"lb_endpoints": [
            {"endpoint": {"address": {"socket_address": {"address": REPLICAS[r], "port_value": port}}, "hostname": f"app-{r.lower()}"},
             "load_balancing_weight": w} for r, w in members.items()]}]},
    }
    if policy == "LEAST_REQUEST":
        c["least_request_lb_config"] = {"choice_count": 2}     # Envoy's default: pick 2 at random, send to the one with fewer active requests
    if grpc:
        c["typed_extension_protocol_options"] = {"envoy.extensions.upstreams.http.v3.HttpProtocolOptions": {
            "@type": "type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions",
            "explicit_http_config": {"http2_protocol_options": {
                "connection_keepalive": {"interval": "2s", "timeout": "1s"}}}}}   # dead-peer detection, see module docstring
    return c


def render(mode: str) -> dict:
    m = MODES[mode]
    return {
        "admin": {"address": {"socket_address": {"address": "0.0.0.0", "port_value": 9901}}},
        "static_resources": {
            "listeners": [listener("http", 10000, "canteen_http"), listener("grpc", 10001, "canteen_grpc")],
            "clusters": [cluster("canteen_http", HTTP_PORT, m["members"], m["policy"], grpc=False),
                         cluster("canteen_grpc", GRPC_PORT, m["members"], m["policy"], grpc=True)],
        },
    }


if __name__ == "__main__":
    for mode, m in MODES.items():
        out = HERE / f"{mode}.yaml"
        header = (f"# GENERATED by envoy/render.py - do not edit; mode={mode}: destinations "
                  f"{', '.join(f'{r}(w={w})' for r, w in m['members'].items())}, policy {m['policy']}\n")
        out.write_text(header + yaml.safe_dump(render(mode), sort_keys=False, width=120))
        print(f"wrote {out}")
