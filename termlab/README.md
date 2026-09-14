# termlab

## What it is

`termlab` gives anyone with a browser a throwaway Linux shell: open the page, click **New sandbox**, and an xterm.js terminal attaches to a fresh, hard-limited Docker container (0.5 CPU, 256 MiB, 100 processes, no network, read-only root filesystem). Ten sandboxes can run at once; the eleventh person queues. A box is destroyed when you type `exit` or after 15 minutes without keystrokes. It is the classroom-scale version of Codespaces / Killercoda / Replit.

It exists for Enterprise Software Development, Assignment 1 (Observability): the control plane is instrumented with Prometheus metrics (all four types, application + business), writes structured JSON logs, and ships with Grafana, Node Exporter, Filebeat, Elasticsearch and Kibana in one `docker compose`. The interesting operational problems of a multi-tenant sandbox host — pool exhaustion, cold starts, noisy neighbours, leaked containers — are real here, not simulated, which is what makes it worth observing.

`REPORT.md` is the assignment report; `docs/architecture.md` has the diagram and failure analysis.

## Prerequisites

- **Docker Desktop** (macOS/Windows) or Docker Engine + Compose v2 on Linux. Give it **≥ 8 GB RAM**: Elasticsearch + Kibana take ~2.5 GB and ten sandboxes can take 2.5 GB more.
- [`uv`](https://docs.astral.sh/uv/) for the tests and the load generator (`curl -LsSf https://astral.sh/uv/install.sh | sh`). The stack itself needs only Docker.
- A browser with internet access for the first page load (xterm.js is loaded from jsDelivr).

## Start

```sh
cp .env.example .env          # optional; defaults are fine
docker compose up -d --build  # builds the sandbox image and the api; pulls ~4 GB of images on first run
docker compose ps             # wait until elasticsearch/kibana are "healthy" (~90 s)
docker compose ps -a | grep -E 'setup|sandbox-image'   # both must show "Exited (0)"
```

| Service | URL | Notes |
|---|---|---|
| termlab | http://localhost:8000 | the terminal page; API docs at `/docs`, metrics at `/metrics` |
| Prometheus | http://localhost:9090 | Status → Targets should show `api`, `node`, `prometheus` UP |
| Grafana | http://localhost:3000 | login `admin` / `admin`; folder **termlab**: Application, Business, Node Exporter |
| Kibana | http://localhost:5601 | Discover → data view **termlab logs** (`termlab-logs-*`) |
| Elasticsearch | http://localhost:9200 | `_cat/indices/termlab-logs-*?v` |
| Node Exporter | (host network, not published on a Mac) | machine metrics of the Docker VM (`nodename=docker-desktop`); see them at Prometheus → Status → Targets → `node`, or `docker exec termlab-prometheus wget -qO- http://node-exporter:9100/metrics` |

The api reaches the Docker daemon through `/var/run/docker.sock` mounted into its container. That makes the api container root-equivalent on the Docker host; fine for a local classroom stack, not for a public deployment (see `REPORT.md` §D.4).

On Linux, add the host mounts for Node Exporter so it measures the real machine:
`docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d`.

## Use

Open http://localhost:8000, click **New sandbox**, type. The header shows where the sandbox came from (`warm` = pre-created, `cold` = created on demand), how long it took, and the pool fill; the sidebar shows live session stats and one-click demo commands. Reloading the page reattaches to your sandbox; **New sandbox** destroys the previous one first (one sandbox per person — ten clicks do not take ten slots); **Destroy** ends it, and also cancels a request that is still waiting in the queue. Try:

```sh
htop                      # 0.5 CPU, 256 MiB - watch the limits
stress-ng --cpu 4 --timeout 20s   # pegs your 0.5 core; see Grafana "Sandbox CPU" and the Node dashboard
python3 -c 'x = bytearray(300 * 2**20)'   # OOM-killed at 256 MiB -> sandbox reaped, reason=oom
touch /etc/x              # read-only rootfs
ping 1.1.1.1              # no network
exit                      # destroys the sandbox
```

Everything the page does is plain HTTP + WebSocket, so it can be scripted:

```sh
S=$(curl -s -X POST localhost:8000/sessions)                       # {"session_id":..,"token":..}
SID=$(echo $S | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')
TOK=$(echo $S | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
curl -s -X POST -H "Authorization: Bearer $TOK" localhost:8000/sessions/$SID/sandbox   # queues if full
curl -s -H "Authorization: Bearer $TOK" localhost:8000/sessions/$SID                   # state, bytes, uptime
curl -s localhost:8000/pool                                                            # capacity/active/free/queue/warm
curl -s -X DELETE -H "Authorization: Bearer $TOK" localhost:8000/sessions/$SID
```

Terminal: `ws://localhost:8000/ws/$SID?token=$TOK&cols=120&rows=40` — binary frames are keystrokes/output, text frames are `{"type":"resize","cols":..,"rows":..}` in and `{"type":"exit"}` out. `scripts/load.py` is a complete client.

Tunables (`.env`, then `docker compose up -d api`): `TERMLAB_POOL_SIZE` (10), `TERMLAB_WARM_POOL_SIZE` (2), `TERMLAB_IDLE_TIMEOUT_S` (900), `TERMLAB_QUEUE_TIMEOUT_S` (60), `TERMLAB_FAULT` (`none|cold_start|cpu_hog`), `TERMLAB_DEMO_CARDINALITY` (0/1).

## Test

```sh
unset VIRTUAL_ENV   # only if your shell has another venv active
uv sync
uv run pytest       # 55 tests; the Docker integration test skips itself when no daemon is reachable
```

The unit tests run the whole control plane against an in-memory `FakeDocker` (echo shells over socketpairs). `tests/test_integration_docker.py` spawns a real sandbox and checks the exec PTY, resize, read-only rootfs, no network, stats and removal.

Load: `uv run scripts/load.py --users 20 --concurrency 4 --min-duration 60 --json --label smoke` opens real WebSocket terminals and prints spawn/queue/roundtrip statistics. `--users 14 --concurrency 14 --session-seconds 25` saturates the pool of 10 and shows the queue.

## Run the experiments

Run these under `caffeinate -i` on a Mac (a sleeping laptop pauses the Docker VM).

**E.1 fault** — three stages (baseline / fault / recovery), ≥ 120 s each, fault toggled by re-creating the api:

```sh
caffeinate -i scripts/experiment_fault.sh cold_start 8    # warm pool off + 2 s per spawn (deterministic)
caffeinate -i scripts/experiment_fault.sh cpu_hog 8       # 4 unlimited stress-ng containers (noisy neighbour)
python3 scripts/stage_counts.py <start> <end> baseline    # per-stage numbers from Prometheus + Elasticsearch
```

The runner prints each stage's UTC window (also in `scripts/results/fault_<mode>_<ts>.txt.stages`); paste them into Grafana / Kibana time pickers.

**E.2 cardinality** — `python3 scripts/experiment_cardinality.py` (capped at 100 labelled series; ~2 min).

Both leave the stack in `TERMLAB_FAULT=none` / `TERMLAB_DEMO_CARDINALITY=0`. Results land in `scripts/results/`.

## Dashboards and Kibana

Grafana → Dashboards → folder **termlab**:

- **termlab / Application** — request rate & latency per route, sandbox spawn p50/p95 by source, terminal roundtrip p50/p95/p99 (the self-explored metric), queue wait, bytes/frames, sandbox CPU/memory, memory-ratio heatmap, sampler duration, build info.
- **termlab / Business** — active/free/warm/queued/connected stats, pool utilisation, sessions by outcome, success rate, reaps by reason, sandbox-hours billed, average paid concurrency, average session duration, commands per minute.
- **termlab / Node Exporter (machine)** — CPU, memory, disk, network, load of the Docker VM (`node_uname_info{nodename}` names it).

The dashboard JSON in `monitoring/grafana/dashboards/` is generated by `scripts/gen_dashboards.py`; edit the script, re-run it, Grafana re-reads the files within 30 s.

Kibana → Discover → **termlab logs**. `scripts/kibana_queries.md` lists the KQL queries used in the report (trace a session, slow spawns, queue waits, limit hits, errors) with `curl` equivalents.

## Clean up

```sh
scripts/cleanup.sh            # compose down -v + removes every container labelled termlab.sandbox=1
docker rmi termlab-sandbox:local   # optional
```

`docker compose down` (without `-v`) keeps Prometheus/Elasticsearch/Grafana data in named volumes and removes the api, which removes its own sandboxes on the way out. User sandboxes are *not* compose services, so `cleanup.sh` is the safe teardown after a crash.

## Troubleshooting

- **`docker compose ps` shows `api` unhealthy / restarting** — `docker logs termlab-api`. `image_missing` means `termlab-sandbox:local` was not built: `docker compose up -d --build sandbox-image`. A `docker.sock` permission error on Linux: add your user to the `docker` group or run compose with sudo.
- **"no sandbox: queued_timeout"** — ten sandboxes are running. `curl localhost:8000/pool`; wait for an idle reap, or `TERMLAB_POOL_SIZE=20 docker compose up -d api`.
- **Terminal hangs after the laptop slept** — the Docker VM was paused; reload the page (a new `bash` attaches to the same sandbox).
- **Prometheus target `node` is DOWN** — node-exporter runs with `network_mode: host` and Prometheus reaches it at the docker0 gateway `172.17.0.1` (`extra_hosts` in `docker-compose.yml`). If `docker network inspect bridge` shows another gateway, change that IP. On Linux, a host firewall (ufw) may block traffic from the compose bridge to the host.
- **Grafana panels empty** — no traffic yet (`scripts/load.py`), or the time picker is outside the run. "Data source not found": `docker compose up -d --force-recreate grafana`.
- **Kibana shows no data** — `docker compose ps -a | grep setup` must be `Exited (0)` and `filebeat` `Up`; `curl 'localhost:9200/_cat/indices/termlab-logs-*?v'`. Filebeat only ships the container named `termlab-api`.
- **Leftover `termlab-sbx-*` containers** after killing the api hard — `scripts/cleanup.sh`, or just start the api: it removes them at startup (`orphan_cleanup` log line).
- **Elasticsearch exits 137** — Docker's memory limit; raise Docker Desktop's RAM or lower `ES_JAVA_OPTS` in `docker-compose.yml`.
- **`uv run` complains about `VIRTUAL_ENV`** — `unset VIRTUAL_ENV`.

## Credits

Built for Enterprise Software Development (Habib University), Fall 2026, Assignment 1. Full credits are in `REPORT.md` → Credits: Lab 1 (Midnight Launch) for the metric vocabulary; the compose/Filebeat/experiment-script pattern from my earlier `fixit` project; the Node dashboard trimmed from Grafana community dashboard 1860 (*Node Exporter Full*); libraries FastAPI, uvicorn, docker-py, prometheus_client, structlog, xterm.js and the pinned Prometheus/Grafana/Elastic images; Claude (Anthropic) as an AI pair programmer for drafting code, generating dashboard JSON and reviewing the report. Design, experiments and results are my own.
