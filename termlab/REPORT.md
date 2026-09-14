# termlab — Observability report

Enterprise Software Development, Fall 2026 — Assignment 1. Individual submission.

Code, compose stack, dashboards, scripts and raw results are in this directory (`README.md` explains how to run everything; `docs/architecture.md` has the full failure analysis). Screenshots are in `docs/screenshots/` (`docs/screenshots/README.md` says which panel, time range and query each one shows).

---

## A. Project

**Problem.** People learning Linux, trying a tool, or running a quick experiment want a shell *now*, without installing anything and without being able to damage anything. Hosted products solve this (GitHub Codespaces, Killercoda, Replit) but the interesting engineering is in the host: many users share one machine, every container is a potential leak, and the moment a spawn is slow or a neighbour is noisy every user feels it.

**Users.** Anyone with a browser on the same network: students in a lab session, a workshop audience, me testing shell one-liners. No account — a page load is an anonymous session.

**Solution.** `termlab`: a FastAPI control plane that owns a pool of hardened Docker containers and bridges a `docker exec` PTY to an xterm.js terminal over WebSocket.

- Click **New sandbox** → `POST /sessions` (anonymous token) → `POST /sessions/{id}/sandbox` → a slot in the pool of 10 (queue up to 60 s if full) → a *warm* pre-created container is claimed (~0 ms) or a *cold* one is created (~0.4–1.5 s) → `WS /ws/{id}` attaches `bash -l`.
- Every sandbox: `--network none`, 0.5 CPU, 256 MiB with swap disabled, 100 pids, uid 1000, read-only rootfs, tmpfs `/home/user` and `/tmp`, all capabilities dropped, `no-new-privileges`. `sleep infinity` under docker-init is PID 1; each terminal attach is a separate exec, so closing the tab does not kill the box — the **idle reaper** (15 min without keystrokes) does, or `exit`, or the OOM killer.
- Every container carries the label `termlab.sandbox=1`; the api removes everything with that label at startup (orphans from a crash) and shutdown.

**What works.** All of the above, verified by 55 tests (unit tests against an in-memory fake Docker; an integration test against the real daemon checking exec, resize, read-only rootfs, writable tmpfs home, no network, stats, removal) and by load runs: a 68-user smoke run and a 14-user run against the pool of 10 that shows the queue (`scripts/results/`), plus the experiment stages in E. Deliberately out of scope: persistence between sessions, login, sharing a terminal, and exposure to the internet (see D.4 on the Docker socket).

**How to try it.** `docker compose up -d --build`, open http://localhost:8000, click New sandbox, run `htop`, `stress-ng --cpu 4 --timeout 20s`, `python3 -c 'x=bytearray(300*2**20)'` (OOM), `exit`. Grafana at :3000 (admin/admin), Kibana at :5601.

---

## B. Metrics

### B.1 Metric table

All metrics are defined in one module, `api/metrics.py`, and carry the `termlab_` prefix. **Labels are bounded by construction**: `method`/`path` (path is templated to `/sessions/{id}`), `status`, `source∈{warm,cold}`, `outcome` (4 values), `reason` (6 values), `direction∈{in,out}`. Session ids, container ids and tokens never become labels — they are log fields. Enumerated label values are pre-initialised so `rate()` starts from 0 instead of "no data".

| Metric | Type | Labels | Unit | Kind | Purpose | Where / how recorded |
|---|---|---|---|---|---|---|
| `termlab_http_requests_total` | Counter | method, path, status | 1 | app | request volume and error share per route | `api/main.py` `observe_http` middleware, after every response (except `/metrics`) |
| `termlab_http_request_duration_seconds` | Histogram (11 buckets 5 ms–10 s) | method, path | s | app | latency per route; `POST /sessions/{id}/sandbox` = queue + spawn | same middleware, `observe(duration)` |
| `termlab_ws_messages_total` | Counter | direction | 1 | app | WebSocket frames relayed | `api/bridge.py`, per frame |
| `termlab_terminal_bytes_total` | Counter | direction | bytes | app | terminal throughput (counts only, never content) | `api/bridge.py`, `inc(len(chunk))` |
| **`termlab_terminal_roundtrip_seconds`** | Histogram (11 buckets 1 ms–2 s) | — | s | app, **self-explored** | typing lag as the server sees it: keystroke chunk forwarded → next PTY output chunk | `api/bridge.py`: timestamp on stdin forward, observed on next stdout chunk, one probe outstanding, discarded after 2 s |
| `termlab_sandbox_spawn_seconds` | Histogram (9 buckets 50 ms–10 s) | source | s | app | time to hand a user a running sandbox, warm vs cold | `api/sessions.py` `request_sandbox`, around warm-claim / cold `docker run` |
| `termlab_queue_wait_seconds` | Histogram (9 buckets 0–60 s) | — | s | app/biz | how long users waited for a slot | `api/pool.py` `acquire` |
| `termlab_stats_sample_seconds` | Summary | — | s | app | duration of one resource-sampling pass (Python summary: mean only) | `api/sessions.py` `stats_tick` |
| `termlab_pool_free` / `termlab_queue_length` | Gauge | — | 1 | app | free slots / waiting requests | `api/pool.py`, on every acquire/release |
| `termlab_warm_pool_size` | Gauge | — | 1 | app | pre-created sandboxes ready | `api/warm_pool.py` |
| `termlab_sandbox_cpu_cores_sum` / `_max` | Gauge | — | cores | app | CPU used by all / the busiest sandbox | `stats_tick` every 10 s from `docker stats` (Δcpu/Δsystem × cpus), aggregated |
| `termlab_sandbox_memory_bytes_sum` / `_max` | Gauge | — | bytes | app | memory used by all / the hungriest sandbox | same sampler |
| `termlab_sandbox_memory_ratio` | Histogram (7 buckets 5 %–100 %) | — | ratio | app | how close sandboxes get to their 256 MiB limit | same sampler, one observation per sandbox per pass |
| `termlab_build_info` | Gauge (=1) | version, fault_mode | 1 | — | which fault mode was active when | `api/main.py` lifespan |
| `termlab_sessions_started_total` | Counter | outcome | 1 | **business** | sandbox requests: ok / queued_timeout / pool_full / error | `api/sessions.py` `request_sandbox` |
| `termlab_sandboxes_reaped_total` | Counter | reason | 1 | **business** | why sandboxes end: idle / user_exit / oom / admin / orphan / error | `api/sessions.py` `reap` |
| `termlab_sandbox_seconds_total` | Counter | — | s | **business** | billable sandbox-time (sum of lifetimes) | `reap`, `inc(lifetime)` |
| `termlab_session_duration_seconds` | Summary | — | s | **business** | mean sandbox lifetime | `reap`, `observe(lifetime)` |
| `termlab_commands_total` | Counter | — | 1 | **business** | Enter presses (usage intensity, no content) | `api/bridge.py`, `count(b"\r")` |
| `termlab_sandboxes_active` / `termlab_users_connected` | Gauge | — | 1 | **business** | running sandboxes / open terminals | `sessions.py` `request_sandbox`/`reap` / `sessions.py` `on_attach`/`on_detach` (called by the bridge) |
| `termlab_demo_requests_total` | Counter | `request_id` **only when** `TERMLAB_DEMO_CARDINALITY=1` | 1 | E.2 only | the cardinality experiment | `POST /sessions` |

