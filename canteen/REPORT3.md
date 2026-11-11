# canteen — Assignment 3 report: Load Balancing and Observability

Enterprise Software Development, Fall 2026 — Assignment 3. Individual submission; extends the Assignment 2 project in this directory (`REPORT2.md`, `REPORT.md`). Branch `assignment3`.

What was added: `app/replica.py` (one process per replica serving HTTP and gRPC, with a shutdown handler), `app/chaos.py` (fault settings shared by both interfaces of a replica), a fix to `app/store.py`'s SQLite connection handling (B.3), a `replica` identifier on every response, log line and metric target, `canteen_http_in_flight`, the gRPC health service, `envoy/render.py` and the four generated Envoy configurations, three replica services and the `envoy` service in `docker-compose.yml`, Prometheus/Filebeat/index-template changes, the dashboard `monitoring/grafana/dashboards/lb.json`, `scripts/fixed_rate.py` (fixed-rate open-loop client), `scripts/envoy_mode.sh`, `scripts/experiment_lb.sh`, `scripts/lb_summary.py`, `scripts/shared_state_demo.sh`, 8 new tests (30 in all). Raw outputs are in `scripts/results/a3_*`. `README.md` has the setup, experiment and recovery commands.

---

## Part A: Replicas and routing

### A.1 Architecture

```
                         HTTP/1.1 :8000              HTTP/2 :50051
   fixed_rate.py  ───────────────────┐    ┌─────────────────────  grpc_client.py / fixed_rate.py grpc
   curl, load.py                     ▼    ▼                       (ONE connection for the whole run)
                            ┌──────────────────────┐
                            │   Envoy  (canteen-envoy)   admin :9901 ──► Prometheus (envoy_cluster_*, envoy_http_*)
                            │   listener http  :10000  ─► cluster canteen_http  (STATIC, 3 endpoints :8000)
                            │   listener grpc  :10001  ─► cluster canteen_grpc  (STATIC, 3 endpoints :50051, HTTP/2)
                            │   active health checks every 2 s; JSON access log + health events ─► stdout ─► Filebeat
                            └──────────┬───────────┬───────────┬──────────┘
                                       │           │           │          weights / policy per mode (envoy/*.yaml)
                             ┌─────────▼──┐  ┌─────▼──────┐  ┌─▼──────────┐
                             │ app-a  "A" │  │ app-b  "B" │  │ app-c  "C" │   one Python process each (app/replica.py):
                             │ 172.28.0.11│  │ 172.28.0.12│  │ 172.28.0.13│   uvicorn app.main:app  :8000  (HTTP API, /metrics, /chaos)
                             │ host :8001 │  │ host :8002 │  │ host :8003 │   grpc_service.server   :50051 (PlaceOrder, grpc.health.v1)
                             └──────┬─────┘  └──────┬─────┘  └──────┬─────┘   in-memory: rate-limit windows, chaos settings, REQUEST_COUNTER
                                    │               │               │
                                    └───────────────┼───────────────┘
                                          ┌─────────▼──────────┐
                                          │ SQLite  canteen.db │  volume app_data, WAL mode; tables orders, idempotency_keys
                                          └────────────────────┘
   Prometheus scrapes app-a:8000, app-b:8000, app-c:8000 (label replica=A|B|C) and envoy:9901 directly — never via :8000/Envoy.
   Filebeat tails canteen-app-a/-b/-c (stdout+stderr) and canteen-envoy (stdout) ─► Elasticsearch canteen-logs-* ─► Kibana.
```

**Replicas.** Three services `app-a`, `app-b`, `app-c` in `docker-compose.yml` share one YAML anchor (same image, same volume, same logging); only `CANTEEN_REPLICA`, the container name, the host port and the fixed IP differ. The brief says "Docker Compose scaling"; I chose three named services over `docker compose up --scale app=3` because Part D needs to address replicas individually: a weight of 2 on A, a delay on B only, `docker compose stop app-b`. Scaled replicas have no stable identity (same environment, generated names, DNS-assigned IPs), so the identifier would have to be reverse-engineered from the container name, and Envoy would have to discover endpoints by DNS, where a stopped container disappears from DNS as well as from health checks and the two effects could not be separated in Part D.3. Fixed IPs plus `type: STATIC` clusters make Envoy's health checks the only thing that decides membership.

