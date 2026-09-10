#!/usr/bin/env python3
"""Load generator: run N agent tasks against the server (stdlib only, no deps).

    python scripts/load.py --tasks 20 --concurrency 1 [--no-reset] [--url http://localhost:8000]

For each task: (optionally) run sample_repo/reset.sh, POST /tasks with an X-Request-ID
header, record outcome + duration. Prints a summary table (success count, mean/p95 duration)
and, with --json, writes the raw rows to scripts/results/<timestamp>.json.
Meant to be run with FIXIT_LLM=mock so results are free and repeatable.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
RESET = ROOT / "sample_repo" / "reset.sh"
_reset_lock = threading.Lock()


def utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_task(i: int, url: str, task: str, reset: bool) -> dict:
    if reset:
        with _reset_lock:
            subprocess.run(["bash", str(RESET)], check=True, capture_output=True)
    request_id = uuid4().hex[:12]
    body = json.dumps({"task": task, "repo": "/workspace"}).encode()
    req = urllib.request.Request(f"{url}/tasks", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Request-ID": request_id})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            status, payload = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {}
    except Exception as e:  # noqa: BLE001
        status, payload = 0, {"error": str(e)}
    dur = time.perf_counter() - t0
    row = {
        "i": i, "request_id": request_id, "http_status": status, "duration_s": round(dur, 3),
        "run_id": payload.get("run_id"), "outcome": payload.get("outcome", "http_error"),
        "iterations": payload.get("iterations"), "cost_usd": payload.get("cost_usd"),
    }
    print(f"[{i:3d}] {row['outcome']:<9} http={status} iter={row['iterations']} {dur:6.2f}s run_id={row['run_id']} req={request_id}", flush=True)
    return row


def p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * len(xs) + 0.5)) - 1)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--url", default=os.environ.get("FIXIT_URL", "http://localhost:8000"))
    ap.add_argument("--task", default="make the failing tests pass")
    ap.add_argument("--reset", dest="reset", action="store_true", default=True)
    ap.add_argument("--no-reset", dest="reset", action="store_false")
    ap.add_argument("--json", action="store_true", help="write rows to scripts/results/")
    ap.add_argument("--label", default="", help="free-text label stored with --json results")
    args = ap.parse_args()

    start_ts = utc()
    t0 = time.perf_counter()
    print(f"load: {args.tasks} tasks, concurrency {args.concurrency}, url {args.url}, start {start_ts}")
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        rows = list(ex.map(lambda i: one_task(i, args.url, args.task, args.reset), range(1, args.tasks + 1)))
    end_ts = utc()
    wall = time.perf_counter() - t0
    if args.reset:
        subprocess.run(["bash", str(RESET)], check=True, capture_output=True)

    durs = [r["duration_s"] for r in rows]
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    ok = by_outcome.get("success", 0)
    print("\n=== summary ===")
    print(f"start (UTC) : {start_ts}")
    print(f"end   (UTC) : {end_ts}   wall {wall:.1f}s")
    print(f"tasks       : {len(rows)}   success {ok}   " + "  ".join(f"{k} {v}" for k, v in sorted(by_outcome.items()) if k != "success"))
    print(f"duration    : mean {statistics.mean(durs):.2f}s   p95 {p95(durs):.2f}s   max {max(durs):.2f}s")
    print(f"http errors : {sum(1 for r in rows if r['http_status'] >= 500 or r['http_status'] == 0)}")
    if args.json:
        out = ROOT / "scripts" / "results"
        out.mkdir(exist_ok=True)
        f = out / f"load_{start_ts.replace(':', '')}{('_' + args.label) if args.label else ''}.json"
        f.write_text(json.dumps({"start": start_ts, "end": end_ts, "label": args.label, "args": vars(args), "rows": rows}, indent=1))
        print(f"rows written: {f}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
