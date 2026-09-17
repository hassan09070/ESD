#!/usr/bin/env python3
"""Load generator: N concurrent "customers" placing orders at random stalls. Stdlib only.

    python3 scripts/load.py --duration 120 --concurrency 4 --label baseline

Each customer loop: POST /orders -> sleep (prep) -> POST ready -> sleep (walk to counter)
-> POST pickup; one in ten orders is cancelled instead. Every HTTP call's latency is
measured client-side so the report can compare it with the server-side histogram.
Results are printed and, with --json, written to scripts/results/load_<start>_<label>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

STALLS = ("chai", "biryani", "shawarma", "juice")
ITEMS = {"chai": ["karak", "doodh patti"], "biryani": ["chicken", "beef"], "shawarma": ["chicken", "zinger"], "juice": ["mango", "apple"]}
RESULTS = Path(__file__).resolve().parent / "results"


def http(method: str, url: str, body: dict | None = None, rid: str | None = None) -> tuple[int, dict, float]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if rid:
        headers["X-Request-ID"] = rid
    req = request.Request(url, method=method, data=data, headers=headers)
    t0 = time.perf_counter()
    try:
        with request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), time.perf_counter() - t0
    except error.HTTPError as e:
        return e.code, {}, time.perf_counter() - t0


def customer(base: str, deadline: float, rows: list, lock: threading.Lock, prep: float, seed: int) -> None:
    rng = random.Random(seed)
    while time.perf_counter() < deadline:
        stall = rng.choice(STALLS)
        row = {"stall": stall, "calls": [], "outcome": "ok"}
        status, order, dt = http("POST", f"{base}/orders", {"stall": stall, "item": rng.choice(ITEMS[stall])})
        row["calls"].append(("POST /orders", status, dt))
        if status != 201:
            row["outcome"] = f"place_{status}"
        else:
            oid = order["order_id"]
            if rng.random() < 0.1:
                time.sleep(rng.uniform(0.1, 0.5))
                status, _, dt = http("POST", f"{base}/orders/{oid}/cancel")
                row["calls"].append(("POST /orders/{id}/cancel", status, dt))
                row["outcome"] = "cancelled"
            else:
                time.sleep(rng.uniform(0.5, prep))                       # the stall cooks
                status, _, dt = http("POST", f"{base}/orders/{oid}/ready")
                row["calls"].append(("POST /orders/{id}/ready", status, dt))
                time.sleep(rng.uniform(0.2, 1.0))                        # customer walks over
                status, _, dt = http("POST", f"{base}/orders/{oid}/pickup")
                row["calls"].append(("POST /orders/{id}/pickup", status, dt))
                if status != 200:
                    row["outcome"] = f"pickup_{status}"
        with lock:
            rows.append(row)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=60, help="seconds to keep ordering")
    ap.add_argument("--concurrency", type=int, default=4, help="customers in flight at once")
    ap.add_argument("--prep", type=float, default=3.0, help="max seconds a stall takes to cook")
    ap.add_argument("--url", default=os.environ.get("CANTEEN_URL", "http://localhost:8000"))
    ap.add_argument("--json", action="store_true", help="write scripts/results/load_<start>_<label>.json")
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    start = datetime.now(timezone.utc)
    rows: list[dict] = []
    lock = threading.Lock()
    deadline = time.perf_counter() + args.duration
    print(f"[{start:%H:%M:%S}Z] load: concurrency={args.concurrency} duration={args.duration}s -> {args.url}", flush=True)
    threads = [threading.Thread(target=customer, args=(args.url, deadline, rows, lock, args.prep, i)) for i in range(args.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    end = datetime.now(timezone.utc)

    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    by_route: dict[str, list[float]] = {}
    statuses: dict[int, int] = {}
    for r in rows:
        for route, status, dt in r["calls"]:
            by_route.setdefault(route, []).append(dt * 1000)
            statuses[status] = statuses.get(status, 0) + 1
    print(f"\n== {args.label}: {len(rows)} orders {start:%Y-%m-%dT%H:%M:%S}Z -> {end:%H:%M:%S}Z ({(end - start).total_seconds():.0f}s) ==")
    print(f"outcomes: {outcomes}   http statuses: {statuses}")
    for route, v in by_route.items():
        print(f"{route:26s} n={len(v):4d} mean={statistics.mean(v):6.1f}ms p50={pct(v, 50):6.1f}ms p95={pct(v, 95):6.1f}ms max={max(v):6.1f}ms")
    if args.json:
        RESULTS.mkdir(exist_ok=True)
        out = RESULTS / f"load_{start:%Y-%m-%dT%H%M%S}Z_{args.label}.json"
        out.write_text(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "label": args.label, "args": vars(args), "rows": rows}, indent=1))
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