Counts (`curl -s localhost:8000/metrics | grep "^# TYPE termlab_"`): 25 metric families — 8 Counters, 10 Gauges, 5 Histograms, 2 Summaries — all four types, across application and business categories. `tests/test_metrics.py` asserts the four types and the `termlab_` prefix.

### B.2 Dashboard: termlab / Application (`monitoring/grafana/dashboards/termlab_app.json`)

| Panel | Query | What the chart shows |
|---|---|---|
| Requests/s by HTTP status | `sum by (status) (rate(termlab_http_requests_total[1m]))` | traffic and failures per second; 503 = turned away, 5xx = Docker failure |
| HTTP p95 by route (5m) | `histogram_quantile(0.95, sum by (le, path) (rate(termlab_http_request_duration_seconds_bucket[5m])))` | only `POST /sessions/{id}/sandbox` is slow (it contains queue + spawn); other routes are ms |
| Sandbox spawn p50/p95 by source (5m) | `histogram_quantile(0.95, sum by (le, source) (rate(termlab_sandbox_spawn_seconds_bucket[5m])))` | warm ≈ 0–50 ms, cold ≈ 0.4–1.5 s; under cold_start only cold exists and p95 > 2.5 s |
| Sandbox spawn mean by source | `sum by (source) (rate(…_sum[5m])) / sum by (source) (rate(…_count[5m]))` | the mean next to the percentiles |
| Terminal roundtrip p50/p95/p99 (1m) | `histogram_quantile(0.99, sum by (le) (rate(termlab_terminal_roundtrip_seconds_bucket[1m])))` | typing lag; ~5 ms p50 / ~20 ms p95 idle machine |
| Queue wait p95 + queue length | `histogram_quantile(0.95, sum by (le) (rate(termlab_queue_wait_seconds_bucket[5m])))`, `termlab_queue_length` | non-zero only when 10 sandboxes are running |
| Terminal bytes/s, WS frames/s | `sum by (direction) (rate(termlab_terminal_bytes_total[1m]))` | out ≫ in |
| Sandbox CPU / memory sum & max | the gauges | what the sandboxes cost the machine |
| Memory used/limit heatmap | `sum by (le) (increase(termlab_sandbox_memory_ratio_bucket[1m]))` | how close boxes get to OOM |
| Stats sampler mean (Summary) | `rate(termlab_stats_sample_seconds_sum[5m]) / rate(termlab_stats_sample_seconds_count[5m])` | daemon responsiveness |
| Build info | `termlab_build_info` | fault mode in effect |

![Application dashboard during the smoke load (2026-09-13 13:44–13:45 UTC)](docs/screenshots/b2_app_dashboard_smoke.png)

### B.3 Dashboard: termlab / Business (`termlab_business.json`)

| Panel | Query | What the chart shows |
|---|---|---|
| Active / free / warm / queued / connected | the gauges | live capacity picture |
| Pool utilisation | `100 * termlab_sandboxes_active / clamp_min(termlab_sandboxes_active + termlab_pool_free, 1)` | % of the pool in use |
| Sessions started by outcome (/min) | `sum by (outcome) (rate(termlab_sessions_started_total[5m])) * 60` | demand and how much of it was served |
| Sandbox success rate (10m) | `sum(rate(…{outcome="ok"}[10m])) / sum(rate(…[10m]))` | 1.0 unless users are turned away or Docker fails |
| Reaped by reason (/min), reap reasons (1h donut) | `sum by (reason) (rate(termlab_sandboxes_reaped_total[5m])) * 60`, `increase(…[1h])` | idle share = wasted capacity |
| Sandbox-hours billed | `termlab_sandbox_seconds_total / 3600` | what a hosted product would invoice |
| Average paid concurrency | `sum(rate(termlab_sandbox_seconds_total[5m]))` | billed sandbox-seconds per second |
| Average session duration (Summary) | `rate(termlab_session_duration_seconds_sum[10m]) / clamp_min(rate(…_count[10m]), 1e-9)` | mean lifetime of reaped sandboxes |
| Commands per minute | `sum(rate(termlab_commands_total[5m])) * 60` | how actively boxes are used |

![Business dashboard during the queue demo: 14 users on a pool of 10, four of them queued ~26 s (2026-09-13 13:45–13:46 UTC)](docs/screenshots/b3_business_dashboard_queue_demo.png)

### B.4 p95 / p99 and the time window

`histogram_quantile(0.95, sum by (le) (rate(<metric>_bucket[W])))` estimates the 95th percentile from the bucket counters *observed during the last W*. Two consequences drive the choice of W:

- **Enough observations.** With 5-second scrapes, `[1m]` contains 12 samples and `[5m]` 60. The roundtrip histogram receives several observations per second under load, so `[1m]` is stable and reacts within a minute — that is why the roundtrip panel uses it. Spawns and queue waits happen every few seconds, so a `[1m]` quantile jumps between bucket edges; those panels use `[5m]`, and HTTP `[5m]` as in the lab.
- **The value is a bucket boundary, not a measurement.** The spawn histogram has buckets 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10 s; a p95 of "3 s" means "95 % of spawns finished within the 3 s bucket" (during the cold_start fault the real values are ≈ 2.4–2.6 s). The Python Summaries (`termlab_stats_sample_seconds`, `termlab_session_duration_seconds`) have no percentiles, so the dashboards show their mean via `rate(_sum)/rate(_count)` and the percentiles come from histograms.

The experiment stages are ≥ 120 s so every `[1m]` panel is fully inside a stage and `[5m]` panels visibly ramp up and down.

### B.5 Self-explored metric: `termlab_terminal_roundtrip_seconds`