**One process per replica.** In A2 the HTTP API and the gRPC service were two containers. A replica in A3 must expose both, so `app/replica.py` starts the gRPC server (its thread pool) and runs uvicorn in the main thread of the same process. Both interfaces therefore share one Prometheus registry (one `/metrics` per replica carries HTTP and gRPC metrics; Prometheus has one target per replica), one stdout (every line carries the same `replica`), and one chaos object (`PUT /chaos {"fixed_delay_ms": 200}` on B slows B's HTTP *and* gRPC, which Part D.2 needs).

**Identifier.** `CANTEEN_REPLICA` is read once (`app/logging_config.py`) and appears (1) in every log line as `replica`, (2) on every HTTP response as `X-Replica` and on every RPC as initial metadata `x-replica`, which Envoy's access log records as `%RESP(X-REPLICA)%`, (3) in metrics as the target label `replica` that Prometheus attaches to every series of that target (`prometheus.yml`), plus `canteen_replica_info{replica="A"} 1` in the exposition itself. The label has three values; no unique id is a label anywhere (A1 Part E.2 still applies).

**Request/trace correlation.** Envoy's HTTP connection managers have `generate_request_id: true, preserve_external_request_id: true`: a client's `X-Request-ID` (header or gRPC metadata) is kept, otherwise Envoy generates a UUID and forwards it. The replica's middleware/interceptor reads the same header, binds it to every log line of the request, and echoes it back. One Kibana search `request_id : "<id>"` therefore returns Envoy's access-log event and the replica's events for the same request (B.4 below).

### A.2 Test traffic

| Interface | Operation (from A2) | Why |
|---|---|---|
| HTTP | `GET /shops` — queue length per shop | read-only, not rate limited, one SQLite read; the brief's "read-only operations are easiest" |
| gRPC | `OrderService/PlaceOrder` (unary) | the only A2 RPC. It is a write (each call creates an order in the shared store), so it also exercises duplicate prevention and the shared-store write path under balancing |

Business rules and data are identical on every replica because all three run the same image and the same `app/store.py` against the same SQLite file (WAL mode, `BEGIN IMMEDIATE` for writes, 5 s busy timeout). Nothing was duplicated per replica except what is in memory, listed next.

### A.3 Shared or separate state

| State | Where | Shared? | What goes wrong when spread across replicas |
|---|---|---|---|
| Orders (the data) | SQLite `orders` table on the `app_data` volume | **shared** by A, B, C | nothing: an order placed via B is read via A or C (`scripts/shared_state_demo.sh` step 1). SQLite's file lock serialises the three writers; at 40 writes/s it is not a bottleneck once the store opens its connections properly (see "what the baseline taught me", B.3) |
| Idempotency records | SQLite `idempotency_keys` table, written in the same transaction as the order | **shared** | duplicate prevention survives balancing: the same `Idempotency-Key` sent six times through round robin landed on B, A, C, B, C, B and returned the **same order** every time, `Idempotent-Replayed: true` from the second try on (demo step 2). Had the records been per replica, each replica would have created its own order for the "same" request |
| Rate-limit counters | `FixedWindowLimiter` in each replica's memory (`app/ratelimit.py`) | **separate** | each replica allows the full limit for the same client. Demo step 3, limit 60/min: against A directly 183 orders gave **60 × 201, 123 × 429**; through Envoy round robin the same client got **179 × 201, 4 × 429**: three times the limit. The client's budget is also unpredictable (depends on which replica each request lands on). Fix, not implemented: keep the window counters in the shared store or Redis, or rate limit in Envoy (local or global rate-limit filter), where the counter sits before the split |
| Chaos settings | in memory, per replica (`app/chaos.py`) | **separate, on purpose** | a fault must be settable on one replica (Part D.2 "add a fixed delay to B"); it is set on the replica's own port (8002), never through Envoy |
| `REQUEST_COUNTER` for the A1 "every n-th request" fault | in memory | separate | the A1 fault now hits every n-th request *of that replica*, i.e. every 3n-th through Envoy; harmless, documented in the README |
| Prometheus metrics | each process's registry | separate by design | correct: Prometheus scrapes each replica and the dashboards `sum by (replica)`; the shared-store gauge `canteen_orders_waiting` is read from SQLite at scrape time, so every replica reports the same true value and the Business dashboard uses `max by (shop)`, not `sum` |

---

## Part B: Observe the baseline

### B.1 Dashboard and queries

`canteen / Load balancing: replicas behind Envoy (A3)` (`monitoring/grafana/dashboards/lb.json`, uid `canteen-lb`). Three rows: **HTTP per replica**, **gRPC per replica**, **Envoy**. The rate window is **1 minute** everywhere and is stated in every panel title; the Prometheus scrape interval is 5 s, so a 1 m window holds 12 samples. Health checks (Envoy every 2 s per replica per cluster, Docker every 10 s) are excluded from the HTTP panels with `route!~"/health|/metrics"`; gRPC health checks are never counted because the interceptor skips `/grpc.health.*`.

| Panel (per interface) | PromQL (HTTP shown; gRPC uses `canteen_grpc_server_handled_total`, `..._handling_seconds_bucket`, `canteen_grpc_server_in_flight`, `grpc_code`) |
|---|---|
| requests/s by replica | `sum by (replica) (rate(canteen_http_requests_total{route!~"/health\|/metrics"}[1m]))` |
| share of traffic % | `100 * sum by (replica) (rate(…[1m])) / scalar(sum(rate(…[1m])))` |
| share over the selected range (bar gauge) | `100 * sum by (replica) (increase(…[$__range])) / scalar(sum(increase(…[$__range])))` — set the time picker to a run's window and read the shares for the results table |
| active requests | `sum by (replica) (canteen_http_in_flight)` — a gauge inc'd when a request arrives, dec'd when it is answered (new in A3; gRPC had `canteen_grpc_server_in_flight` since A2) |
| p50 / p95 (1 m window) | `histogram_quantile(0.5 \| 0.95, sum by (replica, le) (rate(canteen_http_request_duration_seconds_bucket{…}[1m])))` — server side, inside the replica, so a chaos delay is included and Envoy/network are not |
| errors/s by status | `sum by (replica, status) (rate(canteen_http_requests_total{…, status!~"2.."}[1m]))` |

Envoy row (job `envoy`, `GET envoy:9901/stats/prometheus`): `envoy_cluster_membership_healthy` and `_total` per cluster (the health-check view), `sum by (envoy_cluster_name, envoy_response_code) (rate(envoy_cluster_upstream_rq[1m]))`, `envoy_cluster_upstream_rq_active` / `_cx_active`, `rate(envoy_cluster_health_check_failure[1m])`, `envoy_http_downstream_rq_xx{envoy_response_code_class="5"}` (client-visible 5xx), `envoy_http_downstream_rq_total` per listener.

**Prometheus scrapes directly.** `prometheus.yml` has one static target per replica by compose name (`app-a:8000`, …) with `labels: {replica: A}`, and `envoy:9901`. Nothing is scraped through port 8000/Envoy: a scrape through the balanced route would land on a random replica and the label would lie.

### B.2 Workload: the fixed-rate client

`scripts/fixed_rate.py <http|grpc> --rate 40 --duration 60`. It is **open loop**: request *n* is sent at exactly `start + n/rate`, whether or not earlier requests were answered (HTTP: one thread per request from a pool; gRPC: `stub.PlaceOrder.future()` on **one** channel to Envoy, so the client keeps a single HTTP/2 connection as the brief describes). Every request has its own `x-request-id`, a 5 s deadline, no retries, no breaker. It records sent, answered, successes, failures by status/code, client p50/p95/max, and which replica answered (from `X-Replica` / `x-replica`), writing `scripts/results/a3_<run>_<iface>.json`. All runs below use **40 req/s for 60 s = 2 400 requests per interface**, HTTP first, then gRPC, with the `load` service paused. `scripts/experiment_lb.sh` drives every run; `scripts/lb_summary.py` evaluates the per-replica numbers from Prometheus over each run's window (`increase(…[60s])` at the window end) and counts Envoy's access-log events per replica in Elasticsearch for the same window.

### B.3 Baseline (Envoy → A only)

`scripts/envoy_mode.sh a_only`, then 40 req/s × 60 s per interface (window HTTP 07:53:34–07:54:34 Z, gRPC 07:54:39–07:55:40 Z; `scripts/results/a3_b_a_only_*.json`).

| Interface | Sent / answered | Successes | Failures | Client p50 / p95 / max | Share A / B / C (replica metrics) | Envoy access log A / B / C |
|---|---|---|---|---|---|---|
| HTTP `GET /shops` | 2400 / 2400 | 2400 | 0 | 6.2 / **9.1** / 25 ms | **100 % / 0 / 0** (2400 / 0 / 0) | 2400 / 0 / 0 |
| gRPC `PlaceOrder` | 2400 / 2400 | 2399 | 1 (`INTERNAL`, see note) | 5.7 / **8.3** / 113 ms | **100 % / 0 / 0** (2440 / 0 / 0, the extra 40 are the 10 s of probe traffic before the window plus one scrape of skew) | 2400 / 0 / 0 |

Server-side (per replica, from the latency histograms): A's p95 is in the first bucket, i.e. **< 5 ms** for both operations (`histogram_quantile` interpolates 4.8 ms; the A2 buckets start at 5 ms). Active requests on A: mean 0.0 (HTTP) / 0.08 (gRPC) — at 40 req/s with ~3 ms service time the server is idle most of the time. B and C: 0 requests, 0 active, no series moving except the health checks. The dashboard shows one line at 100 % and two at 0 %. The one gRPC failure was an `IntegrityError: UNIQUE constraint failed: orders.order_id` on A: the A2 order id is 8 hex characters (32 bits) and with 60 000 orders in the table a collision every ~10 000 inserts is expected; it happened twice in the 24 000 inserts of this session. `app/store.py` now retries with a fresh id (fixed after the runs, with a test).

**What the baseline taught me (and why it took three attempts).** The first A-only gRPC baseline had a server-side p95 of 2.5 s and 66 deadline errors while HTTP on the same replica was fine. Direct measurements inside the replica showed `place_order` at 9 ms median with stalls up to 2 s, and the WAL file permanently empty: every request opened and closed its own SQLite connection, and closing the *last* connection to a WAL database checkpoints and fsyncs it, so each write paid a disk sync and inherited the Docker VM's fsync stalls (347 ms max measured). Keeping one connection open per process (`store.init`) fixed it (0.9 ms median, 2 ms p95). The second attempt was slow for a different reason: every gRPC test call leaves a `waiting` order that nobody cooks, so `GET /shops`' `COUNT … WHERE state='waiting'` grew from 1 ms to 6 ms as 28 000 test orders piled up and one replica hit 60 % CPU. The runner now cancels the load-test orders before each run (`store.cancel_waiting`) and the index became a covering `(state, shop)` index. Both are real single-replica capacity limits that only a baseline *measurement* revealed; the dashboard's per-replica p95 and active-requests panels are what pointed at them.

### B.4 Envoy access logs in Elasticsearch, and the correlation searches

Envoy writes one JSON line per request to stdout (`envoy/render.py`, `json_format`), with the same field names the replicas use so both kinds of event share Kibana columns: `ts, service=envoy, event=envoy_access, listener, replica (=%RESP(X-REPLICA)%), request_id (=%REQ(X-REQUEST-ID)%), method, path, authority, protocol, status_code, grpc_code (=%GRPC_STATUS(SNAKE_STRING)%), response_flags, duration_ms, upstream_ms, upstream_host, upstream_cluster, bytes_*`. Health-check state changes go to stdout as JSON too (`health_check.event_sinks.file`). Filebeat's autodiscover tails `canteen-envoy` (stdout only; Envoy's own plain-text log goes to stderr and is kept at `warn`) and the three replicas, `decode_json_fields` turns the line into fields, the index template maps `replica`, `listener`, `response_flags`, … as keywords. Everything lands in `canteen-logs-*`, the A1 index.

