# canteen

A university canteen order queue, built small on purpose so that every part of its observability stack can be understood: Prometheus + Grafana + Node Exporter for metrics, Filebeat + Elasticsearch + Kibana for logs. Enterprise Software Development, Fall 2026, Assignment 1. `REPORT.md` is the report (sections A–E, diagram and failure analysis included).

## What it is

Four shops: `sky_dhaba` (chai, pharata), `tapal` (biryani, pulao), `cafetogo` (burger, roll) and `grito` (corn, ice cream). A customer places an order at a shop, the shop marks it ready, the customer picks it up; or the order is cancelled. That is the whole business:

```
POST /orders {shop,item} -> waiting --POST /orders/{id}/ready--> ready --POST /orders/{id}/pickup--> picked_up
                                |                                   |
                                +---------- POST /orders/{id}/cancel ----------> cancelled
```

The app is one FastAPI file with an in-memory dict (`app/main.py`, ~200 lines). It records 9 Prometheus metrics (all four types, business and application), writes one JSON log line per event, and has chaos endpoints to make it slow or partly broken for the experiments.

## Prerequisites

- **Docker Desktop** (macOS/Windows) or Docker Engine + Compose v2 on Linux, with **≥ 6 GB RAM** for Docker (Elasticsearch + Kibana take ~2.5 GB).
- **Python 3.12+** for the scripts (stdlib only). [`uv`](https://docs.astral.sh/uv/) only for the tests: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

## Start

```sh
docker compose up -d --build     # first run pulls ~3 GB of images
docker compose ps                # wait until elasticsearch and kibana are "healthy" (~90 s); setup must be "Exited (0)"
```

| Service | Open | Notes |
|---|---|---|
| canteen | [Swagger UI](http://localhost:8000/docs) · [/health](http://localhost:8000/health) · [/metrics](http://localhost:8000/metrics) · [/shops](http://localhost:8000/shops) · [/chaos](http://localhost:8000/chaos) | every endpoint has "Try it out" |
| load | `docker logs -f canteen-load` | continuous random traffic (6 customers, busy/quiet cycle, rushes, lulls); one status line a minute |
| Prometheus | [Targets](http://localhost:9090/targets) · [Query](http://localhost:9090/query) | `canteen`, `node`, `prometheus` must be UP |
| Grafana | [Application](http://localhost:3000/d/canteen-app) · [Business](http://localhost:3000/d/canteen-business) · [Node Exporter](http://localhost:3000/d/canteen-node) | login `admin` / `admin` |
| Kibana | [Discover](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs')>) | data view **canteen logs** |
| Elasticsearch | [indices](http://localhost:9200/_cat/indices/canteen-logs-*?v) · [count](http://localhost:9200/canteen-logs-*/_count) | |

Node Exporter runs on the host network and is not published on a Mac; see it at Prometheus → Targets, or `docker exec canteen-prometheus wget -qO- http://node-exporter:9100/metrics`.


## Where to look

Ready-made views for the report (Prometheus/Grafana open on the last hour; Kibana on the last hour, change the range as needed):

- **Orders per minute by shop** — [Prometheus graph](<http://localhost:9090/query?g0.expr=sum%20by%20%28shop%29%20%28rate%28canteen_orders_placed_total%5B5m%5D%29%29%20%2A%2060&g0.tab=graph&g0.range_input=1h>) · [Grafana Business](http://localhost:3000/d/canteen-business?from=now-1h&to=now)
- **HTTP p95 (1m window)** — [Prometheus graph](<http://localhost:9090/query?g0.expr=histogram_quantile%280.95%2C%20sum%20by%20%28le%29%20%28rate%28canteen_http_request_duration_seconds_bucket%5B1m%5D%29%29%29&g0.tab=graph&g0.range_input=1h>) · [Grafana Application](http://localhost:3000/d/canteen-app?from=now-1h&to=now)
- **Machine (Node Exporter)** — [Grafana Node Exporter](http://localhost:3000/d/canteen-node?from=now-1h&to=now) · [node target](http://localhost:9090/targets?search=node)
- **Cardinality experiment (E.2)** — [count(canteen_demo_requests_total)](<http://localhost:9090/query?g0.expr=count%28canteen_demo_requests_total%29&g0.tab=graph&g0.range_input=1h&g1.expr=count%28last_over_time%28canteen_demo_requests_total%5B15m%5D%29%29&g1.tab=graph&g1.range_input=1h>)
- **All log lines** — [Kibana Discover](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs',columns:!(event,shop,order_id,request_id,msg))>)
- **Slow requests (E.1)** — [Kibana Discover](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs',columns:!(event,route,status_code,duration_ms,request_id),query:(language:kuery,query:'event%20:%20%22http_request%22%20and%20duration_ms%20%3E%20400'))>)
- **One order end to end** — [Kibana Discover](<http://localhost:5601/app/discover#/?_g=(time:(from:now-1h,to:now))&_a=(index:'canteen-logs',columns:!(event,shop,prep_s,pickup_delay_s,request_id),query:(language:kuery,query:'order_id%20:%20%22PASTE_ID%22'))>) (replace `PASTE_ID`)
- **Raw metrics text** — [/metrics](http://localhost:8000/metrics) · **raw JSON logs** — `docker logs canteen-app | tail -5`

The Kibana data view has the fixed id `canteen-logs` (set by `scripts/setup_elastic.sh`), so these links survive a rebuild.

## Use

```sh
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' -d '{"shop":"sky_dhaba","item":"chai"}'
#  {"order_id":"6942c973","shop":"sky_dhaba","item":"chai","state":"waiting",...}
curl -s -X POST localhost:8000/orders/6942c973/ready
curl -s -X POST localhost:8000/orders/6942c973/pickup
curl -s localhost:8000/shops              # orders waiting per shop
curl -s localhost:8000/metrics | grep canteen_orders
docker logs canteen-app | tail -3          # the JSON lines behind Kibana
```

Traffic is generated all the time by the `load` service, so Grafana → canteen / Business fills on its own within a minute. `docker compose stop load` pauses it, `docker compose start load` resumes. For a fixed, reproducible burst run `python3 scripts/load.py --duration 60 --concurrency 4` (prints client-side latency per route).

Faults (Part E), toggled over HTTP, no restart:

```sh
curl -s -X POST localhost:8000/chaos -H 'Content-Type: application/json' -d '{"slow_every_n":5,"delay_ms":500}'   # every 5th /orders call sleeps 500 ms
curl -s -X POST localhost:8000/chaos -H 'Content-Type: application/json' -d '{"fail_shop":"tapal"}'           # tapal answers 503
curl -s -X POST localhost:8000/chaos/reset
```

## Test

```sh
uv sync
uv run pytest        # 12 tests: lifecycle, metrics, logs, chaos; no Docker needed
```

## Run the experiments

```sh
caffeinate -i scripts/experiment_fault.sh slow        # E.1: baseline / slow-every-5th / recovery, >= 120 s each; pauses the load service for the run (Mac: caffeinate keeps the laptop awake; drop it on Linux)
python3 scripts/stage_counts.py <start> <end> fault   # per-stage numbers from Prometheus + Elasticsearch (windows are printed by the runner)
python3 scripts/experiment_cardinality.py             # E.2: request_id label on a demo counter, capped at 100 series (~2 min, re-creates the app twice)
```

Both leave the app with chaos reset and `CANTEEN_DEMO_CARDINALITY=0`. Raw output lands in `scripts/results/`.

## Dashboards and Kibana

- **canteen / Application** — requests/s by status and by route, HTTP p50/p95/p99 and p95 by route, error rate, mean latency.
- **canteen / Business** — orders waiting, placed and cancelled, busiest shop, cancel rate, orders/min by shop, the waiting gauge by shop, prep-time p95 (histogram) next to prep-time mean (summary), pickup delay (the self-explored metric), cancellations/min.
- **canteen / Node Exporter (machine)** — the machine name, CPU, memory, disk, network, load, disk I/O.

Dashboards are JSON files in `monitoring/grafana/dashboards/`; Grafana re-reads them every 30 s. Kibana queries used in the report are in `scripts/kibana_queries.md`.

## Clean up

```sh
scripts/cleanup.sh            # docker compose down -v: containers + Prometheus/Elasticsearch/Grafana/Filebeat volumes
docker compose down           # containers only; metrics and logs are kept in named volumes for next time
```



