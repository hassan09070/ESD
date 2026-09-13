#!/usr/bin/env python3
"""Summarise one experiment stage from Prometheus and Elasticsearch, given its UTC time range.

    python3 scripts/stage_counts.py 2026-09-13T14:00:00Z 2026-09-13T14:02:30Z [label]

Prints, for that window: spawn p50/p95/p99 by source, terminal roundtrip p95/p99, queue
wait p95, HTTP p95 for POST /sessions/{id}/sandbox, sessions by outcome, reaps by reason,
machine CPU busy % (Prometheus, evaluated at the window end over the window length) and the
Kibana-style document counts (Elasticsearch). Used to fill the [RESULT] cells in REPORT.md.
Stdlib only.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROM = "http://localhost:9090"
ES = "http://localhost:9200"


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def prom(query: str, at: float) -> float | None:
    q = urllib.parse.urlencode({"query": query, "time": at})
    with urllib.request.urlopen(f"{PROM}/api/v1/query?{q}", timeout=10) as r:
        res = json.load(r)["data"]["result"]
    if not res:
        return None
    v = float(res[0]["value"][1])
    return None if v != v else v  # NaN -> None


def es_count(q: str, start: str, end: str) -> int:
    body = {"query": {"bool": {"must": [{"query_string": {"query": q}}],
                               "filter": [{"range": {"@timestamp": {"gte": start, "lte": end}}}]}}}
    req = urllib.request.Request(f"{ES}/termlab-logs-*/_count", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["count"]


def main() -> int:
    start, end = sys.argv[1], sys.argv[2]
    label = sys.argv[3] if len(sys.argv) > 3 else ""
    at = ts(end)
    w = f"{max(60, int(ts(end) - ts(start)))}s"

    def hq(p, metric, sel=""):
        return f"histogram_quantile({p}, sum by (le) (rate({metric}_bucket{sel}[{w}])))"

    rows = {
        "spawn p50 warm (s)": prom(hq(0.50, "termlab_sandbox_spawn_seconds", "{source='warm'}"), at),
        "spawn p95 warm (s)": prom(hq(0.95, "termlab_sandbox_spawn_seconds", "{source='warm'}"), at),
        "spawn p50 cold (s)": prom(hq(0.50, "termlab_sandbox_spawn_seconds", "{source='cold'}"), at),
        "spawn p95 cold (s)": prom(hq(0.95, "termlab_sandbox_spawn_seconds", "{source='cold'}"), at),
        "spawn p99 all (s)": prom(hq(0.99, "termlab_sandbox_spawn_seconds"), at),
        "spawn mean all (s)": prom(f"sum(rate(termlab_sandbox_spawn_seconds_sum[{w}])) / sum(rate(termlab_sandbox_spawn_seconds_count[{w}]))", at),
        "spawns warm (count)": prom(f"sum(increase(termlab_sandbox_spawn_seconds_count{{source='warm'}}[{w}]))", at),
        "spawns cold (count)": prom(f"sum(increase(termlab_sandbox_spawn_seconds_count{{source='cold'}}[{w}]))", at),
        "roundtrip p50 (s)": prom(hq(0.50, "termlab_terminal_roundtrip_seconds"), at),
        "roundtrip p95 (s)": prom(hq(0.95, "termlab_terminal_roundtrip_seconds"), at),
        "roundtrip p99 (s)": prom(hq(0.99, "termlab_terminal_roundtrip_seconds"), at),
        "queue wait p95 (s)": prom(hq(0.95, "termlab_queue_wait_seconds"), at),
        "HTTP p95 POST sandbox (s)": prom(hq(0.95, "termlab_http_request_duration_seconds", "{path='/sessions/{id}/sandbox',method='POST'}"), at),
        "HTTP p95 POST /sessions (s)": prom(hq(0.95, "termlab_http_request_duration_seconds", "{path='/sessions',method='POST'}"), at),
        "HTTP 5xx (count)": prom(f"sum(increase(termlab_http_requests_total{{status=~'5..'}}[{w}])) or vector(0)", at),
        "sessions ok": prom(f"sum(increase(termlab_sessions_started_total{{outcome='ok'}}[{w}]))", at),
        "sessions queued_timeout": prom(f"sum(increase(termlab_sessions_started_total{{outcome='queued_timeout'}}[{w}]))", at),
        "sessions error": prom(f"sum(increase(termlab_sessions_started_total{{outcome='error'}}[{w}]))", at),
        "reaped user_exit": prom(f"sum(increase(termlab_sandboxes_reaped_total{{reason='user_exit'}}[{w}]))", at),
        "reaped admin": prom(f"sum(increase(termlab_sandboxes_reaped_total{{reason='admin'}}[{w}]))", at),
        "warm pool size (avg)": prom(f"avg_over_time(termlab_warm_pool_size[{w}])", at),
        "sandbox cpu cores sum (max)": prom(f"max_over_time(termlab_sandbox_cpu_cores_sum[{w}])", at),
        "machine CPU busy % (avg)": prom(f"100 * (1 - avg(rate(node_cpu_seconds_total{{mode='idle'}}[{w}])))", at),
        "machine load1 (max)": prom(f"max_over_time(node_load1[{w}])", at),
    }
    kql = {
        'event:"sandbox_spawn" and spawn_ms > 2000': 'event:"sandbox_spawn" AND spawn_ms:>2000',
        'event:"sandbox_spawn" and source:"cold"': 'event:"sandbox_spawn" AND source:"cold"',
        'event:"sandbox_spawn" and source:"warm"': 'event:"sandbox_spawn" AND source:"warm"',
        'event:"queue_wait" and queue_ms > 0': 'event:"queue_wait" AND queue_ms:>0',
        'event:"limit_hit"': 'event:"limit_hit"',
        'event:"ws_detach"': 'event:"ws_detach"',
        'event:"sandbox_reaped" and reason:"user_exit"': 'event:"sandbox_reaped" AND reason:"user_exit"',
        'level:"error"': 'level:"error"',
        'event:"http_request" and status_code >= 500': 'event:"http_request" AND status_code:>=500',
    }
    print(f"=== stage {label} {start} -> {end} (window {w}) ===")
    print("Prometheus (instant query at window end):")
    for k, v in rows.items():
        print(f"  {k:<30} {'n/a' if v is None else f'{v:.3f}'}")
    print("Elasticsearch document counts (Kibana KQL equivalents):")
    for k, q in kql.items():
        print(f"  {k:<50} {es_count(q, start, end)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
