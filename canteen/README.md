# canteen

A university canteen order queue, built small on purpose so that every part of its observability stack can be understood: Prometheus + Grafana + Node Exporter for metrics, Filebeat + Elasticsearch + Kibana for logs, and (Assignment 3) three replicas of the application behind an Envoy load balancer. Enterprise Software Development, Fall 2026. `REPORT.md` is the Assignment 1 report (observability); `REPORT2.md` the Assignment 2 report (HTTP API practices, gRPC service, metrics); `REPORT3.md` the Assignment 3 report (load balancing and observability).

## What it is

Four shops: `sky_dhaba` (chai, pharata), `tapal` (biryani, pulao), `cafetogo` (burger, roll) and `grito` (corn, ice cream). A customer places an order at a shop, the shop marks it ready, the customer picks it up; or the order is cancelled. That is the whole business:

```
POST /orders {shop,item} -> waiting --POST /orders/{id}/ready--> ready --POST /orders/{id}/pickup--> picked_up
                                |                                   |
                                +---------- POST /orders/{id}/cancel ----------> cancelled
```

The HTTP API is `app/main.py`; the business rules and the data are `app/store.py`, a SQLite file on the `app_data` volume that the gRPC service (`grpc_service/server.py`, Assignment 2) shares, so `PlaceOrder` over gRPC and `POST /orders` over HTTP create the same orders. The app records Prometheus metrics of all four types (business and application), writes one JSON log line per event, and has chaos endpoints to make it slow or partly broken for the experiments.

Since Assignment 3 the application runs as **three identical replicas A, B and C** (`app/replica.py`: one process per replica serving HTTP on 8000 and gRPC on 50051) behind **Envoy**:

```
HTTP / gRPC clients ──► envoy  :8000 (HTTP/1.1)  :50051 (HTTP/2, gRPC)  :9901 admin
                           │  active health checks every 2 s; round robin / weighted / least request (envoy/*.yaml)
                           ├──► app-a  172.28.0.11   :8000 HTTP + :50051 gRPC   (also localhost:8001)
                           ├──► app-b  172.28.0.12                               (also localhost:8002)
                           └──► app-c  172.28.0.13                               (also localhost:8003)
                                   └───── one SQLite file (orders, idempotency keys) on the app_data volume ─────┘
Prometheus scrapes app-a, app-b, app-c (label replica=A|B|C) and envoy:9901 directly; Filebeat ships the replicas' and Envoy's JSON logs.
```

## Prerequisites