The lab's RED metrics describe request/response services; a terminal has no requests. What a user feels is *typing lag*. The bridge records the time from forwarding a stdin chunk to the PTY until the next output chunk arrives (normally the echo), one probe at a time, discarding probes older than 2 s (the user may be inside `cat` with echo off). It is a lower bound when a program prints on its own (`top`), so the load generator also measures it client-side (echo of the command line) and the two agree to within a few ms.

It is the metric that separates the two faults: **cold_start moves spawn latency and leaves roundtrip flat; cpu_hog leaves spawn mostly alone and moves roundtrip** (E.1). Baseline on this laptop: p50 ≈ 5 ms, p95 ≈ 15–20 ms, p99 ≈ 25–50 ms. 

![Terminal roundtrip p50/p95/p99 across the three cpu_hog stages (2026-09-13 14:04–14:12 UTC)](docs/screenshots/b5_roundtrip_cpu_hog.png)

### B.6 Node Exporter

`prom/node-exporter:v1.9.1` runs with `pid: host`, `uts: host` and `network_mode: host` and is scraped as job `node`. **Machine measured:** `node_uname_info{nodename="docker-desktop", release="6.12.54-linuxkit"}` — the Docker Desktop Linux VM (8 vCPU, 8 GiB), not macOS. That is the honest choice: every sandbox (and the cpu_hog stressors) runs inside this VM, so its CPU/memory panels show what the sandboxes do to the host. The **termlab / Node Exporter (machine)** dashboard has CPU busy %, memory used, root filesystem, network rx/tx, load average. Host networking matters for the network panel: without it Node Exporter reports its own container's `veth` (a few kB of scrape traffic), which is what my first version did; with it `node_network_*` lists the VM's `eth0`, `docker0` and the bridges, and the panel excludes `lo|veth.*|docker.*|br-.*` so it shows the VM's external traffic. A host-network container has no compose DNS name, so Prometheus reaches it via `extra_hosts: node-exporter:172.17.0.1` (the docker0 gateway); the price is that port 9100 is not published to the Mac (use Prometheus → Targets, or `docker exec termlab-prometheus wget -qO- http://node-exporter:9100/metrics`). On a Linux host `docker-compose.linux.yml` additionally mounts `/` so the disk panels are the physical machine's. 

![Node Exporter dashboard during cpu_hog: CPU busy pinned at 100 %, load1 up to 35 (2026-09-13 14:04–14:12 UTC)](docs/screenshots/b6_node_dashboard_cpu_hog.png)

---

## C. Logs

### C.1 What is logged, why, where

`api/logging_config.py` configures structlog to emit **one JSON object per line on stdout**; uvicorn's own loggers are routed through the same formatter, so the container never prints a plain-text line. Every line has `ts` (UTC ISO-8601), `level`, `service` (`termlab-api`), `event` (machine name), `msg` (human sentence); `request_id` and `session_id` are bound as context variables inside HTTP requests and WebSocket sessions.

| Event | Level | Extra fields | Emitted in | Why |
|---|---|---|---|---|
| `startup` / `shutdown` | info | version, fault_mode, pool_capacity, warm_pool_size, idle_timeout_s, orphans_removed, hogs | `main.py` lifespan | know the configuration behind every time range |
| `http_request` | info | method, path, route, status_code, status, duration_ms, request_id, session_id | middleware | one line per request, the log twin of the RED metrics (`/metrics` and `/health` are skipped: Prometheus and the container healthcheck would add 18 identical lines a minute; `/health` is still counted in the metrics) |
| `session_created` | info | session_id, user_id | `create_session` | start of a user's trace |
| `queue_wait` | info (warning > 5 s) | queue_ms, queue_length, pool_free | `request_sandbox` | who waited, how long |
| `limit_hit` | warning | outcome, waited_ms, pool_capacity, queue_length | `request_sandbox` | who was turned away |
| `sandbox_spawn` | info / error | sandbox_id, source, spawn_ms, queue_ms, image, cold_delay_ms, exc_type | `request_sandbox` | the per-spawn detail behind the histogram |
| `ws_attach` / `ws_detach` | info | sandbox_id, cols, rows, ws_count / reason, duration_ms, bytes_in, bytes_out, commands | `bridge.py` via `sessions.py` | terminal lifecycle; byte *counts* only |
| `sandbox_reaped` | info | reason, lifetime_s, bytes_in, bytes_out, commands, source | `reap` | end of trace + billing record |
| `queue_cancelled` | info | session_id | `request_sandbox` | user pressed Destroy (or closed the page) while still queued |
| `orphan_cleanup` | warning | removed, names, reason | startup/shutdown | leaks after a crash |
| `warm_pool` | info | action (fill/claim/drain), size, sandbox_id | `warm_pool.py` | why a spawn was warm or cold |
| `fault` | warning | fault_mode, hogs | `faults.py` `start_hogs` | the cpu_hog stressors were started (E.1.b) |
| `image_missing` | error | image | `main.py` lifespan | the sandbox image was not built; every spawn will fail |
| `resize_failed`, `error` | warning / error | exc_type, exc_message, traceback | anywhere | failures |

uvicorn's own loggers go through the same formatter, so the few lines it emits (`Started server process`, startup/shutdown messages, warnings) are JSON too, with the message in `event`/`msg` and no request id. `uvicorn.error` is kept at WARNING: at INFO it logs `connection open` / `connection closed` for every terminal and the WebSocket handshake URL *with the token in the query string*. I found that out from Kibana (`message : "[accepted]"` matched every attach), which is exactly the kind of thing the log pipeline is for; the level change plus a `redact_secrets_in_text` processor that masks any `token=…` inside a string fixed it, and `tests/test_logging.py` asserts both.

**What is deliberately not logged:** terminal input and output. Users type passwords into shells. `drop_forbidden_keys` removes `stdin`, `stdout`, `data`, `output`, `token`, `authorization`, `cookie`, … before rendering, `redact_secrets_in_text` masks `token=` values inside strings, and tests assert a fake secret never reaches the rendered line. No personal data exists: sessions are anonymous.

### C.2 How Filebeat collects and parses

Docker's `json-file` driver writes each stdout line as `{"log":"<our JSON>\n","stream":"stdout","time":"…"}` under `/var/lib/docker/containers/<id>/<id>-json.log` (3 files × 10 MB per the compose `logging` block). Filebeat (`monitoring/filebeat/filebeat.yml`) runs with the Docker socket and that directory mounted read-only:

