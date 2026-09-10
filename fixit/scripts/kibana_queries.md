# Kibana queries (KQL) used in the report

Data view: `fixit-logs-*` (time field `@timestamp`, created by `scripts/setup_elastic.sh`).
Open Kibana → Discover → pick the data view → paste a query into the search bar.
Every query below was verified against a 20-task `scripts/load.py` run; the `curl` line
under each one is the Elasticsearch equivalent you can run from a shell to get a count.

| # | Purpose | KQL |
|---|---------|-----|
| 1 | Trace one run end-to-end (replace the id) | `run_id : "a3f9c1d2e8b7"` |
| 2 | Trace one CLI invocation (the `X-Request-ID` the CLI sent) | `request_id : "3054e7cb1961"` |
| 3 | All errors | `level : "error"` |
| 4 | Slow LLM calls (the slow fault shows here) | `event : "llm_call" and duration_ms > 3000` |
| 5 | Retries during the flaky fault | `event : "llm_retry"` |
| 6 | Failed tasks | `event : "task_finished" and outcome : "failed"` |
| 7 | Tasks that hit the iteration cap | `event : "task_finished" and outcome : "aborted"` |
| 8 | Sandbox test runs that timed out | `event : "test_run" and result : "timeout"` |
| 9 | 5xx HTTP responses | `event : "http_request" and status_code >= 500` |
| 10 | What files did the agent write? | `event : "tool_call" and tool : "write_file"` |

Useful Discover columns: `event`, `run_id`, `iteration`, `tool`, `duration_ms`, `status`, `msg`.

## Shell equivalents (Lucene query_string on the same index)

```sh
ES=http://localhost:9200
count() { curl -s --get "$ES/fixit-logs-*/_count" --data-urlencode "q=$1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])'; }
count 'run_id:"a3f9c1d2e8b7"'
count 'level:"error"'
count 'event:"llm_call" AND duration_ms:>3000'
count 'event:"llm_retry"'
count 'event:"task_finished" AND outcome:"failed"'
```

## Reading one document

```sh
curl -s "$ES/fixit-logs-*/_search?q=event:tool_call&size=1&pretty"
```
The `message` field holds the original line exactly as the app wrote it; every other
field (`event`, `run_id`, `duration_ms`, …) was extracted from it by Filebeat's
`decode_json_fields` processor. `@timestamp` was copied from our `ts`.
