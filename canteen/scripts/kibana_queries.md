# Kibana queries (KQL) used in the report

Data view: **canteen logs** (`canteen-logs-*`, time field `@timestamp`), created by `scripts/setup_elastic.sh`.
Kibana → Discover → pick the data view → paste a query. Useful columns: `event`, `stall`, `order_id`, `request_id`, `status_code`, `duration_ms`, `msg`.

| # | Purpose | KQL |
|---|---------|-----|
| 1 | Follow one order end to end (placed → ready → picked up) | `order_id : "2bdcba54"` |
| 2 | Follow one HTTP request (the `X-Request-ID` the client sent, or the one the app generated and returned) | `request_id : "load-7-42"` |
| 3 | All errors | `level : "error"` |
| 4 | Slow requests (the every-5th-request fault shows here) | `event : "http_request" and duration_ms > 400` |
| 5 | Failed requests (a closed stall answers 503) | `event : "http_request" and status_code >= 500` |
| 6 | Orders that took long to prepare | `event : "order_ready" and prep_s > 2` |
| 7 | Food that sat at the counter | `event : "order_picked_up" and pickup_delay_s > 1` |
| 8 | Cancellations, and whether the food was already cooked | `event : "order_cancelled" and was_ready : true` |
| 9 | One stall's whole day | `stall : "biryani"` |
| 10 | Chaos changes (what fault was active when) | `event : "chaos_changed"` |
| 11 | App restarts | `event : "startup"` |

## Shell equivalents (Lucene query_string on the same index)

```sh
ES=http://localhost:9200
count() { curl -s --get "$ES/canteen-logs-*/_count" --data-urlencode "q=$1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])'; }
count 'order_id:"2bdcba54"'
count 'level:"error"'
count 'event:"http_request" AND duration_ms:>400'
count 'event:"http_request" AND status_code:>=500'
```

## Reading one stored document

```sh
curl -s "$ES/canteen-logs-*/_search?q=event:order_ready&size=1&pretty"
```

`message` is the original line exactly as the app wrote it; every other field (`event`, `order_id`, `prep_s`, …) was extracted from it by Filebeat's `decode_json_fields`; `@timestamp` was copied from our `ts`; `container.name` was added by `add_docker_metadata`.