1. **autodiscover (docker provider)** — a template with condition `equals: docker.container.name: termlab-api` starts a `filestream` input on that container's log file only. Prometheus/Grafana/ES/Kibana output and the sandboxes (whose PID 1 is `sleep`; shell output goes to the WebSocket, not the container log) never enter the index, and a re-created api (new container id) is followed automatically.
2. **container parser** (`format: docker`) unwraps the Docker envelope into `message`.
3. **`decode_json_fields`** (`fields: [message]`, `target: ""`, `overwrite_keys: true`) parses our JSON into top-level fields — `event`, `session_id`, `spawn_ms`, … become first-class, searchable fields.
4. **`timestamp`** copies our `ts` into `@timestamp`, so Kibana orders by *when the app wrote the line*, not when Filebeat read it.
5. **`drop_fields`** removes beat/docker noise; `add_docker_metadata` keeps `container.name`/`container.id`.
6. Output: `index: "termlab-logs-%{+yyyy.MM.dd}"`. Filebeat's own template/ILM setup is disabled: `scripts/setup_elastic.sh` (run by the one-shot `setup` compose service before Filebeat starts) installs `monitoring/elasticsearch/index_template.json` (keyword mappings for `event`, `session_id`, `source`, `reason`, …; numbers for `spawn_ms`, `queue_ms`, `bytes_in`, …; `lifetime_s` float) and the ILM policy. This avoids Filebeat's ECS template, which maps `event` as an object and would reject our string `event`.

No text parsing is needed anywhere: the app writes JSON, Filebeat decodes JSON.

### C.3 Where logs live, what survives, when they are deleted

| Stage | Where | Survives api restart | Survives `docker compose down` | Deleted |
|---|---|---|---|---|
| raw stdout | `/var/lib/docker/containers/<api-id>/*-json.log` (VM) | yes (until the container is re-created) | no | rotation at 3 × 10 MB; container removal |
| Filebeat offsets | volume `filebeat_registry` | yes | yes (not `down -v`) | — |
| indexed documents | volume `es_data`, one index per day `termlab-logs-YYYY.MM.DD` | yes | yes (not `down -v`) | ILM `termlab-logs-policy`: hot phase, **delete 7 days after index creation** (`_ilm/policy/termlab-logs-policy`) |

If Filebeat is down, nothing is lost up to 30 MB of api output: it resumes from the registry offset. If Elasticsearch is down, Filebeat retries with backoff. If the api container is removed (`down`), its raw log file is gone but everything already shipped remains in ES.

### C.4 One log line, its stored fields, a working search

Original line as the api wrote it (`docker logs termlab-api | grep sandbox_spawn | head -1`):

```json
{"msg": "sandbox ready from warm pool in 0 ms", "session_id": "386ed51a5c39", "user_id": "386ed51a5c39", "sandbox_id": "f54cd8362a1e", "source": "warm", "spawn_ms": 0, "queue_ms": 0, "image": "termlab-sandbox:local", "cold_delay_ms": 0, "request_id": "ac2d5cb47e3b", "event": "sandbox_spawn", "level": "info", "service": "termlab-api", "ts": "2026-09-13T13:41:28.403371Z"}
```

Stored document (`curl 'localhost:9200/termlab-logs-*/_search?q=event:sandbox_spawn&size=1'`, abridged):

```json
{"@timestamp": "2026-09-13T13:41:28.403Z", "ts": "2026-09-13T13:41:28.403371Z", "service": "termlab-api", "level": "info",
 "event": "sandbox_spawn", "msg": "sandbox ready from warm pool in 0 ms", "session_id": "386ed51a5c39", "user_id": "386ed51a5c39",
 "sandbox_id": "f54cd8362a1e", "source": "warm", "spawn_ms": 0, "queue_ms": 0, "cold_delay_ms": 0, "image": "termlab-sandbox:local",
 "request_id": "ac2d5cb47e3b", "container": {"id": "9141855e3b50…", "name": "termlab-api"},
 "message": "{\"msg\": \"sandbox ready from warm pool in 0 ms\", … }"}
```

`message` is the untouched original; every other field was extracted by `decode_json_fields`; `@timestamp` came from `ts`; `container.*` from `add_docker_metadata`. The mapping (`_mapping`) shows `event: keyword`, `session_id: keyword`, `spawn_ms: long`, `lifetime_s: float`, so range queries work.

Working searches (Kibana → Discover → **termlab logs**; full list in `scripts/kibana_queries.md`):

- Follow one user: `session_id : "386ed51a5c39"` → `session_created` → `http_request` (POST …/sandbox) → `queue_wait` → `sandbox_spawn` → `ws_attach` → `ws_detach` → `sandbox_reaped`.
- Find an error: `level : "error"`; slow spawns: `event : "sandbox_spawn" and spawn_ms > 2000`; users turned away: `event : "limit_hit"`.
- Shell equivalent: `curl --get 'localhost:9200/termlab-logs-*/_count' --data-urlencode 'q=event:"sandbox_spawn" AND spawn_ms:>2000'`.

![Kibana Discover: every line of session 386ed51a5c39, columns event / source / spawn_ms / queue_ms / reason](docs/screenshots/c4_kibana_session_trace.png)

---

## D. System design

### D.1 Architecture

```mermaid
flowchart LR
    subgraph browser["Browser (xterm.js)"]
        UI[index.html]
    end
    subgraph api["termlab-api  (FastAPI, :8000)"]
        HTTP["HTTP: /sessions, /sessions/{id}/sandbox, /pool, /health, /metrics"]
        WS["WS /ws/{id}: PTY bridge"]
        SM["SessionManager\npool (10) · queue · warm pool (2)\nidle reaper · stats sampler"]
    end
    subgraph dockerd["Docker daemon (docker-desktop VM)"]
        SBX1["sandbox\nsleep infinity + bash exec\n0.5 CPU · 256 MiB · no net"]
        SBX2["sandbox …"]
        WARM["warm sandbox ×2"]
        HOG["cpu_hog stressors\n(fault only)"]
    end
    subgraph metrics["Metrics"]
        PROM[(Prometheus\n5 s scrape · 7 d)]
        GRAF[Grafana\n3 provisioned dashboards]
        NODE[Node Exporter\n:9100]
    end
    subgraph logs["Logs"]
        JSONF[(json-file\n3 × 10 MB)]
        FB[Filebeat\nautodiscover termlab-api]
        ES[(Elasticsearch\ntermlab-logs-YYYY.MM.DD · ILM 7 d)]
        KB[Kibana\ndata view termlab-logs-*]
    end
    UI -- "fetch JSON (Bearer token)" --> HTTP
    UI -- "binary frames = keystrokes / output\ntext frames = resize / exit" --> WS
    HTTP --> SM
    WS --> SM
    SM -- "/var/run/docker.sock\ncreate · exec · resize · stats · remove" --> dockerd
    WS -- "exec socket (tty)" --> SBX1
    PROM -- "GET /metrics" --> HTTP
    PROM -- "GET /metrics" --> NODE
    NODE -. "/proc, /sys of the VM\n(where sandboxes run)" .- dockerd
    GRAF -- PromQL --> PROM
    api -- "stdout, one JSON object per line" --> JSONF
    JSONF -- "tail + decode_json_fields" --> FB
    FB -- "bulk index" --> ES
    KB -- KQL --> ES
    FB -. "docker.sock (read-only):\ncontainer names for autodiscover" .- dockerd
    UI -. "links in the sidebar" .-> GRAF
    UI -. "links in the sidebar" .-> KB
    SETUP[setup (one-shot curl)] -- "ILM policy · index template · data view" --> ES
    SETUP --> KB
    IMG[sandbox-image (one-shot build)] -. "termlab-sandbox:local" .- dockerd
```

