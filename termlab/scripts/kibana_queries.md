# Kibana queries (KQL) used in the report

Data view: `termlab-logs-*` (time field `@timestamp`, created by `scripts/setup_elastic.sh`).
Open Kibana → Discover → pick the data view → paste a query into the search bar.
The `curl` line under each one is the Elasticsearch equivalent you can run from a shell.

| # | Purpose | KQL |
|---|---------|-----|
| 1 | Trace one session end-to-end (replace the id) | `session_id : "386ed51a5c39"` |
| 2 | Trace one HTTP request (the `X-Request-ID` a client sent) | `request_id : "phase3-check"` |
| 3 | All errors | `level : "error"` |
| 4 | Slow spawns (the cold_start fault shows here) | `event : "sandbox_spawn" and spawn_ms > 2000` |
| 5 | Cold vs warm spawns | `event : "sandbox_spawn" and source : "cold"` |
| 6 | Users who had to wait for a slot | `event : "queue_wait" and queue_ms > 0` |
| 7 | Users turned away (queue timeout / pool full) | `event : "limit_hit"` |
| 8 | Sandboxes reaped for idling | `event : "sandbox_reaped" and reason : "idle"` |
| 9 | Heavy terminals (lots of typing) | `event : "ws_detach" and bytes_in > 10000` |
| 10 | 5xx HTTP responses | `event : "http_request" and status_code >= 500` |
| 11 | Orphan cleanups after an api restart | `event : "orphan_cleanup"` |
| 12 | Which fault mode was active when | `event : "startup"` (column `fault_mode`) |

Useful Discover columns: `event`, `session_id`, `source`, `spawn_ms`, `queue_ms`, `reason`, `duration_ms`, `msg`.

## Shell equivalents (Lucene query_string on the same index)

```sh
ES=http://localhost:9200
count() { curl -s --get "$ES/termlab-logs-*/_count" --data-urlencode "q=$1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])'; }
count 'session_id:"386ed51a5c39"'
count 'level:"error"'
count 'event:"sandbox_spawn" AND spawn_ms:>2000'
count 'event:"queue_wait" AND queue_ms:>0'
count 'event:"limit_hit"'
```

## Reading one document

```sh
curl -s "$ES/termlab-logs-*/_search?q=event:sandbox_spawn&size=1&pretty"
```
The `message` field holds the original line exactly as the app wrote it; every other
field (`event`, `session_id`, `spawn_ms`, …) was extracted from it by Filebeat's
`decode_json_fields` processor. `@timestamp` was copied from our `ts`.
