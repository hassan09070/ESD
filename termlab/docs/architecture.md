# termlab architecture

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

## Components

| Component | What it does | Talks to | State it holds |
|---|---|---|---|
| **browser** (`api/static/index.html`) | xterm.js terminal; creates a session, requests a sandbox, opens the WebSocket, forwards keystrokes and resize events | api over HTTP + WS | session id + token in JS memory only |
| **termlab-api** | control plane: anonymous sessions, pool/queue, warm pool, idle reaper, orphan cleanup, resource sampler, PTY bridge, `/metrics` | Docker daemon through the mounted socket | in-memory session table (lost on restart, by design — sandboxes are throwaway) |
| **sandbox containers** | `sleep infinity` under docker-init; each terminal attach is a `docker exec bash -l`; hardened: `--network none`, 0.5 CPU, 256 MiB (no swap), 100 pids, uid 1000, read-only rootfs, tmpfs `/home/user` + `/tmp`, all capabilities dropped, `no-new-privileges` | nothing (no network) | tmpfs only; gone at reap |
| **warm sandboxes** | 2 pre-created sandboxes (label `termlab.warm=1`) so a claim costs ~0 ms instead of a ~0.5–1.5 s create+start | – | – |
| **Prometheus** | scrapes `api:8000/metrics`, `node-exporter:9100`, itself every 5 s; 7-day retention | api, node-exporter | `prom_data` volume (TSDB) |
| **Grafana** | three provisioned dashboards (Application, Business, Node Exporter); datasource uid `prometheus` | Prometheus | `grafana_data` volume (only prefs/users; dashboards are files) |
| **Node Exporter** | machine metrics of the kernel it runs on (`pid`, `uts` and `network` namespaces are the host's) — under Docker Desktop that is the `docker-desktop` VM, i.e. exactly where the sandboxes run; Prometheus reaches it at the docker0 gateway because a host-network container has no compose DNS name | – | – |
| **Filebeat** | Docker autodiscover; tails only the container named `termlab-api`; `decode_json_fields` turns each line into fields; `@timestamp` := our `ts` | Docker socket (metadata), `/var/lib/docker/containers` (log files), Elasticsearch | `filebeat_registry` volume (read offsets: a restart does not re-ship) |
| **Elasticsearch** | stores `termlab-logs-YYYY.MM.DD`; explicit keyword/number mappings from `monitoring/elasticsearch/index_template.json`; ILM deletes indices after 7 days | – | `es_data` volume |
| **Kibana** | Discover on the `termlab-logs-*` data view | Elasticsearch | its own saved objects in ES |
| **setup** | one-shot `curl` job: PUT ILM policy, PUT index template, POST Kibana data view; Filebeat waits for it | ES, Kibana | none (idempotent) |
| **sandbox-image** | one-shot `docker build` of `sandbox/`; the api waits for it | – | image `termlab-sandbox:local` |

## Why this shape

- **One control plane, many throwaway containers.** The product *is* multi-tenancy on one machine, so the interesting problems (pool exhaustion, cold starts, noisy neighbours, leaked containers) are real, not simulated. Every sandbox carries `termlab.sandbox=1`; that label is the whole ownership model — the api removes everything with it at startup (orphans) and shutdown (admin reap), and `scripts/cleanup.sh` does the same from outside.
- **The shell is an `exec`, not the container's main process.** A closed browser tab therefore never kills the sandbox; the idle reaper does (15 min without keystrokes). Reattaching creates a fresh `bash`; the previous one gets `SIGHUP`.
- **Metrics stay low-cardinality; identities go to logs.** Session ids, container ids, tokens are never labels. Per-container CPU/memory is aggregated (sum/max + a ratio histogram) before it reaches Prometheus. The one deliberate exception — `termlab_demo_requests_total{request_id}` — exists only for the cardinality experiment and only when `TERMLAB_DEMO_CARDINALITY=1`.
- **Terminal content is never logged.** Users type passwords into shells. The log pipeline carries byte counts, newline counts and lifecycle events; `drop_forbidden_keys` in `api/logging_config.py` is the safety net.
- **Logs are JSON from the first byte.** uvicorn's loggers are routed through the same structlog formatter, so Filebeat never meets a plain-text line and needs no grok/dissect parsing.
- **Filebeat autodiscover by container name**, not a glob over `/var/lib/docker/containers`: Prometheus, Grafana, ES and Kibana stdout never enter the index, sandboxes cannot (their PID 1 is `sleep infinity` and shell output goes to the WebSocket, never to the container log), and a re-created api container (new id) is picked up automatically.
- **Setup is a compose job, not a README step.** The ILM policy, index template and data view are installed by the `setup` service before Filebeat starts, so the first document already lands with the right mapping.

## What happens if a component stops

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

## Where data lives and when it is deleted

| Data | Location | Survives `docker compose down`? | Survives `down -v` / `cleanup.sh`? | Deleted when |
|---|---|---|---|---|
| api stdout (raw JSON logs) | `/var/lib/docker/containers/<api-id>/*-json.log` in the VM | no (container removed) | no | rotated at 3 × 10 MB; removed with the container |
| Filebeat read offsets | volume `filebeat_registry` | yes | no | – |
| Indexed logs | volume `es_data`, indices `termlab-logs-YYYY.MM.DD` | yes | no | ILM `termlab-logs-policy` deletes an index 7 days after creation |
| Metrics | volume `prom_data` | yes | no | Prometheus retention 7 d (`--storage.tsdb.retention.time=7d`) |
| Grafana prefs/users | volume `grafana_data` | yes | no | – (dashboards are re-provisioned from `monitoring/grafana/dashboards/`) |
| Sessions | api memory | no | no | api restart, or the reaper forgets sandbox-less sessions after 15 min |
| Sandbox filesystems | tmpfs inside each container | no | no | reap (idle 15 min / exit / OOM / api restart) |