Solid arrows are the runtime data paths; dotted ones are metadata and one-shot jobs. Every box is a compose service except the sandboxes, which the api creates through the Docker socket. `docs/architecture.md` has the same diagram with the per-component table (role, who it talks to, what state it holds and why) and the data-location table (what survives `docker compose down`, what `down -v` deletes, when ILM and retention delete the rest). Summary of the choices:

- **One control plane, N throwaway containers**, owned through one label (`termlab.sandbox=1`). Ownership by label is what makes crash recovery trivial: at startup the api removes everything it does not know about.
- **Shell = `docker exec`, not the container's main process**, so a closed tab does not kill a box and reattach is possible; the idle reaper is the real lifecycle owner.
- **Metrics are aggregated, logs are specific.** Prometheus gets bounded labels and per-sandbox resources summed/maxed; Elasticsearch gets session ids and per-event detail. Each tool gets the data shape it is good at.
- **Warm pool** turns the dominant latency (container create+start) into a background cost, and gives the spawn histogram a `source` label that makes the cold_start experiment legible.
- **Pull metrics, push logs.** Prometheus scrapes `/metrics` every 5 s (7-day TSDB); Filebeat tails Docker's json-file and pushes to ES (daily indices, 7-day ILM). Both keep data in named volumes across `docker compose down`.
- **Failure behaviour** (details in the architecture doc): losing Prometheus or Filebeat costs observability, never users; losing the Docker daemon or the api costs users their sandboxes but leaks nothing; a full pool degrades to a queue with a timeout rather than an error.

**What happens if a component stops** (users vs observability, and how it recovers):

| Failure | Effect on users | Effect on observability | Recovery |
|---|---|---|---|
| **Docker daemon down / socket unmounted** | `POST /sandbox` → 500 `spawn_failed`, `outcome=error`; attached terminals get EOF when their exec dies; `/health` reports `docker:false` | `termlab_sessions_started_total{outcome="error"}` rises, `level:"error"` logs with `exc_type` | api reconnects on the next call; orphans removed at next api start |
| **termlab-api crashes / restarts** | every terminal disconnects; in-memory session table is gone, so old tokens are 404; sandboxes are removed at the next startup (`orphan_cleanup`, reason `orphan`) | metrics counters reset to 0 (Prometheus `rate()` handles resets); a gap in the scrape; startup line in Kibana with the new `fault_mode` | compose `restart: unless-stopped` is deliberately *not* set so a crash is visible; `docker compose up -d api` |
| **A sandbox hits 256 MiB** | the kernel OOM-kills it; the shell dies; reaper marks the session `reason=oom` within 10 s | `termlab_sandboxes_reaped_total{reason="oom"}`, `sandbox_reaped` log with `reason: oom` | user clicks *New sandbox* |
| **Pool is full (10 running)** | the 11th request waits up to 60 s (`queue_length`, `queue_wait_seconds`), then 503 `queued_timeout` | Business dashboard: queued > 0, `outcome=queued_timeout`; `limit_hit` warnings in Kibana | someone exits or idles out; raise `TERMLAB_POOL_SIZE` |
| **Prometheus down** | none | Grafana panels empty ("no data"); metrics for the outage are lost forever (pull model, no buffering in the app) | `docker compose up -d prometheus`; history before the outage is in `prom_data` |
| **Grafana down** | none | no dashboards; Prometheus still has the data (query it at :9090) | restart; dashboards re-provision from files |
| **Node Exporter down** | none | machine panels empty; target shows DOWN in Prometheus | restart |
| **Filebeat down** | none | logs stop appearing in Kibana but are **not lost**: Docker keeps the last 3 × 10 MB per container and Filebeat resumes from its registry offset | restart; catch-up is automatic unless 30 MB was exceeded meanwhile |
| **Elasticsearch down** | none | Filebeat retries with backoff (its output queue holds events in memory; on restart it re-reads from the registry); Kibana shows "unavailable" | restart ES, wait for `healthy` |
| **Kibana down** | none | no UI; `curl :9200/termlab-logs-*/_search` still works | restart |
| **Laptop sleeps** (Docker Desktop VM paused) | terminals freeze; idle timer does not advance (monotonic clock) | flat gaps in every panel; a stage of an experiment is unusable | `caffeinate -i` while running experiments |

![Architecture diagram (docs/architecture.md rendered)](docs/screenshots/d1_architecture.png)

### D.2 Follow a metric: `termlab_sessions_started_total{outcome="ok"}`

1. **Code updates it.** `api/sessions.py` `request_sandbox()`: after the container is assigned, `metrics.SESSIONS_STARTED.labels(outcome="ok").inc()`. The Counter object lives in `api/metrics.py` (`Counter("termlab_sessions_started_total", …, ["outcome"])`; all four outcomes pre-created so each series exists from startup at 0).
2. **Exposed.** `GET /metrics` calls `generate_latest(REGISTRY)`; the exposition contains e.g. `termlab_sessions_started_total{outcome="ok"} 82.0` (`curl localhost:8000/metrics | grep sessions_started`).
3. **Prometheus collects and stores it.** `monitoring/prometheus/prometheus.yml` job `api`, target `api:8000`, `scrape_interval: 5s`. Each scrape appends a sample `(timestamp, 82)` to the series identified by the label set `{__name__="termlab_sessions_started_total", outcome="ok", instance="api:8000", job="api", service="termlab-api"}`, kept 7 days in `prom_data`. Query: `curl 'localhost:9090/api/v1/query?query=termlab_sessions_started_total{outcome="ok"}'` → `82`.
4. **Grafana queries and displays it.** Datasource uid `prometheus` (`monitoring/grafana/provisioning/datasources/prometheus.yml`). Business dashboard panel "Sessions started by outcome" runs `sum by (outcome) (rate(termlab_sessions_started_total[5m])) * 60` — a per-minute rate that survives counter resets when the api restarts — and "Sandbox requests by outcome (last hour)" runs `increase(…[1h])` for the absolute count. 

