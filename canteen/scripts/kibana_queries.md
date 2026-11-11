# Kibana queries (KQL) used in the report

Data view: **canteen logs** (`canteen-logs-*`, time field `@timestamp`), created by `scripts/setup_elastic.sh`.
Kibana → Discover → pick the data view → paste a query. Useful columns: `event`, `shop`, `order_id`, `request_id`, `status_code`, `duration_ms`, `msg`; since A3 also `service` (`canteen` = a replica, `envoy` = the proxy), `replica`, `listener`, `grpc_code`, `upstream_ms`, `response_flags`.

| # | Purpose | KQL |
|---|---------|-----|
| 1 | Follow one order end to end (placed → ready → picked up) | `order_id : "134cdd02"` |
| 2 | Follow one HTTP request (the `X-Request-ID` the client sent, or the one the app generated and returned) | `request_id : "4574638028a0"` |
| 3 | All errors | `level : "error"` |
| 4 | Slow requests (the every-5th-request fault shows here) | `event : "http_request" and duration_ms > 400` |
| 5 | Failed requests (a closed shop answers 503) | `event : "http_request" and status_code >= 500` |
| 6 | Orders that took long to prepare | `event : "order_ready" and prep_s > 2` |
| 7 | Food that sat at the counter | `event : "order_picked_up" and pickup_delay_s > 1` |
| 8 | Cancellations, with `was_ready` showing whether the food was already cooked | `event : "order_cancelled"` |
| 9 | One shop's whole day | `shop : "tapal"` |
| 10 | Chaos changes (what fault was active when) | `event : "chaos_changed"` |
| 11 | App restarts | `event : "startup"` |
| 12 | A3: one request end to end: Envoy's access-log event **and** the replica's event (same `request_id`; Envoy generated it, or kept the client's) | `request_id : "4574638028a0"` |
| 13 | A3: everything Envoy sent to replica B (HTTP and gRPC) | `event : "envoy_access" and replica : "B"` |
| 14 | A3: gRPC calls through Envoy and the code they ended with | `event : "envoy_access" and listener : "grpc"` |
| 15 | A3: requests Envoy could not deliver (no healthy upstream, connection refused: response flags UH, UF, NR …) | `event : "envoy_access" and response_flags : * and not response_flags : "-"` |
| 16 | A3: health-check state changes (`eject_unhealthy_event`, `add_healthy_event`) with the exact time | `health_checker_type : *` |
| 17 | A3: one replica's own view of the same traffic | `service : "canteen" and replica : "B" and (event : "http_request" or event : "grpc_request")` |

## Shell equivalents (Lucene query_string on the same index)

```sh
ES=http://localhost:9200
count() { curl -s --get "$ES/canteen-logs-*/_count" --data-urlencode "q=$1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])'; }
count 'order_id:"134cdd02"'
count 'level:"error"'
count 'event:"http_request" AND duration_ms:>400'
count 'event:"http_request" AND status_code:>=500'
```

## Reading one stored document

```sh
curl -s "$ES/canteen-logs-*/_search?q=event:order_ready&size=1&pretty"
```

`message` is the original line exactly as the app (or Envoy) wrote it; every other field (`event`, `order_id`, `prep_s`, …) was extracted from it by Filebeat's `decode_json_fields`; `@timestamp` was copied from our `ts` (Envoy health-check events: `timestamp`); `container.name` was added by `add_docker_metadata`. Envoy's access log uses the same field names as the replicas (`request_id`, `method`, `path`, `status_code`, `grpc_code`, `duration_ms`, `replica`) on purpose, so query 12 shows both events in the same columns.
