#!/usr/bin/env python3
"""Load generator: N concurrent "customers" placing orders at random shops. Stdlib only.

Two modes:

    python3 scripts/load.py --duration 120 --concurrency 4 --label baseline
        Fixed run, used by the experiments. Every customer orders back to back, seeded by
        its index, so two runs with the same flags make the same sequence of choices.
        Every HTTP call's latency is measured client-side and printed; with --json the raw
        rows go to scripts/results/load_<start>_<label>.json.

    python3 scripts/load.py --forever
        Continuous, realistic traffic (the `load` compose service runs this): a slow
        busy/quiet cycle, random rushes and lulls, random think time between orders, a
        cancel rate that drifts, and the odd customer mistake (pickup before ready -> 409).
        Unseeded, so no two minutes look alike. Prints one status line a minute. Survives
        the app being restarted (retries with backoff).

Each customer loop: POST /orders -> sleep (prep) -> POST ready -> sleep (walk to counter)
-> POST pickup; some orders are cancelled instead.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

SHOPS = ("sky_dhaba", "tapal", "cafetogo", "grito")
ITEMS = {"sky_dhaba": ["chai", "pharata"], "tapal": ["biryani", "pulao"], "cafetogo": ["burger", "roll"], "grito": ["corn", "ice cream"]}
RESULTS = Path(__file__).resolve().parent / "results"


def http(method: str, url: str, body: dict | None = None, rid: str | None = None) -> tuple[int, dict, float]:
    """Returns (status, json, seconds). status 0 = could not connect (app down / restarting)."""
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
    except (error.URLError, OSError):
        return 0, {}, time.perf_counter() - t0


def one_order(base: str, rng: random.Random, prep: float, cancel_p: float, mistake_p: float) -> dict:
    """Place one order and take it through its life. Returns a row with every call's latency."""
    shop = rng.choice(SHOPS)
    row = {"shop": shop, "calls": [], "outcome": "ok"}
    status, order, dt = http("POST", f"{base}/orders", {"shop": shop, "item": rng.choice(ITEMS[shop])})
    row["calls"].append(("POST /orders", status, dt))
    if status != 201:
        row["outcome"] = f"place_{status}"
        return row
    oid = order["order_id"]
    if rng.random() < cancel_p:
        time.sleep(rng.uniform(0.1, 0.5))
        status, _, dt = http("POST", f"{base}/orders/{oid}/cancel")
        row["calls"].append(("POST /orders/{id}/cancel", status, dt))
        row["outcome"] = "cancelled"
        return row
    if mistake_p and rng.random() < mistake_p:                       # impatient: pickup before it is ready -> 409
        status, _, dt = http("POST", f"{base}/orders/{oid}/pickup")
        row["calls"].append(("POST /orders/{id}/pickup", status, dt))
    time.sleep(rng.uniform(0.5, prep))                               # the shop cooks
    status, _, dt = http("POST", f"{base}/orders/{oid}/ready")
    row["calls"].append(("POST /orders/{id}/ready", status, dt))
    time.sleep(rng.uniform(0.2, 1.0))                                # customer walks over
    status, _, dt = http("POST", f"{base}/orders/{oid}/pickup")
    row["calls"].append(("POST /orders/{id}/pickup", status, dt))
    if status != 200:
        row["outcome"] = f"pickup_{status}"
    return row


# ------------------------------------------------------------------ fixed run (experiments)
def customer(base: str, deadline: float, rows: list, lock: threading.Lock, prep: float, seed: int) -> None:
    rng = random.Random(seed)
    while time.perf_counter() < deadline:
        row = one_order(base, rng, prep, cancel_p=0.1, mistake_p=0.0)
        with lock:
            rows.append(row)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def fixed_run(args: argparse.Namespace) -> int:
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