One search per interface, `request_id : "<id>"` (Kibana query 12 in `scripts/kibana_queries.md`), returns the proxy event **and** the replica's event(s):

**HTTP** — `request_id : "d5ab05b01d85"` (round-robin run, 07:55:52 Z). The proxy event first (it was written at request start), then the replica's; Envoy's `replica: B` / `upstream_host 172.28.0.12:8000` agrees with the replica's own `replica: B`; the proxy saw 4 ms, the replica 2 ms, so Envoy + two network hops cost about 2 ms:

```json
{"ts":"2026-11-09T07:55:52.190Z","service":"envoy","event":"envoy_access","listener":"http","replica":"B","request_id":"d5ab05b01d85","method":"GET","path":"/shops","protocol":"HTTP/1.1","status_code":200,"response_flags":"-","duration_ms":4,"upstream_ms":"4","upstream_host":"172.28.0.12:8000","upstream_cluster":"canteen_http","authority":"[::1]:8000","bytes_received":0,"bytes_sent":56,"msg":"GET /shops -> 200 via B"}
{"ts":"2026-11-09T07:55:52.194245Z","service":"canteen","replica":"B","event":"http_request","request_id":"d5ab05b01d85","method":"GET","path":"/shops","route":"/shops","status_code":200,"duration_ms":2,"level":"info","msg":"GET /shops -> 200"}
```

