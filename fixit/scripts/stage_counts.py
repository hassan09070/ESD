#!/usr/bin/env python3
"""Summarise one experiment stage from Prometheus and Elasticsearch, given its UTC time range.

    python scripts/stage_counts.py 2026-09-10T06:00:00Z 2026-09-10T06:02:30Z [label]

Prints, for that window: LLM p50/p95/p99 latency, LLM error ratio, task p95 duration,
mean iterations, tasks by outcome (Prometheus, evaluated at the window end over the
window length) and the Kibana-style document counts (Elasticsearch). Used to fill the
[RESULT] cells in REPORT.md. Stdlib only.
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
    req = urllib.request.Request(f"{ES}/fixit-logs-*/_count", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["count"]


def main() -> int:
    start, end = sys.argv[1], sys.argv[2]
    label = sys.argv[3] if len(sys.argv) > 3 else ""
    at = ts(end)
    w = f"{max(60, int(ts(end) - ts(start)))}s"
    rows = {
        "LLM p50 (s)": prom(f"histogram_quantile(0.50, sum by (le) (rate(fixit_llm_request_duration_seconds_bucket[{w}])))", at),
        "LLM p95 (s)": prom(f"histogram_quantile(0.95, sum by (le) (rate(fixit_llm_request_duration_seconds_bucket[{w}])))", at),
        "LLM p99 (s)": prom(f"histogram_quantile(0.99, sum by (le) (rate(fixit_llm_request_duration_seconds_bucket[{w}])))", at),
        "LLM mean (s)": prom(f"sum(rate(fixit_llm_request_duration_seconds_sum[{w}])) / sum(rate(fixit_llm_request_duration_seconds_count[{w}]))", at),
        "LLM error ratio": prom(f"(sum(rate(fixit_llm_requests_total{{status='error'}}[{w}])) or vector(0)) / sum(rate(fixit_llm_requests_total[{w}]))", at),
        "LLM retries (count)": prom(f"sum(increase(fixit_llm_requests_total{{status='retry'}}[{w}])) or vector(0)", at),
        "LLM calls (count)": prom(f"sum(increase(fixit_llm_requests_total{{status='ok'}}[{w}]))", at),
        "task p95 (s)": prom(f"histogram_quantile(0.95, sum by (le) (rate(fixit_task_duration_seconds_bucket[{w}])))", at),
        "task mean (s)": prom(f"sum(rate(fixit_task_duration_seconds_sum[{w}])) / sum(rate(fixit_task_duration_seconds_count[{w}]))", at),
        "iterations mean": prom(f"sum(rate(fixit_task_iterations_sum[{w}])) / sum(rate(fixit_task_iterations_count[{w}]))", at),
        "tasks success": prom(f"sum(increase(fixit_tasks_total{{outcome='success'}}[{w}])) or vector(0)", at),
        "tasks error": prom(f"sum(increase(fixit_tasks_total{{outcome='error'}}[{w}])) or vector(0)", at),
        "HTTP p95 POST /tasks (s)": prom(f"histogram_quantile(0.95, sum by (le) (rate(fixit_http_request_duration_seconds_bucket{{path='/tasks',method='POST'}}[{w}])))", at),
        "HTTP 5xx (count)": prom(f"sum(increase(fixit_http_requests_total{{status=~'5..'}}[{w}])) or vector(0)", at),
        "cost USD/h": prom(f"sum(rate(fixit_llm_cost_usd_total[{w}])) * 3600", at),
    }
    kql = {
        'event:"llm_call" and duration_ms > 3000': 'event:"llm_call" AND duration_ms:>3000',
        'event:"llm_retry"': 'event:"llm_retry"',
        'level:"error"': 'level:"error"',
        'event:"task_finished" and outcome:"success"': 'event:"task_finished" AND outcome:"success"',
        'event:"task_finished" and outcome:"error"': 'event:"task_finished" AND outcome:"error"',
        'event:"http_request" and status_code >= 500': 'event:"http_request" AND status_code:>=500',
    }
    print(f"=== stage {label} {start} -> {end} (window {w}) ===")
    print("Prometheus (instant query at window end):")
    for k, v in rows.items():
        print(f"  {k:<28} {'n/a' if v is None else f'{v:.3f}'}")
    print("Elasticsearch document counts (Kibana KQL equivalents):")
    for k, q in kql.items():
        print(f"  {k:<48} {es_count(q, start, end)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
