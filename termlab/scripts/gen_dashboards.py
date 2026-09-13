#!/usr/bin/env python3
"""Generates monitoring/grafana/dashboards/termlab_app.json and termlab_business.json.

The JSON files are the artefact Grafana provisions (and what the report quotes); this
script only exists so 30 panels share one shape. Re-run after editing and commit both.
Window rule (REPORT B.4): [1m] for high-frequency observations (roundtrip, bytes, HTTP),
[5m] for events that happen every few seconds (spawn, queue, reap) so histogram_quantile
has enough samples, [1h] increase() for business counts.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "monitoring" / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}
_id = 0


def nid() -> int:
    global _id
    _id += 1
    return _id


def target(expr: str, legend: str, ref: str, fmt: str | None = None) -> dict:
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}
    if fmt:
        t["format"] = fmt
    return t


def timeseries(title, desc, queries, unit, x, y, w=12, h=8, stack=False, maxv=None, fill=12):
    d = {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": fill, "showPoints": "never"}, "min": 0}
    if stack:
        d["custom"]["stacking"] = {"mode": "normal"}
    if maxv is not None:
        d["max"] = maxv
    return {"id": nid(), "type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [target(e, l, chr(65 + i)) for i, (e, l) in enumerate(queries)],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}


def stat(title, desc, expr, legend, unit, x, y, w=4, h=4, thresholds=None, decimals=None):
    steps = thresholds or [{"color": "green", "value": None}]
    d = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        d["decimals"] = decimals
    return {"id": nid(), "type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(expr, legend, "A")],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "value",
                        "graphMode": "area", "textMode": "auto", "justifyMode": "auto", "orientation": "auto"}}


def piechart(title, desc, expr, legend, x, y, w=12, h=8):
    return {"id": nid(), "type": "piechart", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(expr, legend, "A")],
            "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "pieType": "donut",
                        "legend": {"displayMode": "list", "placement": "right", "showLegend": True},
                        "displayLabels": ["percent"], "tooltip": {"mode": "single"}}}


def heatmap(title, desc, expr, x, y, w=12, h=8, yunit="percentunit"):
    return {"id": nid(), "type": "heatmap", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target(expr, "{{le}}", "A", fmt="heatmap")],
            "fieldConfig": {"defaults": {"custom": {"scaleDistribution": {"type": "linear"}}}, "overrides": []},
            "options": {"calculate": False, "yAxis": {"unit": yunit, "decimals": 2}, "color": {"mode": "scheme", "scheme": "Oranges", "steps": 64},
                        "cellGap": 1, "legend": {"show": True}, "tooltip": {"mode": "single", "yHistogram": True}, "exemplars": {"color": "rgba(255,0,255,0.7)"}}}


def dashboard(uid, title, tags, panels, refresh="10s"):
    return {"uid": uid, "title": title, "tags": tags, "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
            "refresh": refresh, "time": {"from": "now-1h", "to": "now"}, "templating": {"list": []}, "annotations": {"list": []},
            "panels": panels}


def q(p, expr):
    return f"histogram_quantile({p}, sum by (le) (rate({expr})))"


APP = [
    timeseries("Requests/s by HTTP status", "Control-plane request rate split by status code, 1-minute window (12 scrapes). 503 = pool full / queue timeout, 4xx = bad tokens, 5xx = spawn failures.",
               [("sum by (status) (rate(termlab_http_requests_total[1m]))", "{{status}}")], "reqps", 0, 0),
    timeseries("HTTP p95 by route (5m window)", "95th percentile latency per route from histogram buckets. POST /sessions/{id}/sandbox includes queue wait + spawn, so it moves with the cold_start fault; every other route should stay flat.",
               [("histogram_quantile(0.95, sum by (le, path) (rate(termlab_http_request_duration_seconds_bucket[5m])))", "{{path}}")], "s", 12, 0),
    timeseries("Sandbox spawn p50 / p95 by source (5m window)", "Time to hand a user a running sandbox. warm = claimed from the pre-created pool (~ms); cold = docker create+start (~0.5-1.5 s). TERMLAB_FAULT=cold_start disables warm and adds +2 s: only the cold series exists and p95 > 2.5 s.",
               [("histogram_quantile(0.5, sum by (le, source) (rate(termlab_sandbox_spawn_seconds_bucket[5m])))", "p50 {{source}}"),
                ("histogram_quantile(0.95, sum by (le, source) (rate(termlab_sandbox_spawn_seconds_bucket[5m])))", "p95 {{source}}")], "s", 0, 8),
    timeseries("Sandbox spawn mean by source (5m window)", "rate(sum)/rate(count) of the same histogram: the average, for comparison with the percentiles. Both sources are pre-initialised so a missing line means 'no spawns of that kind', not 'no data'.",
               [("sum by (source) (rate(termlab_sandbox_spawn_seconds_sum[5m])) / sum by (source) (rate(termlab_sandbox_spawn_seconds_count[5m]))", "mean {{source}}")], "s", 12, 8),
    timeseries("Terminal roundtrip p50 / p95 / p99 (1m window)", "SELF-EXPLORED METRIC. Time from forwarding a keystroke chunk to the PTY until the next output chunk - typing lag as the server sees it. Many observations per second under load, so a 1-minute window is stable. Rises under cpu_hog (noisy neighbour), unchanged under cold_start.",
               [(q(0.5, "termlab_terminal_roundtrip_seconds_bucket[1m]"), "p50"), (q(0.95, "termlab_terminal_roundtrip_seconds_bucket[1m]"), "p95"), (q(0.99, "termlab_terminal_roundtrip_seconds_bucket[1m]"), "p99")], "s", 0, 16),
    timeseries("Queue wait p95 (5m) and queue length", "How long sandbox requests waited for a free pool slot, and how many are waiting right now. Non-zero only when active sandboxes == pool capacity (10 by default).",
               [(q(0.95, "termlab_queue_wait_seconds_bucket[5m]"), "wait p95 (s)"), ("termlab_queue_length", "queued now")], "s", 12, 16),
    timeseries("Terminal bytes/s", "Bytes relayed through all terminals per second (counts only; content is never recorded). out >> in: a few keystrokes produce screens of output.",
               [("sum by (direction) (rate(termlab_terminal_bytes_total[1m]))", "{{direction}}")], "Bps", 0, 24),
    timeseries("WebSocket frames/s", "Frames relayed per second in each direction.",
               [("sum by (direction) (rate(termlab_ws_messages_total[1m]))", "{{direction}}")], "ops", 12, 24),
    timeseries("Sandbox CPU (cores) - sum / max", "Sampled every 10 s from docker stats and aggregated across running user sandboxes (no per-container labels). Each sandbox is capped at 0.5 core.",
               [("termlab_sandbox_cpu_cores_sum", "sum"), ("termlab_sandbox_cpu_cores_max", "max")], "none", 0, 32),
    timeseries("Sandbox memory - sum / max", "Same sampler, memory. Each sandbox is capped at 256 MiB (swap disabled): an OOM-killed sandbox is reaped with reason=oom.",
               [("termlab_sandbox_memory_bytes_sum", "sum"), ("termlab_sandbox_memory_bytes_max", "max")], "bytes", 12, 32),
    heatmap("Sandbox memory used / limit (heatmap)", "Distribution of memory-usage ratio per sandbox per sample (histogram buckets 5%..100%). Hot cells near 1.0 mean sandboxes are close to being OOM-killed.",
            "sum by (le) (increase(termlab_sandbox_memory_ratio_bucket[1m]))", 0, 40),
    timeseries("Stats sampler pass duration - mean (Summary)", "A Python Summary (no percentiles): rate(sum)/rate(count). Grows with the number of sandboxes and when the Docker daemon is busy.",
               [("rate(termlab_stats_sample_seconds_sum[5m]) / rate(termlab_stats_sample_seconds_count[5m])", "mean")], "s", 12, 40),
    stat("Build info", "Current api configuration from termlab_build_info labels. Changes when the container is re-created with a different TERMLAB_FAULT.",
         "termlab_build_info", "v{{version}} fault={{fault_mode}}", "none", 0, 48, w=24, h=3),
]

BIZ = [
    stat("Active sandboxes", "User sandboxes running right now.", "termlab_sandboxes_active", "active", "none", 0, 0),
    stat("Free slots", "Pool capacity minus active.", "termlab_pool_free", "free", "none", 4, 0,
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 1}, {"color": "green", "value": 3}]),
    stat("Warm sandboxes", "Pre-created sandboxes ready to be claimed (0 under cold_start).", "termlab_warm_pool_size", "warm", "none", 8, 0,
         thresholds=[{"color": "orange", "value": None}, {"color": "green", "value": 1}]),
    stat("Queued requests", "Sandbox requests waiting for a slot.", "termlab_queue_length", "queued", "none", 12, 0,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 4}]),
    stat("Connected terminals", "Open terminal WebSockets.", "termlab_users_connected", "connected", "none", 16, 0),
    stat("Pool utilisation", "active / capacity.", "100 * termlab_sandboxes_active / clamp_min(termlab_sandboxes_active + termlab_pool_free, 1)", "util", "percent", 20, 0,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 70}, {"color": "red", "value": 90}], decimals=0),
    timeseries("Sessions started by outcome (per minute)", "Business counter: sandbox requests per minute by outcome. ok = got a sandbox; queued_timeout = waited the full TERMLAB_QUEUE_TIMEOUT_S; pool_full = refused immediately (timeout 0); error = Docker failed.",
               [("sum by (outcome) (rate(termlab_sessions_started_total[5m])) * 60", "{{outcome}}")], "none", 0, 4, stack=True),
    timeseries("Sandbox success rate (10m window)", "ok / all sandbox requests. Drops below 1 only when users are turned away or Docker fails - not when spawns are merely slow.",
               [("sum(rate(termlab_sessions_started_total{outcome=\"ok\"}[10m])) / clamp_min(sum(rate(termlab_sessions_started_total[10m])), 1e-9)", "success")], "percentunit", 12, 4, maxv=1),
    timeseries("Sandboxes reaped by reason (per minute)", "idle = 15 min without keystrokes; user_exit = typed exit / DELETE; oom = hit the 256 MiB limit; admin = api shutdown; orphan = leftovers removed at startup.",
               [("sum by (reason) (rate(termlab_sandboxes_reaped_total[5m])) * 60", "{{reason}}")], "none", 0, 12, stack=True),
    piechart("Reap reasons (last hour)", "Share of each reap reason over the last hour. A large idle share means people walk away from sandboxes - capacity is wasted on nobody.",
             "sum by (reason) (increase(termlab_sandboxes_reaped_total[1h]))", "{{reason}}", 12, 12),
    timeseries("Sandbox-hours billed (cumulative)", "termlab_sandbox_seconds_total / 3600: total sandbox lifetime, the number a hosted product would invoice. Increments when a sandbox is reaped.",
               [("termlab_sandbox_seconds_total / 3600", "sandbox-hours")], "none", 0, 20),
    timeseries("Average paid concurrency (5m window)", "rate(termlab_sandbox_seconds_total[5m]) = billed sandbox-seconds per second = average number of sandboxes being paid for. Compare with the active gauge: lifetime is only booked at reap time, so this lags.",
               [("sum(rate(termlab_sandbox_seconds_total[5m]))", "avg sandboxes billed")], "none", 12, 20),
    timeseries("Average session duration (Summary)", "Python Summary: rate(sum)/rate(count) over 10 minutes = mean sandbox lifetime among sandboxes reaped in the window.",
               [("rate(termlab_session_duration_seconds_sum[10m]) / clamp_min(rate(termlab_session_duration_seconds_count[10m]), 1e-9)", "mean lifetime")], "s", 0, 28),
    timeseries("Commands per minute", "Enter presses across all terminals per minute (no command content is recorded). A proxy for how actively the sandboxes are used.",
               [("sum(rate(termlab_commands_total[5m])) * 60", "commands/min")], "none", 12, 28),
    timeseries("Sandbox requests by outcome (last hour, cumulative)", "increase() over 1 h of the same counter as the rate panel - the absolute numbers for the report.",
               [("sum by (outcome) (increase(termlab_sessions_started_total[1h]))", "{{outcome}}")], "none", 0, 36, w=24),
]

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, d in (("termlab_app.json", dashboard("termlab-app", "termlab / Application", ["termlab", "application"], APP)),
                    ("termlab_business.json", dashboard("termlab-business", "termlab / Business", ["termlab", "business"], BIZ))):
        (OUT / name).write_text(json.dumps(d, indent=2) + "\n")
        print(f"wrote {OUT / name} ({len(d['panels'])} panels)")