**gRPC** — `request_id : "e21aef64748c"` (07:56:57 Z): the proxy event (`listener: grpc`, HTTP/2, `grpc_code: OK`) and the replica's two events for the same call, `order_placed` and `grpc_request`:

```json
{"ts":"2026-11-09T07:56:57.359Z","service":"envoy","event":"envoy_access","listener":"grpc","replica":"B","request_id":"e21aef64748c","method":"POST","path":"/canteen.v1.OrderService/PlaceOrder","protocol":"HTTP/2","status_code":200,"grpc_code":"OK","response_flags":"-","duration_ms":6,"upstream_ms":"2","upstream_host":"172.28.0.12:50051","upstream_cluster":"canteen_grpc","authority":"localhost:50051","bytes_received":23,"bytes_sent":51,"msg":"POST /canteen.v1.OrderService/PlaceOrder -> 200 via B"}
{"ts":"2026-11-09T07:56:57.364604Z","service":"canteen","replica":"B","event":"order_placed","request_id":"e21aef64748c","order_id":"35e7f330","shop":"tapal","queue_length":2343,"level":"info","msg":"order placed at tapal"}
{"ts":"2026-11-09T07:56:57.364990Z","service":"canteen","replica":"B","event":"grpc_request","request_id":"e21aef64748c","grpc_method":"PlaceOrder","grpc_code":"OK","duration_ms":3,"level":"info","msg":"PlaceOrder -> OK"}
```

The same search also works for failures: in the failure experiment, `event : "envoy_access" and response_flags : "UF"` lists the requests Envoy could not deliver, with `replica` empty because no replica ever saw them (B.4 query 15 in `scripts/kibana_queries.md`).

---

## Part C: Enable load balancing (equal-weight round robin)

`scripts/envoy_mode.sh round_robin`: both clusters get A, B, C with `load_balancing_weight: 1`, `lb_policy: ROUND_ROBIN`.

**gRPC is balanced per call, not per connection.** The gRPC listener (10001) is an HTTP connection manager with `codec_type: AUTO` and the `canteen_grpc` cluster has `http2_protocol_options`, so Envoy terminates the client's HTTP/2 connection, sees every call as an HTTP/2 stream (`POST /canteen.v1.OrderService/PlaceOrder`), and picks an upstream per stream. The client still has one connection to Envoy; Envoy keeps one HTTP/2 connection per replica upstream (`envoy_cluster_upstream_cx_active{canteen_grpc}` = 3) and multiplexes. Had the listener been a TCP proxy, the one client connection would be pinned to one replica for the whole run and B and C would stay idle. The access log proves the per-call routing: six calls over one `grpc_client.py` channel were logged as `replica` B, C, A, B, C, A.

Same workload (windows HTTP 07:55:51–07:56:52 Z, gRPC 07:56:57–07:57:57 Z):

| Interface | Share A / B / C, replica metrics (Envoy log) | vs 33.3 % | Client p50 / p95 | Server p95 per replica | Active per replica (mean) | Failures |
|---|---|---|---|---|---|---|
| HTTP | 33.4 / 33.0 / 33.6 % (802 / 798 / 800 of 2400) | within ±0.4 pt | 5.6 / **9.1 ms** | < 5 ms each | 0.00 / 0.00 / 0.08 | 0 |
| gRPC | 33.3 / 33.4 / 33.3 % (800 / 800 / 800 of 2400) | exact | 5.4 / **9.1 ms** | < 5 ms each | 0.00 / 0.00 / 0.00 | 0 |

The Envoy access log is the exact count: 800 / 800 / 800 gRPC calls and 802 / 798 / 800 HTTP requests, i.e. Envoy's round robin is a strict rotation, not a random choice. The replica-side counts (811 / 800 / 814) differ from it by the health checks that fall into the window edges and by scrape alignment (`increase()` over 60 s evaluated at the window end); the client's own count by `X-Replica` matches the proxy exactly.

**Difference from the A-only baseline.** None in latency: at 40 req/s one replica was already idle 90 % of the time, so spreading the load over three gives the same ~9 ms client p95 (the floor is Python + Envoy + two hops, not queueing). What changed is the *headroom*: the per-replica request rate dropped from 40 to 13 req/s, and active requests, already near 0, stayed there. Load balancing bought capacity and fault tolerance here, not speed, which is exactly what Part D's slow-replica and failure runs then exercise.

---

## Part D: Experiments and conclusions

All experiments start from Part C's healthy round-robin setup (`scripts/envoy_mode.sh round_robin`, chaos reset on B, every endpoint `healthy`), use the same workload (40 req/s × 60 s per interface, HTTP then gRPC), and end by restoring it. Client retries and circuit breakers are off (`fixed_rate.py` has neither; Envoy has no `retry_policy`, no outlier detection, and circuit-breaker thresholds of 10 000 so they never trigger).

### D.0 Active health checks (settings used in every mode)

