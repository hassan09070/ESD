# canteen

A university canteen order queue, built small on purpose so that every part of its observability stack can be understood: Prometheus + Grafana + Node Exporter for metrics, Filebeat + Elasticsearch + Kibana for logs. Enterprise Software Development, Fall 2026, Assignment 1. `REPORT.md` is the report; `docs/architecture.md` has the diagram and failure analysis.

## What it is

Four stalls (`chai`, `biryani`, `shawarma`, `juice`). A customer places an order at a stall, the stall marks it ready, the customer picks it up; or the order is cancelled. That is the whole business:

```
POST /orders {stall,item} -> waiting --POST /orders/{id}/ready--> ready --POST /orders/{id}/pickup--> picked_up
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

| Service | URL | Notes |
|---|---|---|
| canteen | http://localhost:8000/docs | Swagger UI: every endpoint has "Try it out"; metrics at `/metrics` |
| Prometheus | http://localhost:9090 | Status → Targets: `canteen`, `node`, `prometheus` must be UP |
| Grafana | http://localhost:3000 | login `admin` / `admin`; folder **canteen**: Application, Business, Node Exporter |
| Kibana | http://localhost:5601 | Discover → data view **canteen logs** |
| Elasticsearch | http://localhost:9200 | `_cat/indices/canteen-logs-*?v` |

Node Exporter runs on the host network and is not published on a Mac; see it at Prometheus → Targets, or `docker exec canteen-prometheus wget -qO- http://node-exporter:9100/metrics`.

## Use

```sh
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' -d '{"stall":"chai","item":"karak"}'
#  {"order_id":"6942c973","stall":"chai","item":"karak","state":"waiting",...}
curl -s -X POST localhost:8000/orders/6942c973/ready
curl -s -X POST localhost:8000/orders/6942c973/pickup
curl -s localhost:8000/stalls              # orders waiting per stall
curl -s localhost:8000/metrics | grep canteen_orders
docker logs canteen-app | tail -3          # the JSON lines behind Kibana
```

Traffic for the dashboards: `python3 scripts/load.py --duration 60 --concurrency 4` runs 4 customers ordering at random stalls (10 % cancel) and prints client-side latency per route. Then open Grafana → canteen / Business.

Faults (Part E), toggled over HTTP, no restart:

```sh
curl -s -X POST localhost:8000/chaos -H 'Content-Type: application/json' -d '{"slow_every_n":5,"delay_ms":500}'   # every 5th /orders call sleeps 500 ms
curl -s -X POST localhost:8000/chaos -H 'Content-Type: application/json' -d '{"fail_stall":"biryani"}'           # biryani answers 503
curl -s -X POST localhost:8000/chaos/reset
```

## Test

```sh
uv sync
uv run pytest        # 11 tests: lifecycle, metrics, logs, chaos; no Docker needed
```

## Run the experiments

```sh
caffeinate -i scripts/experiment_fault.sh slow        # E.1: baseline / slow-every-5th / recovery, >= 120 s each (Mac: caffeinate keeps the laptop awake; drop it on Linux)
python3 scripts/stage_counts.py <start> <end> fault   # per-stage numbers from Prometheus + Elasticsearch (windows are printed by the runner)
python3 scripts/experiment_cardinality.py             # E.2: request_id label on a demo counter, capped at 100 series (~2 min, re-creates the app twice)
```

Both leave the app with chaos reset and `CANTEEN_DEMO_CARDINALITY=0`. Raw output lands in `scripts/results/`.

## Dashboards and Kibana

- **canteen / Application** — requests/s by status and by route, HTTP p50/p95/p99 and p95 by route, error rate, mean latency.
- **canteen / Business** — orders waiting, placed and cancelled, busiest stall, cancel rate, orders/min by stall, the waiting gauge by stall, prep-time p95 (histogram) next to prep-time mean (summary), pickup delay (the self-explored metric), cancellations/min.
- **canteen / Node Exporter (machine)** — the machine name, CPU, memory, disk, network, load, disk I/O.

Dashboards are JSON files in `monitoring/grafana/dashboards/`; Grafana re-reads them every 30 s. Kibana queries used in the report are in `scripts/kibana_queries.md`.

## Clean up

```sh
scripts/cleanup.sh            # docker compose down -v: containers + Prometheus/Elasticsearch/Grafana/Filebeat volumes
docker compose down           # containers only; metrics and logs are kept in named volumes for next time
```

## Troubleshooting

- **Kibana shows no data** — `docker compose ps -a | grep setup` must be `Exited (0)` and `filebeat` `Up`; generate a request first; `curl 'localhost:9200/_cat/indices/canteen-logs-*?v'`.
- **Prometheus target `node` DOWN** — node-exporter is on the host network and Prometheus reaches it at the docker0 gateway `172.17.0.1` (`extra_hosts` in `docker-compose.yml`). If `docker network inspect bridge` shows another gateway, change it. On Linux a firewall may block bridge → host.
- **Grafana panels empty** — no traffic yet (`scripts/load.py`), or the time picker is outside the run. "Data source not found": `docker compose up -d --force-recreate grafana`.
- **Elasticsearch exits 137** — Docker's memory limit; give Docker more RAM or lower `ES_JAVA_OPTS` in `docker-compose.yml`.
- **Port already in use** — another stack (e.g. `termlab`) owns 8000/3000/9090/9200/5601; `docker compose down` it first.

## Credits

See `REPORT.md` → Credits: Lab 1 (Midnight Launch) for the shape of the stack and the chaos-over-HTTP idea; my earlier `termlab` project for the Filebeat/Elasticsearch/Node Exporter configuration; Grafana community dashboard 1860 as the model for the Node dashboard; FastAPI, uvicorn, prometheus_client, structlog, pytest; Claude (Anthropic) as an AI pair programmer.
