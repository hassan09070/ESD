#!/usr/bin/env python3
"""Summarise one experiment stage from Prometheus and Elasticsearch, given its UTC window.

    python3 scripts/stage_counts.py 2026-09-17T08:00:00Z 2026-09-17T08:02:00Z baseline

Prometheus queries are evaluated at the window end over the window length (so `rate` and
`increase` cover exactly the stage); the Elasticsearch counts are the Kibana searches with
the same time filter. Used to fill the result columns in REPORT.md. Stdlib only.
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
    return None if v != v else v


def es_count(q: str, start: str, end: str) -> int:
    body = {"query": {"bool": {"must": [{"query_string": {"query": q}}],
                               "filter": [{"range": {"@timestamp": {"gte": start, "lte": end}}}]}}}
    req = urllib.request.Request(f"{ES}/canteen-logs-*/_count", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
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
        "HTTP p50 all routes (s)": prom(hq(0.50, "canteen_http_request_duration_seconds"), at),
        "HTTP p95 all routes (s)": prom(hq(0.95, "canteen_http_request_duration_seconds"), at),
        "HTTP p99 all routes (s)": prom(hq(0.99, "canteen_http_request_duration_seconds"), at),
        "HTTP mean (s)": prom(f"sum(rate(canteen_http_request_duration_seconds_sum[{w}])) / sum(rate(canteen_http_request_duration_seconds_count[{w}]))", at),
        "HTTP requests (count)": prom(f"sum(increase(canteen_http_requests_total[{w}]))", at),
        "HTTP 5xx (count)": prom(f"sum(increase(canteen_http_requests_total{{status=~'5..'}}[{w}])) or vector(0)", at),
        "orders placed (count)": prom(f"sum(increase(canteen_orders_placed_total[{w}]))", at),
        "orders placed biryani (count)": prom(f"sum(increase(canteen_orders_placed_total{{stall='biryani'}}[{w}]))", at),
        "orders cancelled (count)": prom(f"sum(increase(canteen_orders_cancelled_total[{w}]))", at),
        "orders waiting (avg gauge)": prom(f"avg_over_time(sum(canteen_orders_waiting)[{w}:5s])", at),
        "prep p95 all stalls (s)": prom(hq(0.95, "canteen_order_prep_seconds"), at),
        "prep mean, Summary (s)": prom(f"sum(rate(canteen_order_prep_summary_seconds_sum[{w}])) / sum(rate(canteen_order_prep_summary_seconds_count[{w}]))", at),
        "pickup delay p95 (s)": prom(hq(0.95, "canteen_pickup_delay_seconds"), at),
        "machine CPU busy % (avg)": prom(f"100 * (1 - avg(rate(node_cpu_seconds_total{{mode='idle'}}[{w}])))", at),
    }
    kql = {
        'event:"http_request" and duration_ms > 400': 'event:"http_request" AND duration_ms:>400',
        'event:"http_request" and status_code >= 500': 'event:"http_request" AND status_code:>=500',
        'event:"http_request"': 'event:"http_request"',
        'event:"order_placed"': 'event:"order_placed"',
        'event:"order_placed" and stall:"biryani"': 'event:"order_placed" AND stall:"biryani"',
        'level:"error"': 'level:"error"',
        'event:"chaos_changed"': 'event:"chaos_changed"',
    }
    print(f"=== stage {label} {start} -> {end} (window {w}) ===")
    print("Prometheus (instant query at window end):")
    for k, v in rows.items():
        print(f"  {k:<32} {'n/a' if v is None else f'{v:.3f}'}")
    print("Elasticsearch document counts (Kibana KQL equivalents):")
    for k, q in kql.items():
        print(f"  {k:<48} {es_count(q, start, end)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
