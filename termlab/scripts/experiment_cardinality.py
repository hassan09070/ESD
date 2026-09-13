#!/usr/bin/env python3
"""Part E.2 - cardinality explosion, capped at 100 unique request_ids. Stdlib only.

    python3 scripts/experiment_cardinality.py

1. Re-create the api with TERMLAB_DEMO_CARDINALITY=1 (termlab_demo_requests_total gets a
   request_id label), send 100 x POST /sessions each with its own X-Request-ID (no sandboxes
   are spawned - the counter is incremented per session created), wait for a scrape, query:
       count(termlab_demo_requests_total)            -> expect 100
       prometheus_tsdb_head_series                   -> before/after
2. Re-create with the flag off (same metric, no label), send 20 more, wait, query again:
       count(termlab_demo_requests_total)            -> 1 live series (old ones are stale-marked)
       count(last_over_time(termlab_demo_requests_total[15m])) -> still 101: history is retained
       /api/v1/series                                -> lists all 101 until retention/compaction
Never exceeds 100 labelled series. Re-creating the api reaps any running sandboxes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROM = os.environ.get("PROM_URL", "http://localhost:9090")
API = os.environ.get("TERMLAB_URL", "http://localhost:8000")
SCRAPE_WAIT_S = 12  # > 2 scrape intervals


def prom(query: str):
    q = urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(f"{PROM}/api/v1/query?{q}", timeout=10) as r:
        res = json.load(r)["data"]["result"]
    return float(res[0]["value"][1]) if res else 0.0


def series_count(match: str, minutes: int = 30) -> int:
    now = time.time()
    q = urllib.parse.urlencode({"match[]": match, "start": now - minutes * 60, "end": now})
    with urllib.request.urlopen(f"{PROM}/api/v1/series?{q}", timeout=10) as r:
        return len(json.load(r)["data"])


def restart_api(flag: str) -> None:
    print(f"\n==> docker compose up -d api with TERMLAB_DEMO_CARDINALITY={flag}")
    env = {**os.environ, "TERMLAB_DEMO_CARDINALITY": flag}
    subprocess.run(["docker", "compose", "up", "-d", "api"], cwd=ROOT, env=env, check=True, capture_output=True)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{API}/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    else:
        sys.exit("api did not become healthy")
    time.sleep(SCRAPE_WAIT_S)


def create_sessions(n: int, prefix: str) -> None:
    for i in range(n):
        req = urllib.request.Request(f"{API}/sessions", method="POST", headers={"X-Request-ID": f"{prefix}-{i:03d}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 201


def snapshot(label: str) -> dict:
    time.sleep(SCRAPE_WAIT_S)
    snap = {
        "count(termlab_demo_requests_total)": prom("count(termlab_demo_requests_total)"),
        "count(last_over_time(termlab_demo_requests_total[15m]))": prom("count(last_over_time(termlab_demo_requests_total[15m]))"),
        "series API termlab_demo_requests_total (30m)": series_count("termlab_demo_requests_total"),
        "prometheus_tsdb_head_series": prom("prometheus_tsdb_head_series"),
        'count({__name__=~"termlab_.*"})': prom('count({__name__=~"termlab_.*"})'),
    }
    print(f"\n--- {label} ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}) ---")
    for k, v in snap.items():
        print(f"  {k:<60} {v:,.0f}")
    return snap


def main() -> int:
    results = {}
    results["0_before"] = snapshot("before: flag off, nothing run yet")

    restart_api("1")
    results["1_flag_on_idle"] = snapshot("flag on, api re-created, no requests yet")
    print("==> creating 100 sessions with 100 distinct X-Request-IDs")
    create_sessions(100, "card")
    results["2_flag_on_100_requests"] = snapshot("flag on, after 100 requests")

    restart_api("0")
    print("==> creating 20 sessions with the label removed")
    create_sessions(20, "card-off")
    results["3_flag_off_20_requests"] = snapshot("flag off, after 20 requests")

    out = ROOT / "scripts" / "results"; out.mkdir(exist_ok=True)
    f = out / f"cardinality_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    f.write_text(json.dumps(results, indent=1))
    print(f"\nresults written: {f}")
    print("Reminder: the labelled series stay on disk until the 7d retention deletes them; count() drops to 1 "
          "only because Prometheus writes a staleness marker when a series vanishes from the scrape.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