# ------------------------------------------------------------------ continuous run (the `load` service)
class Traffic:
    """Shared, slowly changing picture of how busy the campus is, 0 (dead) .. 1 (rush)."""

    def __init__(self, period_s: float, rng: random.Random):
        self.period_s = period_s
        self.rng = rng
        self.level = 0.5
        self.cancel_p = 0.1
        self.note = "steady"
        self.lock = threading.Lock()
        self.orders = 0
        self.cancelled = 0
        self.errors = 0
        self.t0 = time.time()

    def controller(self) -> None:
        """Every 30 s: follow a sine 'day' with noise, sometimes override with a rush or a lull."""
        override_until, override_level = 0.0, 0.0
        while True:
            now = time.time()
            if now < override_until:
                level = override_level
            else:
                r = self.rng.random()
                if r < 0.08:                                       # a lecture just ended
                    override_until, override_level, self.note = now + self.rng.uniform(60, 150), 1.0, "rush"
                elif r < 0.16:                                     # everyone is in class
                    override_until, override_level, self.note = now + self.rng.uniform(60, 150), 0.03, "lull"
                else:
                    self.note = "steady"
                if now < override_until:
                    level = override_level
                else:
                    day = 0.5 + 0.5 * math.sin(2 * math.pi * (now - self.t0) / self.period_s)
                    level = day * self.rng.uniform(0.6, 1.3)
            with self.lock:
                self.level = min(1.0, max(0.03, level))
                self.cancel_p = self.rng.uniform(0.04, 0.2)
            time.sleep(30)

    def think_time(self, rng: random.Random, max_think: float) -> float:
        """Seconds a customer waits before the next order: short when busy, long when quiet."""
        with self.lock:
            level = self.level
        mean = 0.5 + (1.0 - level) * max_think
        return min(rng.expovariate(1.0 / mean), 4 * max_think)

    def reporter(self) -> None:
        while True:
            time.sleep(60)
            with self.lock:
                o, c, e, lvl, note = self.orders, self.cancelled, self.errors, self.level, self.note
                self.orders = self.cancelled = self.errors = 0
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] load: level={lvl:.2f} ({note}) orders/min={o} cancelled={c} failed={e}", flush=True)


def real_customer(base: str, traffic: Traffic, prep: float, max_think: float, mistake_p: float) -> None:
    rng = random.Random()                                            # unseeded: real customers are not reproducible
    time.sleep(rng.uniform(0, 10))                                   # do not all arrive at once
    while True:
        with traffic.lock:
            cancel_p = traffic.cancel_p
        row = one_order(base, rng, rng.uniform(1.5, prep), cancel_p, mistake_p)
        with traffic.lock:
            traffic.orders += 1
            if row["outcome"] == "cancelled":
                traffic.cancelled += 1
            elif row["outcome"] != "ok":
                traffic.errors += 1
        if row["outcome"] == "place_0":                              # app unreachable (restarting?): back off
            time.sleep(rng.uniform(2, 5))
            continue
        time.sleep(traffic.think_time(rng, max_think))


def forever_run(args: argparse.Namespace) -> int:
    traffic = Traffic(args.period * 60, random.Random())
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] load: continuous, customers={args.concurrency} period={args.period}min -> {args.url}", flush=True)
    threading.Thread(target=traffic.controller, daemon=True).start()
    threading.Thread(target=traffic.reporter, daemon=True).start()
    threads = [threading.Thread(target=real_customer, args=(args.url, traffic, args.prep, args.think, args.mistakes), daemon=True) for _ in range(args.concurrency)]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=60, help="seconds to keep ordering (fixed mode)")
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("LOAD_CUSTOMERS", "4")), help="customers in flight at once")
    ap.add_argument("--prep", type=float, default=3.0, help="max seconds a shop takes to cook")
    ap.add_argument("--url", default=os.environ.get("CANTEEN_URL", "http://localhost:8000"))
    ap.add_argument("--json", action="store_true", help="fixed mode: write scripts/results/load_<start>_<label>.json")
    ap.add_argument("--label", default="run")
    ap.add_argument("--forever", action="store_true", help="continuous realistic traffic until stopped")
    ap.add_argument("--period", type=float, default=float(os.environ.get("LOAD_PERIOD_MIN", "20")), help="forever mode: minutes per busy/quiet cycle")
    ap.add_argument("--think", type=float, default=float(os.environ.get("LOAD_MAX_THINK_S", "25")), help="forever mode: mean seconds between a customer's orders when it is dead quiet")
    ap.add_argument("--mistakes", type=float, default=0.03, help="forever mode: share of orders where the customer tries to pick up before it is ready (409)")
    args = ap.parse_args()
    return forever_run(args) if args.forever else fixed_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
