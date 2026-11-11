#!/usr/bin/env python3
"""A3: the results table, from the windows scripts/experiment_lb.sh recorded. Stdlib only.

    python3 scripts/lb_summary.py                 # every run in scripts/results/a3_runs.tsv
    python3 scripts/lb_summary.py c_rr d_lr_slow  # only these

For each run and interface:
  - per replica, from Prometheus (scraped directly from A, B, C): requests handled in the
    window (increase over the window, evaluated at its end), share of the total, mean active
    requests, p95 over the whole window;
  - from the client's JSON (scripts/results/a3_<run>_<iface>.json): target/actual rate, sent,
    successes, failures, client p95, and the client's own per-replica counts;
  - from Elasticsearch: Envoy access-log events in the same window grouped by `replica`, the
    cross-check the brief asks for (an access-log line per request, written by the proxy).
Prints one Markdown table for the report and the detail per run.
"""
from __future__ import annotations

import csv
import json
import pathlib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROM = "http://localhost:9090"
ES = "http://localhost:9200"
RESULTS = pathlib.Path(__file__).resolve().parent / "results"
REPLICAS = ("A", "B", "C")


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def prom_vec(query: str, at: float) -> dict[str, float]:
    q = urllib.parse.urlencode({"query": query, "time": at})
    with urllib.request.urlopen(f"{PROM}/api/v1/query?{q}", timeout=15) as r:
        res = json.load(r)["data"]["result"]
    out = {}
    for item in res:
        v = float(item["value"][1])
        out[item["metric"].get("replica", "")] = 0.0 if v != v else v
    return out


def es_by_replica(listener: str, start: str, end: str) -> dict[str, int]:
    body = {"size": 0, "query": {"bool": {"filter": [
        {"term": {"event": "envoy_access"}}, {"term": {"listener": listener}},
        {"bool": {"must_not": {"term": {"path": "/health"}}}},
        {"range": {"@timestamp": {"gte": start, "lte": end}}}]}},
        "aggs": {"r": {"terms": {"field": "replica", "missing": "-", "size": 10}}}}
    req = urllib.request.Request(f"{ES}/canteen-logs-*/_search", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            buckets = json.load(r)["aggregations"]["r"]["buckets"]
    except Exception as e:  # noqa: BLE001
        print(f"  (elasticsearch unavailable: {e})")
        return {}
    return {b["key"]: b["doc_count"] for b in buckets}


def fmt_shares(counts: dict[str, float]) -> str:
    total = sum(counts.get(r, 0) for r in REPLICAS)
    if total <= 0:
        return "-"
    return " / ".join(f"{100 * counts.get(r, 0) / total:.0f}%" for r in REPLICAS)


def summarise(row: dict) -> dict | None:
    run, iface, start, end = row["run"], row["interface"], row["start"], row["end"]
    at, w = ts(end), f"{max(30, int(ts(end) - ts(start)))}s"
    if iface == "http":
        total = f'canteen_http_requests_total{{route!~"/health|/metrics"}}'
        bucket = f'canteen_http_request_duration_seconds_bucket{{route!~"/health|/metrics"}}'
        active, err = "canteen_http_in_flight", f'canteen_http_requests_total{{route!~"/health|/metrics",status!~"2.."}}'
    else:
        total = "canteen_grpc_server_handled_total"
        bucket = "canteen_grpc_server_handling_seconds_bucket"
        active, err = "canteen_grpc_server_in_flight", 'canteen_grpc_server_handled_total{grpc_code!="OK"}'
    handled = prom_vec(f"sum by (replica) (increase({total}[{w}]))", at)
    errors = prom_vec(f"sum by (replica) (increase({err}[{w}]))", at)
    act = prom_vec(f"avg by (replica) (avg_over_time({active}[{w}]))", at)
    p95 = prom_vec(f"histogram_quantile(0.95, sum by (replica, le) (rate({bucket}[{w}])))", at)
    p50 = prom_vec(f"histogram_quantile(0.50, sum by (replica, le) (rate({bucket}[{w}])))", at)
    envoy = es_by_replica(iface, start, end)
    client_file = RESULTS / f"a3_{run}_{iface}.json"
    client = json.loads(client_file.read_text()) if client_file.exists() else {}
    return {"row": row, "handled": handled, "errors": errors, "active": act, "p95": p95, "p50": p50, "envoy": envoy, "client": client}


def main() -> int:
    runs_file = RESULTS / "a3_runs.tsv"
    if not runs_file.exists():
        print(f"no {runs_file}; run scripts/experiment_lb.sh first")
        return 1
    rows = list(csv.DictReader(runs_file.open(), delimiter="\t"))
    wanted = set(sys.argv[1:])
    rows = [r for r in rows if not wanted or r["run"] in wanted]
    # keep the last window per (run, interface) if a run was repeated
    latest = {}
    for r in rows:
        latest[(r["run"], r["interface"])] = r
    results = [summarise(r) for r in latest.values()]

    print("| Run / interface | Policy and fault | Target / actual req/s | A/B/C shares (replica metrics) | A/B/C shares (Envoy log) | Client p95 | Error rate |")
    print("|---|---|---|---|---|---|---|")
    for s in results:
        r, c = s["row"], s["client"]
        total_handled = sum(s["handled"].values()) or 1
        err_rate = 100 * (c.get("failures", 0) / max(1, c.get("sent", 1))) if c else float("nan")
        print(f"| {r['run']} / {r['interface']} | {r['mode']}, {r['fault']} | {r['rate']} / {c.get('actual_rate', '-')} "
              f"| {fmt_shares(s['handled'])} | {fmt_shares(s['envoy']) if s['envoy'] else '-'} "
              f"| {c.get('p95_ms', '-')} ms | {err_rate:.1f}% ({c.get('failures', '-')} of {c.get('sent', '-')}) |")
    print()
    for s in results:
        r, c = s["row"], s["client"]
        print(f"=== {r['run']} / {r['interface']}  {r['start']} -> {r['end']}  mode={r['mode']} fault={r['fault']}")
        if c:
            print(f"  client: sent {c['sent']} answered {c['answered']} ok {c['successes']} failed {c['failures']} "
                  f"p50 {c['p50_ms']} ms p95 {c['p95_ms']} ms max {c['max_ms']} ms outcomes {c['by_outcome']} replicas {c['by_replica']}")
        print("  replica  handled   share   errors  active(avg)  p50 ms   p95 ms   envoy-log")
        for rep in REPLICAS:
            h = s["handled"].get(rep, 0)
            print(f"  {rep:7s} {h:8.0f}  {100 * h / (sum(s['handled'].values()) or 1):5.1f}%  {s['errors'].get(rep, 0):6.0f}  "
                  f"{s['active'].get(rep, 0):10.2f}  {1000 * s['p50'].get(rep, 0):7.1f}  {1000 * s['p95'].get(rep, 0):7.1f}   {s['envoy'].get(rep, 0):6d}")
        if s["envoy"].get("-"):
            print(f"  envoy-log events with no replica (no upstream answered, e.g. 503 UF/UH): {s['envoy']['-']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
