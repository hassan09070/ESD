# fixit

## What it is

`fixit` is a small agentic coding tool: you give it a task such as *"make the failing tests pass"* and an LLM-driven loop reads files, edits them and runs the tests in a sandbox until they pass. It is built as a FastAPI server plus a thin CLI so that every request, LLM call, tool call and task is measured with Prometheus metrics and written as structured JSON logs. Those flow into Grafana (metrics) and Filebeat → Elasticsearch → Kibana (logs), which is the actual point of the project: Enterprise Software Development, Assignment 1 (Observability).

## Prerequisites

- Docker Desktop (or Docker Engine + Compose v2). Give it **≥ 6 GB RAM**: Elasticsearch and Kibana take ~3 GB together.
- [`uv`](https://docs.astral.sh/uv/) for the CLI and the tests (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- Optional: an Anthropic API key. Everything works without one using the deterministic `MockLLM`.

## Start

```sh
cp .env.example .env          # defaults: FIXIT_LLM=mock, FIXIT_FAULT=none, no API key needed
docker compose up -d          # first run pulls ~4 GB of images
docker compose ps             # wait until elasticsearch/kibana are "healthy" and setup has "Exited (0)" (~90 s)
uv sync                       # local venv for the CLI + tests
```

| Service | URL | Notes |
|---|---|---|
| Agent API | http://localhost:8000 | `GET /health`, `GET /metrics`, `POST /tasks`, `GET /tasks/{run_id}` |
| Prometheus | http://localhost:9090 | Status → Targets should show `agent`, `node`, `prometheus` UP |
| Grafana | http://localhost:3000 | login `admin` / `admin`; dashboards are in folder **fixit** |
| Kibana | http://localhost:5601 | Discover → data view **fixit logs** (`fixit-logs-*`) |
| Elasticsearch | http://localhost:9200 | `_cat/indices/fixit-logs-*?v` |
| Node Exporter | http://localhost:9100/metrics | machine metrics |

To use the real model instead of the mock: put `ANTHROPIC_API_KEY=sk-ant-…` and `FIXIT_LLM=anthropic` in `.env`, then `docker compose up -d agent`.

## Use

`./sample_repo` (a calculator with 2 of 5 tests failing) is bind-mounted into the agent container at `/workspace`; the CLI maps `--repo ./sample_repo` to that path.

To point the agent at a different repository, set `FIXIT_REPO` and re-create the container, then use `--repo /workspace`:

```sh
FIXIT_REPO=/path/to/your/repo docker compose up -d agent
uv run fixit run "make the failing tests pass" --repo /workspace
docker compose up -d agent          # back to ./sample_repo
```

Note that the default `MockLLM` is scripted for the sample repo only: it always rewrites `calculator.py` with known-good content. Fixing real code needs `FIXIT_LLM=anthropic` and an API key in `.env`.

```sh
$ uv run fixit health
{'status': 'ok', 'llm': 'mock', 'fault': 'none', 'version': '0.1.0'}

$ uv run fixit run "make the failing tests pass" --repo ./sample_repo
run_id     : 884f4fd91e03
outcome    : success
iterations : 5
duration   : 1.92 s
tokens     : 7500 in / 1000 out
cost       : $0.0375
changed    : calculator.py
summary    : Fixed subtract (arguments were swapped) and divide (off-by-one). All tests pass.

$ uv run fixit status 884f4fd91e03      # same fields, fetched from the server's in-memory history
$ uv run fixit history                  # last 20 tasks, newest first

$ ./sample_repo/reset.sh                # put the bugs back so the next run is identical
sample_repo reset: 2 of 5 tests failing again
```

The agent loop is deliberately tiny: three tools (`read_file`, `write_file`, `run_tests`), at most 10 iterations, tests run with a 30 s timeout in a temp copy of the repo, and only on success are the changed files copied back.

## Test

```sh
uv run pytest                                   # 19 tests: loop, tools, sandbox timeout, metrics, log redaction
python3 scripts/load.py --tasks 20              # 20 mock tasks through the API, prints success count + mean/p95
python3 scripts/load.py --tasks 20 --concurrency 4 --min-duration 60
```

`load.py` resets the sample repo before every task, sends an `X-Request-ID` per request, and needs only the standard library.

## Run the experiments

Part E.1, slow-LLM fault (three stages of ≥ 2 min: baseline → fault → recovery; the fault is toggled by re-creating the agent container with `FIXIT_FAULT=slow`):

```sh
scripts/experiment_fault.sh slow 30      # prints UTC start/end of each stage, writes scripts/results/*.json
scripts/experiment_fault.sh flaky 30     # same with LLMError on every 5th call → retries
```

Part E.2, cardinality explosion (capped at 100 series):

```sh
python3 scripts/experiment_cardinality.py
```

Both scripts restart only the `agent` container, so Prometheus and Elasticsearch keep the history.

## Dashboards and Kibana

Grafana → Dashboards → folder **fixit**:

- **fixit / Application** — requests/s by status, HTTP p95, LLM p95/p99, LLM error rate, avg tool time (Summary), tool mix, build info.
- **fixit / Business** — tasks in progress, success rate, tasks by outcome, task-duration p95, iterations heatmap, cost/hour, cumulative tokens.
- **fixit / Node Exporter (machine)** — hostname from `node_uname_info`, CPU, memory, disk, network, load.

Every panel's PromQL is in `monitoring/grafana/dashboards/*.json` and in the panel description (hover the ⓘ).

Kibana → Discover → data view **fixit logs**. Useful columns: `event`, `run_id`, `iteration`, `tool`, `duration_ms`, `status`, `msg`. The KQL searches used in the report are in [`scripts/kibana_queries.md`](scripts/kibana_queries.md), e.g.

```
run_id : "884f4fd91e03"
event : "llm_call" and duration_ms > 3000
event : "llm_retry"
level : "error"
```

## Clean up

```sh
scripts/cleanup.sh        # docker compose down -v (removes containers, network AND the prom/es/grafana/filebeat volumes), rm -rf /tmp/fixit, reset sample_repo
```

`docker compose down` (without `-v`) keeps the Prometheus TSDB and Elasticsearch indices; `down -v` wipes them. Docker's own copy of the agent log (`json-file`, 3 × 10 MB) is deleted whenever the agent container is removed, which `down` does.

## Troubleshooting

- **Elasticsearch exits with `max virtual memory areas vm.max_map_count [65530] is too low`** (Linux): `sudo sysctl -w vm.max_map_count=262144`. Docker Desktop already sets this.
- **Elasticsearch keeps restarting / OOM**: raise Docker Desktop memory to 6–8 GB; heap is pinned at 1 GB (`ES_JAVA_OPTS`) and the container at 2 GB.
- **Grafana panels empty**: check http://localhost:9090/targets — `agent` must be UP. If it's DOWN, `docker compose logs agent`. If targets are UP but panels are empty, no tasks have run yet: `python3 scripts/load.py --tasks 20`.
- **Kibana shows no data view / no fields**: the one-shot `setup` container creates them; `docker compose logs setup` should end with `setup done`; re-run with `docker compose up setup`.
- **Kibana "No results"**: widen the time picker; `@timestamp` is the time the *agent* wrote the line (UTC), not the Filebeat ingest time.
- **Node Exporter fails with `path / is mounted on / but it is not a shared or slave mount`**: Docker Desktop cannot bind-mount the host root. The default compose file therefore measures the Docker Linux VM (hostname `docker-desktop`). On a real Linux host use the real machine: `docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d`.
- **Filebeat: `config file must be owned by the user identifier`**: already handled by `--strict.perms=false` in the compose command.
- **Duplicate log lines after a restart**: shouldn't happen — Filebeat's registry lives in the `filebeat_registry` volume. If you `down -v`, the registry is wiped together with the indices, so nothing is duplicated either.
- **`uv run fixit run` says repo path not visible**: the agent can only see `./sample_repo` (mounted at `/workspace`). Pass `--repo ./sample_repo` or `--repo /workspace`.

## Credits

- Prometheus, Grafana, Node Exporter, Elasticsearch, Kibana, Filebeat: the official Docker images, versions pinned in `docker-compose.yml`.
- Python libraries: FastAPI, uvicorn, Typer, httpx, prometheus_client, structlog, anthropic SDK.
- Node dashboard is a trimmed hand-written version of the ideas in Grafana community dashboard 1860 (Node Exporter Full).
- Agent loop, tools, sandbox, metrics, logging config, dashboards, scripts and this documentation were written for this assignment, with Claude Code (Anthropic) used as a pair-programming assistant; all design decisions and results are the author's.