- **Docker Desktop** (macOS/Windows) or Docker Engine + Compose v2 on Linux, with **≥ 6 GB RAM** for Docker (Elasticsearch + Kibana take ~2.5 GB).
- **Python 3.12+** for the scripts (stdlib only). [`uv`](https://docs.astral.sh/uv/) only for the tests: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

## Start

```sh
docker compose up -d --build     # first run pulls ~3 GB of images; ENVOY_MODE=round_robin by default
docker compose ps                # wait until elasticsearch and kibana are "healthy" (~90 s); setup must be "Exited (0)"; app-a/b/c "healthy"
scripts/envoy_mode.sh            # every endpoint of canteen_http and canteen_grpc should be "healthy"
```

| Service | Open | Notes |
|---|---|---|
| canteen (via Envoy) | [Swagger UI](http://localhost:8000/docs) · [openapi.json](http://localhost:8000/openapi.json) · [/health](http://localhost:8000/health) · [/shops](http://localhost:8000/shops) | port 8000 is Envoy's HTTP listener; the `X-Replica` response header says which replica answered |
| gRPC (via Envoy) | `localhost:50051` | Envoy's HTTP/2 listener; `uv run python scripts/grpc_client.py --shop tapal --item biryani` |
| replicas directly | A [:8001](http://localhost:8001/metrics) · B [:8002](http://localhost:8002/metrics) · C [:8003](http://localhost:8003/metrics) (`/metrics`, `/chaos`, `/health`, …) | for setting a fault on one replica and for inspection; Prometheus uses the compose names `app-a:8000` … |
| Envoy admin | [/clusters](http://localhost:9901/clusters) · [/stats/prometheus](http://localhost:9901/stats/prometheus) · [/ready](http://localhost:9901/ready) | per-endpoint health flags and weights; `scripts/envoy_mode.sh` prints them |
| load | `docker logs -f canteen-load` | continuous random traffic through Envoy (6 customers, busy/quiet cycle, rushes, lulls); one status line a minute |
| Prometheus | [Targets](http://localhost:9090/targets) · [Query](http://localhost:9090/query) | `canteen` (A, B, C), `envoy`, `node`, `prometheus` must be UP |
| Grafana | [Load balancing (A3)](http://localhost:3000/d/canteen-lb) · [Application](http://localhost:3000/d/canteen-app) · [Business](http://localhost:3000/d/canteen-business) · [Node Exporter](http://localhost:3000/d/canteen-node) · [HTTP vs gRPC](http://localhost:3000/d/canteen-grpc) | login `admin` / `admin`; `GRAFANA_PORT=3001 docker compose up -d grafana` if 3000 is taken |
| Kibana | [Discover](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs')>) | data view **canteen logs** |
| Elasticsearch | [indices](http://localhost:9200/_cat/indices/canteen-logs-*?v) · [count](http://localhost:9200/canteen-logs-*/_count) | |

Node Exporter runs on the host network and is not published on a Mac; see it at Prometheus → Targets, or `docker exec canteen-prometheus wget -qO- http://node-exporter:9100/metrics`.


## Where to look

Ready-made views for the report (Prometheus/Grafana open on the last hour; Kibana on the last hour, change the range as needed):

- **HTTP vs gRPC place-order dashboard** — [Grafana](http://localhost:3000/d/canteen-grpc?from=now-1h&to=now) · [gRPC calls by code](<http://localhost:9090/query?g0.expr=sum%20by%20%28grpc_code%29%20%28rate%28canteen_grpc_server_handled_total%5B5m%5D%29%29&g0.tab=graph&g0.range_input=1h>)
- **Orders per minute by shop** — [Prometheus graph](<http://localhost:9090/query?g0.expr=sum%20by%20%28shop%29%20%28rate%28canteen_orders_placed_total%5B5m%5D%29%29%20%2A%2060&g0.tab=graph&g0.range_input=1h>) · [Grafana Business](http://localhost:3000/d/canteen-business?from=now-1h&to=now)
- **HTTP p95 (1m window)** — [Prometheus graph](<http://localhost:9090/query?g0.expr=histogram_quantile%280.95%2C%20sum%20by%20%28le%29%20%28rate%28canteen_http_request_duration_seconds_bucket%5B1m%5D%29%29%29&g0.tab=graph&g0.range_input=1h>) · [Grafana Application](http://localhost:3000/d/canteen-app?from=now-1h&to=now)
- **Machine (Node Exporter)** — [Grafana Node Exporter](http://localhost:3000/d/canteen-node?from=now-1h&to=now) · [node target](http://localhost:9090/targets?search=node)
- **Cardinality experiment (E.2)** — [count(canteen_demo_requests_total)](<http://localhost:9090/query?g0.expr=count%28canteen_demo_requests_total%29&g0.tab=graph&g0.range_input=1h&g1.expr=count%28last_over_time%28canteen_demo_requests_total%5B15m%5D%29%29&g1.tab=graph&g1.range_input=1h>)
- **Load balancing (A3)** — [Grafana](http://localhost:3000/d/canteen-lb?from=now-15m&to=now) · [share per replica](<http://localhost:9090/query?g0.expr=100%20*%20sum%20by%20%28replica%29%20%28rate%28canteen_http_requests_total%7Broute%21~%22%2Fhealth%7C%2Fmetrics%22%7D%5B1m%5D%29%29%20%2F%20scalar%28sum%28rate%28canteen_http_requests_total%7Broute%21~%22%2Fhealth%7C%2Fmetrics%22%7D%5B1m%5D%29%29%29&g0.tab=graph&g0.range_input=1h>) · [Envoy healthy members](<http://localhost:9090/query?g0.expr=envoy_cluster_membership_healthy&g0.tab=graph&g0.range_input=1h>) · [Envoy + replica events of one request in Kibana](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs',columns:!(service,replica,event,method,path,status_code,grpc_code,duration_ms,upstream_ms,response_flags),query:(language:kuery,query:'request_id%20:%20%22PASTE_ID%22'))>)
- **All log lines** — [Kibana Discover](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs',columns:!(event,shop,order_id,request_id,msg))>)
- **Slow requests (E.1)** — [Kibana Discover](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs',columns:!(event,route,status_code,duration_ms,request_id),query:(language:kuery,query:'event%20:%20%22http_request%22%20and%20duration_ms%20%3E%20400'))>)
- **One order end to end** — [Kibana Discover](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs',columns:!(event,shop,prep_s,pickup_delay_s,request_id),query:(language:kuery,query:'order_id%20:%20%22PASTE_ID%22'))>) (replace `PASTE_ID`)
- **Raw metrics text** — [/metrics of replica A](http://localhost:8001/metrics) · **raw JSON logs** — `docker logs canteen-app-a | tail -5` · `docker logs canteen-envoy | tail -5`

The Kibana data view has the fixed id `canteen-logs` (set by `scripts/setup_elastic.sh`), so these links survive a rebuild.

## Use

```sh
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' -d '{"shop":"sky_dhaba","item":"chai"}'
#  {"order_id":"6942c973","shop":"sky_dhaba","item":"chai","state":"waiting",...}
curl -s -X POST localhost:8000/orders/6942c973/ready
curl -s -X POST localhost:8000/orders/6942c973/pickup
curl -s localhost:8000/shops              # orders waiting per shop
curl -s localhost:8001/metrics | grep canteen_orders      # one replica's metrics (Envoy does not proxy /metrics scrapes; Prometheus goes direct)
docker logs canteen-app-a | tail -3        # the JSON lines behind Kibana (replica A)
```

Traffic is generated all the time by the `load` service, so Grafana → canteen / Business fills on its own within a minute. `docker compose stop load` pauses it, `docker compose start load` resumes. For a fixed, reproducible burst run `python3 scripts/load.py --duration 60 --concurrency 4` (prints client-side latency per route).

Faults (A1 Part E), toggled over HTTP, no restart. Since A3 the settings are per replica: address one replica directly (8001/8002/8003), not Envoy on 8000:

```sh
curl -s -X PUT localhost:8001/chaos -H 'Content-Type: application/json' -d '{"slow_every_n":5,"delay_ms":500}'   # every 5th /orders call sleeps 500 ms
curl -s -X PUT localhost:8001/chaos -H 'Content-Type: application/json' -d '{"fail_shop":"tapal"}'           # tapal answers 503 (on A)
curl -s -X DELETE localhost:8001/chaos                                                                        # back to defaults (204)
```

### Assignment 2: API practices, gRPC, clients

Every error has one shape, `{"error": {"code", "message", "details"}}`; invalid input is 422, an unknown order 404, an impossible transition 409, a closed shop 503. `POST /orders` accepts two optional headers:

```sh
# Idempotency-Key: repeating the request returns the first order (header Idempotent-Replayed: true), never a second one
curl -si -X POST localhost:8000/orders -H 'Content-Type: application/json' -H 'Idempotency-Key: my-order-1' -d '{"shop":"tapal","item":"biryani"}'
# X-Client-ID: the rate-limit key (60 orders per 60 s per client; without it the client IP is the key) -> 429 + Retry-After when exceeded
scripts/ratelimit_demo.sh                       # 65 quick orders from one client: 60 x 201, then 429 with Retry-After
python3 scripts/caller.py                       # a service calling POST /orders: 2 s timeout, 3 retries with backoff, circuit breaker
python3 scripts/caller.py --demo                # success -> retries on 503 -> breaker open -> recovery (toggles /chaos on tapal for ~10 s)
uv run python scripts/grpc_client.py --shop tapal --item biryani            # the gRPC PlaceOrder, 2 s deadline, x-request-id metadata
uv run python scripts/grpc_client.py --shop pizza --item x                  # -> INVALID_ARGUMENT
uv run python scripts/grpc_client.py --shop tapal --item pulao --count 20   # repeatable calls for the dashboard
```

The OpenAPI document is `openapi.json` (regenerate with `uv run python scripts/export_openapi.py` after changing a route; a test checks it matches the app). Since A3 the ports are: Envoy 8000/50051 for clients, replicas 8001-8003 directly. The gRPC contract is `grpc_service/canteen.proto`; regenerate the Python code with

```sh
uv run python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. --pyi_out=. grpc_service/canteen.proto
```

### Assignment 3: replicas behind Envoy

Clients talk to Envoy only (8000 / 50051). `envoy/render.py` generates one config per balancing mode; `ENVOY_MODE` in `docker-compose.yml` picks it (default `round_robin`) and `scripts/envoy_mode.sh` switches at run time by re-creating the Envoy container and waiting until every destination passes its health checks.

| Mode | Destinations | Policy | Used in |
|---|---|---|---|
| `a_only` | A | round robin | Part A/B baseline: B and C run but get no traffic |
| `round_robin` | A, B, C, weights 1:1:1 | `ROUND_ROBIN` | Part C and the start of every Part D experiment |
| `weighted` | A, B, C, weights 2:1:1 | `ROUND_ROBIN` | Part D: weighted round robin |
| `least_request` | A, B, C, weights 1:1:1 | `LEAST_REQUEST` (2 random choices, fewer active requests wins) | Part D: slow replica |

Active health checks on both clusters: every **2 s**, **1 s** timeout, **2** failures remove a replica, **2** successes bring it back (HTTP `GET /health`; gRPC `grpc.health.v1.Health/Check`). No retries, no outlier detection; circuit-breaker thresholds raised to 10 000. Envoy writes one JSON access-log line per request (replica, request id, status/gRPC code, durations) and one event per health-check state change to stdout; Filebeat ships both to the same index as the replicas' logs.

```sh
scripts/envoy_mode.sh                      # current mode + health flag and weight of every endpoint
scripts/envoy_mode.sh weighted             # switch (a_only | round_robin | weighted | least_request)
curl -si localhost:8000/shops | grep -i x-replica                                   # which replica answered
uv run python scripts/grpc_client.py --shop tapal --item pulao --count 6            # one connection, calls spread per call
scripts/shared_state_demo.sh               # Part A: data and idempotency keys are shared (SQLite); rate-limit counters are not (~3x the limit through Envoy)

# the fixed-rate, open-loop test client (one operation per interface: GET /shops, PlaceOrder)
python3 scripts/fixed_rate.py http --rate 40 --duration 60 --label c_rr             # -> scripts/results/a3_c_rr_http.json
uv run python scripts/fixed_rate.py grpc --rate 40 --duration 60 --label c_rr       # -> scripts/results/a3_c_rr_grpc.json

# faults, always on ONE replica directly (never through Envoy)
curl -s -X PUT localhost:8002/chaos -H 'Content-Type: application/json' -d '{"fixed_delay_ms":200}'   # B answers every HTTP request and RPC 200 ms late
curl -s -X DELETE localhost:8002/chaos                                                                # B back to normal
docker compose stop app-b                  # failure: Envoy removes B after 2 failed checks (~2-4 s), clients get 503 UF/UH meanwhile
docker compose start app-b                 # recovery: back after 2 passed checks once the process is up
```

Run all experiments (Parts B, C, D; ~20 min at the default 40 req/s for 60 s per interface) and build the results table:

```sh
caffeinate -i scripts/experiment_lb.sh                      # or a subset: scripts/experiment_lb.sh c_rr d_lr_slow
python3 scripts/lb_summary.py                               # per replica from Prometheus + Envoy access-log cross-check from Elasticsearch
```

The runner pauses the `load` service, re-configures Envoy before each run, sets/clears the fault on B, cancels the waiting load-test orders left by earlier gRPC runs (`store.cancel_waiting`, so `GET /shops` costs the same in every run), runs HTTP then gRPC, and ends in the healthy round-robin setup with chaos reset. Windows are appended to `scripts/results/a3_runs.tsv`; paste one into the [Load balancing dashboard](http://localhost:3000/d/canteen-lb) time picker to see that run. Recovery by hand, if a run is interrupted: `scripts/envoy_mode.sh round_robin; curl -X DELETE localhost:8002/chaos; docker compose start app-b load`.

## Test

```sh
uv sync
uv run pytest        # 30 tests, no Docker needed: tests/test_app.py (lifecycle, metrics, logs, chaos), tests/test_api_practices.py
                     # (error format, idempotency, rate limit, OpenAPI), tests/test_grpc.py (gRPC server in-process, shared store),
                     # tests/test_replica.py (A3: replica id in responses/logs, in-flight gauge, fixed delay on both interfaces, gRPC health)
```

## Run the experiments

```sh
caffeinate -i scripts/experiment_fault.sh slow        # E.1: baseline / slow-every-5th / recovery, >= 120 s each; pauses the load service for the run (Mac: caffeinate keeps the laptop awake; drop it on Linux)
python3 scripts/stage_counts.py <start> <end> fault   # per-stage numbers from Prometheus + Elasticsearch (windows are printed by the runner)
python3 scripts/experiment_cardinality.py             # E.2: request_id label on a demo counter, capped at 100 series (~2 min, re-creates the app twice)
```

Both leave the app with chaos reset and `CANTEEN_DEMO_CARDINALITY=0`. Raw output lands in `scripts/results/`. (Since A3 these A1 scripts address Envoy on 8000, so the fault toggled by `experiment_fault.sh` lands on whichever replica round robin picks; point `CANTEEN_URL` at one replica, e.g. `http://localhost:8001`, to reproduce A1 exactly.)

## Dashboards and Kibana

- **canteen / Application** — requests/s by status and by route, HTTP p50/p95/p99 and p95 by route, error rate, mean latency.
- **canteen / Business** — orders waiting, placed and cancelled, busiest shop, cancel rate, orders/min by shop, the waiting gauge by shop, prep-time p95 (histogram) next to prep-time mean (summary), pickup delay (the self-explored metric), cancellations/min.
- **canteen / Node Exporter (machine)** — the machine name, CPU, memory, disk, network, load, disk I/O.
- **canteen / HTTP vs gRPC: place order** (Assignment 2) — calls/s by HTTP status and by gRPC code, error share, p50/p95 of both versions over 5 minutes, gRPC in-flight calls, idempotent replays and 429s.
- **canteen / Load balancing: replicas behind Envoy** (Assignment 3) — per replica, for HTTP and for gRPC: requests/s and share of traffic (1 m window, plus the share over the selected time range), active requests, p50/p95 (1 m window), errors by status; Envoy's healthy members per cluster, upstream requests by response code, active requests/connections, health-check failures and downstream 5xx.

Dashboards are JSON files in `monitoring/grafana/dashboards/`; Grafana re-reads them every 30 s. Kibana queries used in the reports are in `scripts/kibana_queries.md` (A3: `request_id : "<id>"` returns Envoy's access-log event and the replica's event for the same request).

## Clean up

```sh
scripts/cleanup.sh            # docker compose down -v: containers + Prometheus/Elasticsearch/Grafana/Filebeat volumes
docker compose down           # containers only; metrics and logs are kept in named volumes for next time
```



