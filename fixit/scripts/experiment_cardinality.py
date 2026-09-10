#!/usr/bin/env python3
"""Part E.2 - cardinality explosion, capped at 100 unique run_ids. Stdlib only.

    python scripts/experiment_cardinality.py

1. Restart the agent with FIXIT_DEMO_CARDINALITY=1 (fixit_demo_requests_total gets a run_id
   label), run 100 tasks, wait for a scrape, query Prometheus:
       count(fixit_demo_requests_total)            -> expect 100
       prometheus_tsdb_head_series                 -> before/after
2. Restart with the flag off (same metric, no label), run 20 tasks, wait, query again:
       count(fixit_demo_requests_total)            -> 1 live series (old ones are stale-marked)
       count(last_over_time(fixit_demo_requests_total[15m])) -> still 101: history is retained
       /api/v1/series                              -> lists all 101 until retention/compaction
Never exceeds 100 labelled series.
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
AGENT = os.environ.get("FIXIT_URL", "http://localhost:8000")
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


def restart_agent(flag: str) -> None:
    print(f"\n==> docker compose up -d agent with FIXIT_DEMO_CARDINALITY={flag}")
    env = {**os.environ, "FIXIT_DEMO_CARDINALITY": flag}
    subprocess.run(["docker", "compose", "up", "-d", "agent"], cwd=ROOT, env=env, check=True, capture_output=True)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{AGENT}/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    else:
        sys.exit("agent did not become healthy")
    time.sleep(SCRAPE_WAIT_S)


def load(n: int) -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "load.py"), "--tasks", str(n), "--concurrency", "4"],
                   cwd=ROOT, check=False, stdout=subprocess.DEVNULL)


def snapshot(label: str) -> dict:
    time.sleep(SCRAPE_WAIT_S)
    snap = {
        "count(fixit_demo_requests_total)": prom("count(fixit_demo_requests_total)"),
        "count(last_over_time(fixit_demo_requests_total[15m]))": prom("count(last_over_time(fixit_demo_requests_total[15m]))"),
        "series API fixit_demo_requests_total (30m)": series_count("fixit_demo_requests_total"),
        "prometheus_tsdb_head_series": prom("prometheus_tsdb_head_series"),
        'count({__name__=~"fixit_.*"})': prom('count({__name__=~"fixit_.*"})'),
    }
    print(f"\n--- {label} ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}) ---")
    for k, v in snap.items():
        print(f"  {k:<58} {v:,.0f}")
    return snap


def main() -> int:
    results = {}
    results["0_before"] = snapshot("before: flag off, nothing run yet")

    restart_agent("1")
    results["1_flag_on_idle"] = snapshot("flag on, agent restarted, no tasks yet")
    print("==> running 100 tasks (100 distinct run_ids)")
    load(100)
    results["2_flag_on_100_tasks"] = snapshot("flag on, after 100 tasks")

    restart_agent("0")
    print("==> running 20 tasks with the label removed")
    load(20)
    results["3_flag_off_20_tasks"] = snapshot("flag off, after 20 tasks")

    out = ROOT / "scripts" / "results"; out.mkdir(exist_ok=True)
    f = out / f"cardinality_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    f.write_text(json.dumps(results, indent=1))
    print(f"\nresults written: {f}")
    print("Reminder: the labelled series stay on disk until the 7d retention deletes them; count() drops to 1 "
          "only because Prometheus writes a staleness marker when a series vanishes from the scrape.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