![Step 2: the counter in GET /metrics](docs/screenshots/d2_metrics_endpoint.png)

![Step 3: the same series in the Prometheus graph](docs/screenshots/d2_prometheus_graph.png)

![Step 4: the Grafana panel that plots it](docs/screenshots/d2_grafana_panel.png)

### D.3 Follow a log: one `sandbox_spawn` line

1. **Code writes it.** `api/sessions.py` `request_sandbox()`: `log.info("sandbox_spawn", msg=…, session_id=…, source=…, spawn_ms=…, queue_ms=…)`. structlog processors add `level`, `service`, `ts`, merge the `request_id` context variable, drop forbidden keys, render JSON, print to stdout.
2. **Docker saves it.** json-file driver → `/var/lib/docker/containers/<id>/<id>-json.log`, wrapped as `{"log":"…","stream":"stdout","time":"…"}`.
3. **Filebeat collects and parses it.** autodiscover matches `termlab-api`; container parser unwraps; `decode_json_fields` → top-level fields; `timestamp` → `@timestamp`; bulk-indexed into `termlab-logs-2026.09.13`.
4. **Elasticsearch stores it** with the mapping from the index template (`event`, `source` keyword; `spawn_ms`, `queue_ms` long), ILM policy attached.
5. **Kibana finds it.** Discover, data view `termlab-logs-*`, `event : "sandbox_spawn" and source : "cold" and spawn_ms > 2000` → the cold_start stage's spawns, and only those. Format changes along the way: our JSON → Docker envelope → flat ES document with `message` (original) plus extracted fields and `@timestamp`.

### D.4 Things I do not fully understand yet / known limits

- **The Docker socket.** Mounting `/var/run/docker.sock` makes the api container root-equivalent on the Docker host: the sandboxes are hardened, the api is not. For anything beyond a local classroom stack this needs a socket proxy with an allow-list (e.g. tecnativa/docker-socket-proxy) or a separate host, and a VM-per-sandbox runtime (gVisor/Firecracker) if the users are strangers. I did not build that.
- **Where exactly `docker stats` cpu numbers come from** on cgroup v2 under Docker Desktop — I compute Δ`cpu_usage.total_usage` / Δ`system_cpu_usage` × `online_cpus`, which matches `docker stats` output, but I have not read the daemon code that fills those fields.
- **Filebeat's in-memory queue on ES outage**: I know it retries and resumes from the registry, but not the exact size after which events are dropped without restarting.
- **Prometheus staleness**: after removing the `request_id` label, `count()` drops to 1 within one scrape because Prometheus writes staleness markers, yet `last_over_time(…[15m])` still sees 101 series — I understand the observed behaviour, less so the exact interaction with head compaction.
- The roundtrip metric is a **lower bound** when programs print unsolicited output; the client-side measurement in `load.py` is the cross-check.
- **A bug the fault found.** The first cpu_hog run produced no CPU load at all: `stress-ng` exited with *temp-path '.' must be readable and writeable* because a Docker tmpfs is root-owned by default and uid 1000 could not write its own `/home/user` — which also meant real users could not create files in `~`. The metrics made it obvious (machine CPU flat at 13 % during the "fault"), the fix is `uid=1000,gid=1000` on the tmpfs mount (`api/docker_client.py`) plus an integration-test assertion; the experiment below is the re-run.
- **A second bug found by using it.** Clicking *New sandbox* repeatedly created a new anonymous session each time and left the previous sandbox running until the 15-minute idle reap, so one person could fill the whole pool and then queue behind their own boxes. The Business dashboard showed it plainly (active 10/10, queue 1, one browser). Fix: the page keeps its session in `sessionStorage` and reattaches on reload, destroys the previous sandbox before requesting a new one, and `DELETE /sessions/{id}` now cancels a request that is still queued (`QueueCancelled` in `api/pool.py`, `queue_position` in `GET /sessions/{id}`).
- **Per-sandbox CPU needs two samples.** `termlab_sandbox_cpu_cores_*` is a delta over consecutive 10-second samples, so sandboxes that live < 10 s (the cold_start load pattern) never contribute; the cpu_hog run keeps terminals open 20 s for that reason.

---

## E. Experiments

### E.1 Reproduce a problem — a slow sandbox host (`cold_start`) and a noisy neighbour (`cpu_hog`)