| Setting | Value | Meaning |
|---|---|---|
| `interval` / `unhealthy_interval` / `no_traffic_interval` | **2 s** each | a probe every 2 s per endpoint per cluster, also while unhealthy and also when the cluster carries no traffic (the default `no_traffic_interval` is 60 s, which would have made the A-only baseline's B and C probes rare) |
| `timeout` | **1 s** | a probe with no answer within 1 s counts as a failure |
| `unhealthy_threshold` | **2** | two consecutive failures → endpoint removed from the balancing set (`eject_unhealthy_event`) |
| `healthy_threshold` | **2** | two consecutive successes → endpoint back (`add_healthy_event`) |
| HTTP cluster probe | `GET /health`, `codec_client_type: HTTP1` | the replica answers `{"status":"ok","orders":N}`; the chaos `fixed_delay_ms` deliberately does **not** apply to `/health`, so a slow replica still passes (the brief's point) |
| gRPC cluster probe | `grpc_health_check` → `grpc.health.v1.Health/Check` | served by `grpc_health.v1.HealthServicer` registered in `grpc_service/server.py`, `SERVING` for `""` and `canteen.v1.OrderService`; not counted in the application metrics |
| `healthy_panic_threshold` | default 50 % | with one of three replicas down, 67 % are healthy, so Envoy never enters panic mode in these experiments |
| HTTP/2 `connection_keepalive` on the gRPC cluster | PING every **2 s**, **1 s** timeout | not a health check but the same job for *connections*: a stopped container's packets are silently dropped (no RST), so Envoy's pooled HTTP/2 connection to it would otherwise stay "open" and every call multiplexed on it would hang until the client's deadline. Added after the first failure run (D.3) |
| replica shutdown | SIGTERM → gRPC health `NOT_SERVING`, gRPC `stop(grace=1 s)`, uvicorn `timeout_graceful_shutdown=2 s` | so that `docker compose stop app-b` means "B is gone" within ~3 s. Without it uvicorn waited for Envoy's busy keep-alive connections until Docker's 10 s SIGKILL (first failure run, D.3) |

Expected removal time after `docker compose stop app-b`: between 2 s (a probe fails right after the stop, the next one 2 s later) and ~4 s (the stop happens just after a successful probe) plus up to the 1 s timeout per probe if the connection hangs instead of being refused. Expected recovery: process start-up (~1–2 s for Python + uvicorn + gRPC) + two passed probes = ~3–5 s after `docker compose start app-b`.

### D.1 Weighted round robin (A:B:C = 2:1:1)

**Prediction.** Shares 50 / 25 / 25 % on both interfaces (Envoy's weighted round robin is deterministic: with weights 2:1:1 it emits A, B, A, C, … ). A's rate doubles to ~20 req/s, so A's active requests roughly double and its p95 moves slightly up (more contention in one process); B and C halve. A higher weight does not add capacity: total throughput is the same 40 req/s, it is just distributed unevenly, and the client p95 should not improve.

**Observations** (windows HTTP 07:58:09–07:59:09 Z, gRPC 07:59:14–08:00:15 Z):

| Interface | Share A / B / C (Envoy log) | vs 50 / 25 / 25 | Client p50 / p95 | Server p95 A / B / C | Active A / B / C | Failures |
|---|---|---|---|---|---|---|
| HTTP | 50.0 / 25.0 / 25.0 % (1199 / 601 / 600) | exact | 5.6 / **9.6 ms** (C: 9.1) | < 5 / < 5 / < 5 ms | 0.00 / 0.00 / 0.00 | 0 |
| gRPC | 50.0 / 25.0 / 25.0 % (1200 / 600 / 600) | exact | 5.2 / **8.9 ms** (C: 9.1) | < 5 / < 5 / < 5 ms | 0.00 / 0.00 / 0.00 | 0 |

**Explanation.** Envoy's weighted round robin walks a fixed schedule (A, B, A, C, …), so the shares are the weights to the request. A now serves 20 req/s instead of 13, but its service time is ~3 ms, so its utilisation went from 4 % to 6 %: active requests stayed at 0 and its p95 did not move; the client p95 is the same 9 ms as in Part C. The latency change predicted for A did not appear because the system is nowhere near saturation at this rate. The brief's point holds in the data: the weight moved *work*, not *capacity* — 2400 requests still took 60 s; had A been the small replica, the weight would have made things worse, not better. Weights only pay off when they reflect real capacity differences (bigger machine, warmer cache), and nothing in the metrics would tell you that automatically.

### D.2 Least request vs round robin with a slow B (+200 ms on every request and RPC)

**Prediction.** Round robin ignores latency: B keeps its 33 %; with ~13 req/s × 0.2 s B holds ~2.7 requests in flight on average (Little's law) against ~0.1 on A and C, and the client p95 is ≥ 200 ms because a third of all requests take the slow path. Least request (Envoy's default `choice_count: 2`: pick two endpoints at random, send to the one with fewer active requests) will see B's higher in-flight count and send fewer requests there: B's share drops well below 33 % (not to zero, because when the two random picks are both B, or B's count momentarily equals the other's, B is chosen), B's active requests fall, and the client p95 falls below 200 ms if B's share drops under 5 %, otherwise it stays near 200 ms since p95 is a quantile — it only moves when the slow share crosses 5 %. The server-side p50/p95 **per replica** does not change between the two runs (B is slow either way); only the shares, the active-request plateau on B and the client's distribution change. B passes all health checks throughout: a slow replica is still "healthy".

**Observations** — same workload, `PUT localhost:8002/chaos {"fixed_delay_ms": 200}` on B before both runs (B's own p50 is then ~203 ms: 200 ms sleep + 3 ms work), equal weights, only the policy differs:

| Run | Interface | Share A / B / C (Envoy log) | B's active requests mean / max | Server p95 B (A, C) | Client p50 | Client p95 | Client max |
|---|---|---|---|---|---|---|---|
| round robin (08:00:27–08:01:27 Z) | HTTP | 33.3 / **33.3** / 33.3 % (800 / 800 / 800) | **2.92 / 5** | 243 ms (< 5 ms) | 7.8 ms | **209 ms** | 242 ms |
| round robin (08:01:32–08:02:33 Z) | gRPC | 33.4 / **33.4** / 33.2 % (800 / 800 / 800) | **2.67 / 3** | 243 ms (< 5 ms) | 7.3 ms | **209 ms** | 226 ms |
| least request (08:02:45–08:03:45 Z) | HTTP | 40.3 / **14.3** / 45.4 % (975 / 347 / 1078) | **0.92 / 2** | 243 ms (< 5 ms) | 6.4 ms | **208 ms** | 237 ms |
| least request (08:03:50–08:04:51 Z) | gRPC | 42.9 / **15.2** / 41.9 % (1027 / 364 / 1009) | **1.23 / 3** | 243 ms (< 5 ms) | 6.4 ms | **208 ms** | 586 ms |

(Server-side p95 for B reads 243 ms because the A2 histogram has buckets at 100 and 250 ms and the quantile is interpolated; the client-measured p95 of ~208 ms is the accurate figure.)

**Explanation.** Round robin is blind to latency: B kept exactly one third of the calls, and by Little's law 13.3 req/s × 0.2 s ≈ 2.7 requests were in flight on B at any moment (measured 2.9 / 2.7), against 0 on A and C. Least request sees that number: with Envoy's power-of-two-choices it picks two replicas at random and sends to the one with fewer active requests, so B only wins when both picks are B (1/9 of the time) or when its count happens to be equal to the other pick's (just after a batch completes) — measured 14–15 % instead of 33 %, B's active requests fell to 0.9–1.2, and A and C each took ~42 %. The server-side latencies per replica did not change between the two runs (B is slow either way, A and C are not); what changed is how many requests experience B. The client p50 improved from 7.8 to 6.4 ms, but the **client p95 stayed at 208 ms** in both runs: a p95 only moves once fewer than 5 % of requests take the slow path, and least request still sent 14 % of them to B. Least request therefore protected the *median* and halved the slow replica's queue, but with one slow replica out of three it cannot rescue the tail; that needs the slow replica to be removed (outlier detection, or a latency-aware policy), and a slow replica passes every health check — B was `healthy` throughout both runs, exactly the brief's warning. The gRPC least-request run also shows one 586 ms outlier: P2C can pick B twice in a row for a call that then queues behind B's other calls.

### D.3 Failure and recovery (round robin, stop B at 20 s, start B at 40 s)

**Prediction.** From the stop until Envoy ejects B (2–4 s, D.0), round robin still sends every third request to B; B's container is gone so the connection is refused and Envoy answers the client **503** with response flag `UF` (upstream failure) — roughly 13 req/s × 2–4 s ≈ 25–55 failed requests per interface, i.e. 1–2 %. Then A and C each take 50 % until B is back (~3–5 s after `start`), after which the shares return to 33 % each. `envoy_cluster_membership_healthy` drops 3 → 2 → 3; the health-check events in Envoy's log give the exact times to relate to the settings. With no retries, every one of those 503s reaches the client.

**Observations.** Round robin, equal weights, no delay. The runner issued `docker compose stop app-b` 20 s into each run and `docker compose start app-b` 20 s later; the replica's shutdown handler made the container exit within 1 s of the command. Times from the runner log, Envoy's health-check events (in Elasticsearch, `health_checker_type : *`) and the per-5 s timeline of Envoy's access log (`scripts/results/a3_evidence.txt`):

| | HTTP run (08:05:03–08:06:03 Z) | gRPC run (08:06:23–08:07:23 Z) |
|---|---|---|
| `stop app-b` issued → container exited | 08:05:23.0 → 08:05:23.8 (0.8 s) | 08:06:43.0 → 08:06:44.0 (1.0 s) |
| B ejected from `canteen_grpc` (`eject_unhealthy_event`, `NETWORK`) | 08:05:27.5 = **+3.8 s** after exit | 08:06:47.7 = **+3.7 s** |
| B ejected from `canteen_http` (`NETWORK_TIMEOUT`) | 08:05:28.5 = **+4.7 s** | 08:06:48.6 = **+4.6 s** |
| client errors while B was still a destination | **64 × 503** (`UF`), 2.7 % of 2400 | **53 × UNAVAILABLE** (HTTP 200 + grpc-status 14, `UF`), 2.2 % |
| slowest client call | 1011 ms | 1005 ms |
| `start app-b` issued → container started | 08:05:44.0 → 08:05:44.3 | 08:07:04.0 → 08:07:04.5 |
| B healthy again in `canteen_grpc` / `canteen_http` (`add_healthy_event`) | 08:05:47.0 / 08:05:47.5 = **+2.7 / +3.2 s** after start | 08:07:07.0 / 08:07:07.6 = **+2.6 / +3.2 s** |
| shares over the whole 60 s (Envoy log) | 40 / 20 / 40 % | 40 / 21 / 40 % |

Per-5 s timeline of the HTTP run (requests per replica from Envoy's access log; the gRPC run is the same shape):

```
t(s)   A    B    C   503(UF)        t = 20 s: stop B
 12   66   67   67      0
 17   67   49   67     17           B gone at 20.8 s; Envoy still sends every 3rd request to B -> UF
 22   78    0   75     47           ejected at 25.5 s (http cluster); A and C now take 50 % each
 27   99    0  101      0
 32  100    0  100      0
 37  100    0  100      0
 42   84   34   82      0           started at 44.3 s, healthy at 47.5 s: B back at 33 %
 47   68   65   67      0
```

**Explanation.** Removal took 3.7–4.7 s after the process died, which is what the settings predict: probes every 2 s, `unhealthy_threshold: 2`, so the first failed probe lands 0–2 s after the exit and the second 2 s later, plus up to the 1 s `timeout` on the HTTP probe (`NETWORK_TIMEOUT`: the stopped container's address drops packets, so the TCP connect waits the full second; the gRPC probe reported `NETWORK` a second earlier because its connection was reset first). During those ~4.5 s round robin kept sending every third request to B; Envoy's connect attempt to the vanished address hit `connect_timeout: 1s` and answered **503 UF** (gRPC: `UNAVAILABLE`) — 64 and 53 requests, i.e. 13.3 req/s × ~4.5 s, and the slowest client calls were exactly 1.0 s, the connect timeout. With no retries every one of them reached the client; a single Envoy retry on `connect-failure` would have hidden all of them at the cost of one extra second of latency. Recovery took 2.6–3.2 s after `start`: ~1 s for Python to start and open its ports, then two passed probes 2 s apart (`healthy_threshold: 2`). The shares over the whole minute (40 / 20 / 40) are the arithmetic of B being absent for 24 of 60 s. `envoy_cluster_membership_healthy` on the dashboard goes 3 → 2 → 3 at the same moments.

**The first failure run (before two fixes; kept as `scripts/results/a3_v1_d_failure_*.json`).** HTTP behaved as above (46 × 503), but gRPC lost **584 of 2400 calls (24 %)** with a client p95 of 5 s: for ~10 s after the stop *no* gRPC call completed on *any* replica. Two things combined. (1) The replica did not exit on SIGTERM under traffic: uvicorn waited for Envoy's busy keep-alive connections and Docker killed it after 10 s, during which B's gRPC port stayed open but stopped progressing. (2) Envoy's pooled HTTP/2 connection to B had no keepalive, so the calls multiplexed on it just hung, and because the *client* also multiplexes every call for every replica on one HTTP/2 connection to Envoy, the stuck streams blocked that connection for A's and C's calls too (head-of-line blocking at the HTTP/2 level), until the SIGKILL reset everything at once. The fixes were a shutdown handler in `app/replica.py` and `connection_keepalive` (2 s / 1 s) on the gRPC cluster (D.0). This is the most instructive result of the assignment: a half-dead replica is far worse than a dead one, and the single multiplexed connection that makes gRPC efficient is also what spreads one replica's stall to the others.

### D.4 Summary of all runs

All runs: 40 req/s target, 60 s, 2400 requests per interface; "actual" is sent ÷ duration as measured by the client (the scheduler kept within 0.1 % everywhere). Shares are from Envoy's access log; the replica-metrics shares agree within 1 pt in every run (`scripts/results/a3_summary.txt`).

| Run / interface | Policy and fault | Target / actual req/s | A / B / C traffic shares | Client p95 | Error rate | Explanation |
|---|---|---|---|---|---|---|
| B baseline / HTTP | A only | 40 / 40.0 | 100 / 0 / 0 % | 9.1 ms | 0 % | one idle-enough replica; B, C running, unused |
| B baseline / gRPC | A only | 40 / 40.0 | 100 / 0 / 0 % | 8.3 ms | 0.04 % (1 id collision) | same; single HTTP/2 connection, all calls to A |
| C / HTTP | round robin 1:1:1 | 40 / 40.0 | 33 / 33 / 33 % | 9.1 ms | 0 % | strict rotation; latency floor unchanged, per-replica load ÷ 3 |
| C / gRPC | round robin 1:1:1 | 40 / 40.0 | 33 / 33 / 33 % | 9.1 ms | 0 % | HTTP/2 listener: balanced per call, 800/800/800 |
| D.1 / HTTP | weighted 2:1:1 | 40 / 40.0 | 50 / 25 / 25 % | 9.6 ms | 0 % | shares = weights; A at 6 % utilisation, no latency change |
| D.1 / gRPC | weighted 2:1:1 | 40 / 40.0 | 50 / 25 / 25 % | 8.9 ms | 0 % | same |
| D.2a / HTTP | round robin, B +200 ms | 40 / 40.0 | 33 / 33 / 33 % | 209 ms | 0 % | blind to latency; B holds 2.9 active requests |
| D.2a / gRPC | round robin, B +200 ms | 40 / 40.0 | 33 / 33 / 33 % | 209 ms | 0.04 % (1 id collision) | same; B 2.7 active |
| D.2b / HTTP | least request, B +200 ms | 40 / 40.0 | 40 / 14 / 45 % | 208 ms | 0 % | B's share ÷ 2.3, active 0.9; p50 better, p95 unchanged (14 % > 5 %) |
| D.2b / gRPC | least request, B +200 ms | 40 / 40.0 | 43 / 15 / 42 % | 208 ms | 0 % | same; B 1.2 active |
| D.3 / HTTP | round robin, stop B 20 s → start 40 s | 40 / 40.0 | 40 / 20 / 40 % | 12.4 ms | 2.7 % (64 × 503 UF) | removed 4.7 s after exit, back 3.2 s after start; errors = 13 req/s × 4.7 s |
| D.3 / gRPC | round robin, stop B 20 s → start 40 s | 40 / 40.0 | 40 / 21 / 40 % | 10.1 ms | 2.2 % (53 × UNAVAILABLE) | removed 3.7 s after exit, back 2.6 s after start |
| D.3 v1 / gRPC | same, before shutdown-handler + keepalive fixes | 40 / 40.0 | 42 / 16 / 42 % | 5037 ms | 24.3 % | half-dead B for 10 s stalled the shared HTTP/2 connection for every replica |

**Cross-check against Envoy's access logs.** Part C, gRPC, 07:56:57–07:57:57 Z: the client counted 800 / 800 / 800 by `x-replica`; Elasticsearch `event:"envoy_access" and listener:"grpc"` grouped by `replica` for the same minute gives 800 / 800 / 800; Prometheus `increase(canteen_grpc_server_handled_total[60s])` by replica gives 800 / 801 / 800. Three independent sources, three agreeing counts.

### D.5 Conclusions

1. **Which strategy, and why.** For **HTTP** I would run **least request** (Envoy's default P2C, `choice_count: 2`). With healthy replicas it is indistinguishable from round robin (both gave 9.1 ms p95 and a 33/33/33 split here), and the moment one replica slows down it halved that replica's queue and share (33 → 14 %, 2.9 → 0.9 active) at zero configuration cost; HTTP/1.1 requests are independent connections, so the per-request decision is cheap and the active-request signal is clean. For **gRPC** I would also use least request, but the measurements say the policy matters less than two other things: the listener must be HTTP/2 so Envoy balances per call at all (Part C), and the upstream connections need keepalive plus a replica that dies cleanly, because the v1 failure run showed a half-dead replica costing 24 % of all calls across *every* replica while the policy was irrelevant. Weighted round robin I would use only to encode a known capacity difference; here it moved 50 % of the work to A for no gain. Neither policy fixes a slow replica's p95 (both 208 ms): that needs outlier ejection or removing the replica, since health checks stayed green.

2. **A workload where I would choose differently.** Requests with wildly different cost on the same route — e.g. an order-history export where one customer's request takes 2 s and most take 5 ms — would make me prefer least request even more strongly for HTTP, but for a *stateful* or cache-heavy workload, say per-shop queue views where each replica caches its shop's data, I would choose **consistent hashing (ring hash / Maglev) on the shop id** instead: it sacrifices evenness (shares follow the key distribution) for cache locality and would also make per-replica rate-limit counters correct again for a given key, which neither round robin nor least request can. Conversely, for very bursty short-lived connections (clients that connect, send one request and leave) plain round robin is the safer default, because least request's active-request counts are then almost always zero and P2C degenerates to random choice anyway.

3. **If Envoy stops.** Everything stops: clients address only Envoy (8000 / 50051), so an Envoy crash is a total outage even though all three replicas are healthy — the proxy is a single point of failure, and its in-flight requests are lost too (we saw a 2 s blip every time `envoy_mode.sh` re-created it between runs). Prometheus would still scrape the replicas directly and show their request rates dropping to the health checks only, and Envoy's own target would go down; Filebeat would ship nothing from the proxy. The improvement I would make (not implemented): run **two Envoy instances** behind a shared virtual IP / DNS name (keepalived on a VM pair, or simply two replicas of the `envoy` service behind the Docker-published port with the client given both addresses and a client-side failover), with the Envoy configuration delivered from a control plane (xDS) instead of a file so that changing weights or policy does not require a restart at all. A cheaper first step is simply to make the client-side gRPC channel aware of two Envoy addresses (`dns:///envoy-a,envoy-b` with round-robin), which gRPC supports natively.

### D.6 Charts

Every number above can be re-read from the dashboard `canteen / Load balancing: replicas behind Envoy (A3)` by setting the time picker (UTC) to a run's window from `scripts/results/a3_runs.tsv`; the "share over the selected time range" bar gauges then show the row of the results table directly. Screenshots to accompany this report (`docs/screenshots/a3_*.png`):

| File | Dashboard range (UTC, 2026-11-09) | What it shows |
|---|---|---|
| `a3_b_baseline.png` | 07:53:30 – 07:55:45 | HTTP and gRPC rows: A at 40 req/s, B and C flat at 0; share 100 / 0 / 0 |
| `a3_c_round_robin.png` | 07:55:45 – 07:58:00 | three lines at 13.3 req/s, shares 33 / 33 / 33, p95 < 5 ms everywhere |
| `a3_d1_weighted.png` | 07:58:05 – 08:00:20 | A at 20 req/s, B and C at 10; shares 50 / 25 / 25 |
| `a3_d2_slow_b.png` | 08:00:20 – 08:04:55 | both slow-B runs side by side: B's active requests ~2.8 (round robin) then ~1 (least request), B's share 33 % → 14 %, B's p95 ~240 ms in both |
| `a3_d3_failure.png` | 08:05:00 – 08:07:30 | Envoy row: `membership_healthy` 3 → 2 → 3 twice, upstream 503s / UF at the two stop moments; HTTP and gRPC rows: B's line dropping to 0 and A, C rising to 20 req/s |

---

## Credits

- **Course material.** Enterprise Software Development (Habib University), Fall 2026, Assignment 3 brief; the A2 brief and my A2 submission; Envoy documentation on load-balancing policies (round robin, weighted, least request, power of two choices), active health checking, HTTP connection manager, access-log format strings, and the `grpc.health.v1` protocol.
- **Reused work.** Everything from my Assignment 1 and 2 submissions (`REPORT.md`, `REPORT2.md`): the app, store, metrics, logging, compose stack, Prometheus/Filebeat/Elasticsearch configuration, the load generator and the A1/A2 scripts. This assignment changes `app/main.py`, `app/metrics.py`, `app/logging_config.py`, `app/Dockerfile`, `grpc_service/server.py`, `docker-compose.yml`, `prometheus.yml`, `filebeat.yml`, `index_template.json`, `README.md`, `scripts/kibana_queries.md`, and adds the files listed at the top.
- **Libraries and images.** FastAPI, Pydantic, uvicorn, prometheus_client, structlog, grpcio, grpcio-health-checking, protobuf, PyYAML (config generation), pytest; `envoyproxy/envoy:v1.33.0`, Prometheus 3.5, Grafana 12.1, Elastic Stack 8.18, pinned in `docker-compose.yml`.
- **AI assistance.** Claude (Anthropic, via Claude Code) drafted the code, configuration, dashboard and this report from my design, ran the experiments quoted here and collected the outputs in `scripts/results/`. I have read and can explain every file; the design decisions (one process per replica, named replicas with fixed IPs and static clusters, which state to share, the health-check settings) and the conclusions are mine.