**Setup.** `scripts/experiment_fault.sh <mode> 8` runs three stages of ≥ 120 s (24 scrapes each), each with at least 8 virtual users at concurrency 4, every user opening a real WebSocket terminal, running 5 commands (echo / ls / python / 1 s of stress-ng / base64 of 200 kB) and typing `exit`. The fault is toggled by re-creating the api with `TERMLAB_FAULT=<mode>` (which also reaps running sandboxes, reason `admin`, and — for cpu_hog — leaves the stressors to be removed by the next restart's orphan cleanup). `termlab_build_info{fault_mode}` and the `startup` log line record the mode. For each run I wrote the predictions (the P-rows below) in my notes before starting the script and filled in the result column afterwards from `scripts/stage_counts.py`; the tables keep that order.

#### E.1.a `cold_start` — warm pool disabled, +2 s inside every spawn (deterministic)

| # | Prediction | Result |
|---|---|---|
| P1 | spawn p95 rises from < 0.1 s (warm) to > 2.5 s; only `source="cold"` receives samples in stage 2 | **Confirmed.** spawn p95: warm 0.048 s (baseline) → cold 2.95 s (fault; bucket edge, real values 2.25 s mean / 2.47 s p95 / 2.56 s max from the client) → warm 0.048 s (recovery). Stage 2 had 100 cold spawns and 0 warm. |
| P2 | `termlab_warm_pool_size` 2 → 0 → 2 | **Confirmed.** avg `termlab_warm_pool_size` per stage: 1.6 → 0.0 → 1.5 (it dips below 2 while a claim is being refilled). |
| P3 | HTTP p95 for `POST /sessions/{id}/sandbox` rises by ≈ 2 s; `POST /sessions` unchanged | **Confirmed.** HTTP p95 `POST /sessions/{id}/sandbox`: 0.005 s → 2.465 s → 0.005 s; `POST /sessions`: 0.005 / 0.007 / 0.005 s. |
| P4 | terminal roundtrip p95 unchanged (fault is before the shell exists) | **Confirmed.** roundtrip p95 0.021 / 0.017 / 0.015 s; p99 0.046 / 0.023 / 0.032 s (client-side p95 22.9 / 16.5 / 17.6 ms). |
| P5 | fewer sessions per stage (each user spends 2 s longer), zero `error` outcomes, zero 5xx | **Confirmed.** sessions ok per ~122 s stage: 184 → 100 → 182; `error` = 0, `queued_timeout` = 0, HTTP 5xx = 0 in all stages. |
| P6 | Kibana `event:"sandbox_spawn" and spawn_ms > 2000` matches every spawn in stage 2 and none in 1/3 | **Confirmed.** Kibana `event:"sandbox_spawn" and spawn_ms > 2000`: 0 / **100** / 0 documents per stage; `source:"cold"` 4 / 100 / 4, `source:"warm"` 176 / 0 / 176. |
| P7 | recovery: [1m] panels back to baseline within a minute, [5m] within 5 | **Confirmed.** Recovery stage numbers equal baseline within noise (table below); the [1m] roundtrip panel never moved, the [5m] spawn/HTTP panels decayed over the first ~5 min of stage 3. |

Stage windows (UTC) and per-stage numbers (`scripts/stage_counts.py`, raw in `scripts/results/`). Prometheus counts are `increase()` over the stage window, so they are extrapolated and fractional (184.5 is printed as 184); the Kibana document counts are exact:

| Stage | UTC window | users | spawns warm / cold | spawn p95 warm / cold (s) | HTTP p95 sandbox route (s) | roundtrip p95 / p99 (s) | ok / timeout / error | 5xx | spawn_ms>2000 docs |
|---|---|---|---|---|---|---|---|---|---|
| baseline (none) | 13:47:59 → 13:50:02 | 180 | 176 / 4 | 0.048 / 0.95 | 0.005 | 0.021 / 0.046 | 184 / 0 / 0 | 0 | 0 |
| fault (cold_start) | 13:50:15 → 13:52:20 | 100 | 0 / 100 | – / 2.95 | 2.465 | 0.017 / 0.023 | 100 / 0 / 0 | 0 | 100 |
| recovery (none) | 13:52:34 → 13:54:36 | 182 | 178 / 4 | 0.048 / 0.90 | 0.005 | 0.015 / 0.032 | 182 / 0 / 0 | 0 | 0 |

Raw: `scripts/results/fault_cold_start_20260913T134748Z.txt` (+ `.stages`), `load_*_cold_start_*.json`, `stage_counts_cold_start.txt`. Note the baseline "cold p95 0.95 s" is a bucket-edge estimate from only 4 cold spawns (client-measured 0.47 s mean / 0.57 s max) — see B.4.

**Cause and effect on users.** With the warm pool off every "New sandbox" pays `docker create` + `start` plus the injected 2 s, so the button takes ≈ 2.5 s instead of being instant; nothing fails and typing feels the same once the shell is up. The metrics say exactly that: spawn and the sandbox route's HTTP latency move, roundtrip and error counters do not — a slow *host*, not an overloaded one.

![Spawn p50/p95 by source and HTTP p95 by route across the three cold_start stages (2026-09-13 13:47–13:55 UTC)](docs/screenshots/e1a_spawn_http_p95_cold_start.png)

![Kibana: event:"sandbox_spawn" and spawn_ms > 2000 over the same window; hits only inside the fault stage](docs/screenshots/e1a_kibana_spawn_ms_cold_start.png)

#### E.1.b `cpu_hog` — 4 unlimited `stress-ng --cpu 0` containers (noisy neighbour)

| # | Prediction | Result |
|---|---|---|
| P1 | machine CPU busy → ≈ 100 %, `node_load1` ≥ number of vCPUs | **Confirmed.** machine CPU busy (avg over the stage): 23 % → **99.98 %** → 24 %; `node_load1` max 3.7 → **35.5** (8 vCPUs) → 30.5 (decaying). |
| P2 | terminal roundtrip p95 rises ≥ 3× (user sandboxes and the api compete for CPU) | **Confirmed.** server-side roundtrip p95 0.009 → **0.047** → 0.010 s (5.2×), p99 0.023 → 0.098 → 0.033 s; client-side p95 11.4 → 63.0 → 14.8 ms, p99 27 → 102 → 43 ms. |
| P3 | spawn p95 rises for both sources (the daemon is slower), less than under cold_start | **Confirmed.** cold spawn p95 0.975 → 1.95 → 0.975 s (client: 675 → 1374 → 634 ms mean); warm p95 unchanged at 0.048 s (a claim is a dict pop); HTTP p95 for the sandbox route 0.81 → 2.22 → 0.81 s. Smaller than cold_start's +2 s, as predicted. |
| P4 | no change in outcomes (ok only), no 5xx | **Confirmed.** ok = 31 / 35 / 31 per stage, `queued_timeout` = `error` = 0, HTTP 5xx = 0, `level:"error"` = 0. |
| P5 | recovery after the restart removes the hogs (`orphan_cleanup` log line, `reason=orphan` reaps = 4) | **Confirmed with a nuance.** Kibana in the fault window: `event:"fault"` = 1 ("started 4 unlimited stress-ng containers"), `event:"orphan_cleanup"` = 1 at the recovery restart. The api's own shutdown removes its hogs (`stop_hogs`), so the orphan path only ran for the sandboxes the previous container left; `docker ps -a --filter label=termlab.hog=1` = 0 afterwards. |

| Stage | UTC window | users | machine CPU busy | load1 max | roundtrip p50 / p95 / p99 (s) | cold spawn p95 (s) | HTTP p95 sandbox route (s) | ok / timeout / error |
|---|---|---|---|---|---|---|---|---|
| baseline (none) | 14:04:25 → 14:06:36 | 36 | 23 % | 3.7 | 0.002 / 0.009 / 0.023 | 0.975 | 0.81 | 31 / 0 / 0 |
| fault (cpu_hog) | 14:06:53 → 14:09:02 | 36 | **100 %** | **35.5** | 0.010 / **0.047** / **0.098** | **1.95** | **2.22** | 35 / 0 / 0 |
| recovery (none) | 14:09:21 → 14:11:32 | 36 | 24 % | 30.5 → falling | 0.002 / 0.010 / 0.033 | 0.975 | 0.81 | 31 / 0 / 0 |

Terminals were kept open 20 s each (`SESSION_SECONDS=20`, concurrency 6) so the stats sampler could see them (`termlab_sandbox_cpu_cores_sum` max ≈ 1.0–1.1 cores in every stage: the user sandboxes themselves stayed within their 0.5-core caps; the extra load is entirely the unlimited hogs, which the sampler deliberately excludes and Node Exporter shows). The baseline HTTP p95 of 0.81 s for the sandbox route is the histogram bucket that contains the cold spawns (≈ 0.67 s) — see B.4.

Raw: `scripts/results/fault_cpu_hog_*.txt` (+ `.stages`), `load_*_cpu_hog_*.json`, `stage_counts_cpu_hog.txt`, `experiments_console_cpu_hog.txt`. The first attempt is also kept (`fault_cpu_hog_20260913T135501Z.txt`): its "fault" stage showed *no* change because stress-ng could not start — the bug described in D.4.

**Effect on users:** everything still works but typing lags and sandboxes start slower — the classic noisy-neighbour signature, visible on the Node dashboard (CPU pegged) and the roundtrip panel, invisible in the error counters.

### E.2 Cardinality explosion (capped at 100)

`scripts/experiment_cardinality.py`: re-create the api with `TERMLAB_DEMO_CARDINALITY=1` so `termlab_demo_requests_total` gains a `request_id` label; send 100 × `POST /sessions`, each with its own `X-Request-ID`; wait 2 scrapes; then re-create with the flag off, send 20 more, wait, compare.

| Snapshot | `count(termlab_demo_requests_total)` | `count(last_over_time(…[15m]))` | series API (30 m) | `prometheus_tsdb_head_series` | `count({__name__=~"termlab_.*"})` |
|---|---|---|---|---|---|
| before | 1 | 1 | 1 | 3,587 | 136 |
| flag on, idle | 0 (a labelled counter has no series until its first label value is seen) | 1 | 1 | 3,616 | 105 |
| flag on, after 100 requests | 100 | 101 | 101 | 3,716 | 220 |
| flag off, after 20 requests | 1 | 101 | 101 | 3,722 | 121 |

**Reading.** 100 requests → 100 series for one counter (each id is its own series, each series ≈ 1–2 kB of memory in the head block plus index entries). After the label is removed the live count drops to 1 on the next scrape (Prometheus writes staleness markers for series that vanished), but the 100 old series are still on disk and still answer range queries (`last_over_time`, series API) until the 7-day retention deletes them — removing a label does not delete history.

**At scale:** cardinality is multiplicative, `series = metrics × Π(distinct values per label)`. This service's bounded labels give 105–136 `termlab_` series in the table above (every histogram bucket, `_sum` and `_count` is its own series, and the HTTP counter grows one series per method × route × status seen since the last restart — which is why the number drops after each api re-create and climbs back). One demo counter with one id label added as many series as the whole service had. One `session_id` label on the HTTP counter alone would add a series per session per status per route — at 1 000 sessions/day that is tens of thousands of series that never stop being scraped as long as the process lives, each costing head memory, WAL and index space, and slowing every `rate()` over the metric. That is why ids live in logs: Elasticsearch indexes `session_id` as a keyword and a query for one id costs one lookup, not one time series per id.

![Prometheus graph of count(termlab_demo_requests_total): 1 → 100 → 1 across the experiment (2026-09-13 14:11–14:14 UTC)](docs/screenshots/e2_prometheus_count_demo_requests.png)

---

## Credits

- **Course material.** Enterprise Software Development (Habib University), Fall 2026, Assignment 1. Lab 1 (Midnight Launch) is the reference for the metric vocabulary (RED, `histogram_quantile` over `rate(_bucket[5m])`) and for the structure of Parts B–E.
- **Reused patterns.** The compose layout (one-shot `setup` job for the ILM policy/index template/data view, Filebeat Docker autodiscover with a custom index template, the three-stage fault runner and the capped cardinality script) is carried over from my earlier `fixit` project for the same assignment and adapted to this app. The Node Exporter dashboard is a trimmed, hand-written subset of the community dashboard *Node Exporter Full* (Grafana ID 1860, by rfmoz); the Application and Business dashboards are generated by `scripts/gen_dashboards.py`.
- **Libraries and images.** FastAPI, uvicorn, docker-py, prometheus_client, structlog, pytest, websockets (load generator), uv; xterm.js 5.5 with the fit addon (loaded from jsDelivr); Prometheus, Grafana, Node Exporter, Elasticsearch, Kibana, Filebeat, curl and Alpine images as pinned in `docker-compose.yml`.
- **Documentation consulted.** Prometheus docs (histograms and summaries, label best practices, staleness), prometheus_client docs (Summary has no quantiles), Filebeat docs (autodiscover, `decode_json_fields`, `timestamp` processor), Elasticsearch ILM and index-template docs, Docker docs for the json-file driver and container hardening options.
- **AI assistance.** I used Claude (Anthropic, via Claude Code) as a pair programmer while building this: drafting code and configuration from my design, generating the dashboard JSON from the panel list, reviewing the report against the assignment brief, and finding bugs (it found the token-in-uvicorn-log issue in C.1 during that review). All design decisions, experiments, measurements and the text of this report are mine and I have run and checked everything described here myself.

## Appendix: compose services

| Service | Image | Role |
|---|---|---|
| `api` | built from `api/Dockerfile` (python:3.12-slim) | control plane, `/metrics`, JSON logs |
| `sandbox-image` | built from `sandbox/Dockerfile` (alpine:3.20) | one-shot: builds `termlab-sandbox:local` |
| `prometheus` | prom/prometheus:v3.5.0 | 5 s scrape, 7 d retention |
| `grafana` | grafana/grafana:12.1.0 | provisioned datasource + 3 dashboards |
| `node-exporter` | prom/node-exporter:v1.9.1 | machine metrics (`docker-desktop`) |
| `elasticsearch` | elasticsearch:8.18.0 | single node, security off, 768 MB heap |
| `kibana` | kibana:8.18.0 | Discover |
| `filebeat` | filebeat:8.18.0 | autodiscover `termlab-api` → ES |
| `setup` | curlimages/curl:8.14.1 | one-shot: ILM policy, index template, data view |
