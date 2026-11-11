#!/usr/bin/env python3
"""A3: the fixed-rate, open-loop test client for one interface.

    python3 scripts/fixed_rate.py http --rate 40 --duration 60 --label c_rr
    uv run python scripts/fixed_rate.py grpc --rate 40 --duration 60 --label c_rr      (needs grpcio: uv run)

"Open loop" means the client sends request number n at exactly start + n/rate, whether or not
earlier requests have been answered; a slow or dead replica therefore shows up as more requests
in flight and a higher p95, not as a lower send rate (a closed loop would hide the slowness by
sending less). HTTP requests each run in their own thread (stdlib urllib); gRPC calls are
issued as futures on ONE channel to Envoy, as the brief describes: the client keeps a single
HTTP/2 connection and Envoy routes every call on it separately.

Test traffic (one operation per interface, both from A2):
    http    GET /shops                 read-only; no rate limit; one SQLite read per call
    grpc    OrderService/PlaceOrder    the A2 unary RPC (a write: every call creates one order in the shared store)

Every request carries its own x-request-id and a deadline (--timeout). No retries, no circuit
breaker (Part D asks for both to be off). The client also records which replica answered
(X-Replica header / x-replica metadata), so its own view of the traffic shares can be checked
against the dashboard and against Envoy's access log.

Output: a summary line, then a JSON file scripts/results/a3_<label>_<iface>.json with the
start/end UTC window (paste into Grafana), rate, sent, successes, failures by status,
p50/p95/max client latency (ms) and the per-replica counts.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib import error, request

RESULTS = pathlib.Path(__file__).resolve().parent / "results"
SHOPS = ("sky_dhaba", "tapal", "cafetogo", "grito")


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows: list[tuple[float, str, str]] = []     # (latency_s, outcome, replica)

    def add(self, latency: float, outcome: str, replica: str) -> None:
        with self.lock:
            self.rows.append((latency, outcome, replica))


# ----------------------------------------------------------------------------- HTTP
def http_one(url: str, timeout: float, rec: Recorder) -> None:
    rid = uuid.uuid4().hex[:12]
    req = request.Request(url, headers={"X-Request-ID": rid})
    t0 = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            rec.add(time.perf_counter() - t0, str(resp.status), resp.headers.get("X-Replica", "?"))
    except error.HTTPError as e:
        rec.add(time.perf_counter() - t0, str(e.code), e.headers.get("X-Replica", "?") if e.headers else "?")
    except (error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        kind = "timeout" if isinstance(reason, TimeoutError) or "timed out" in str(reason) else "conn_error"
        rec.add(time.perf_counter() - t0, kind, "-")


def run_http(args, rec: Recorder) -> None:
    url = f"{args.url}/shops"
    pool = ThreadPoolExecutor(max_workers=max(64, int(args.rate * args.timeout * 2)))
    schedule(args, lambda: pool.submit(http_one, url, args.timeout, rec))
    pool.shutdown(wait=True)


# ----------------------------------------------------------------------------- gRPC
def run_grpc(args, rec: Recorder) -> None:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import grpc
    from grpc_service import canteen_pb2, canteen_pb2_grpc

    pending: set = set()
    plock = threading.Lock()
    rng = random.Random(1)

    def done(fut, t0, call):
        latency = time.perf_counter() - t0
        try:
            fut.result()
            md = dict(call.initial_metadata())
            rec.add(latency, "OK", md.get("x-replica", "?"))
        except grpc.RpcError as e:
            md = {}
            try:
                md = dict(e.initial_metadata() or ())
            except Exception:  # noqa: BLE001
                pass
            rec.add(latency, e.code().name, md.get("x-replica", "-"))
        with plock:
            pending.discard(fut)

    with grpc.insecure_channel(args.grpc) as channel:          # ONE connection to Envoy for the whole run
        stub = canteen_pb2_grpc.OrderServiceStub(channel)
        grpc.channel_ready_future(channel).result(timeout=5)

        def fire():
            shop = rng.choice(SHOPS)
            req = canteen_pb2.PlaceOrderRequest(shop=shop, item="load-test")
            rid = uuid.uuid4().hex[:12]
            t0 = time.perf_counter()
            fut = stub.PlaceOrder.future(req, timeout=args.timeout, metadata=(("x-request-id", rid),))
            with plock:
                pending.add(fut)
            fut.add_done_callback(lambda f, t0=t0, call=fut: done(f, t0, call))

        schedule(args, fire)
        deadline = time.time() + args.timeout + 1
        while time.time() < deadline:
            with plock:
                if not pending:
                    break
            time.sleep(0.05)


# ----------------------------------------------------------------------------- scheduler
def schedule(args, fire) -> None:
    """Call fire() at exactly start + n/rate for n in [0, rate*duration). Never waits for a response."""
    total = int(round(args.rate * args.duration))
    interval = 1.0 / args.rate
    start = time.perf_counter()
    for n in range(total):
        target = start + n * interval
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        fire()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("interface", choices=("http", "grpc"))
    ap.add_argument("--rate", type=float, default=float(os.environ.get("RATE", "40")), help="requests per second to send")
    ap.add_argument("--duration", type=float, default=float(os.environ.get("DURATION", "60")), help="seconds to keep sending")
    ap.add_argument("--timeout", type=float, default=5.0, help="per-request deadline (s)")
    ap.add_argument("--url", default=os.environ.get("CANTEEN_URL", "http://localhost:8000"), help="Envoy's HTTP listener")
    ap.add_argument("--grpc", default=os.environ.get("CANTEEN_GRPC", "localhost:50051"), help="Envoy's gRPC listener")
    ap.add_argument("--label", default="run")
    ap.add_argument("--no-json", action="store_true")
    args = ap.parse_args()

    rec = Recorder()
    start = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    print(f"[{start:%H:%M:%S}Z] {args.interface}: {args.rate:g} req/s for {args.duration:g} s, open loop, timeout {args.timeout:g} s "
          f"-> {args.url if args.interface == 'http' else args.grpc}", flush=True)
    (run_http if args.interface == "http" else run_grpc)(args, rec)
    end = datetime.now(timezone.utc)
    wall = time.perf_counter() - t0

    rows = rec.rows
    sent = int(round(args.rate * args.duration))
    ok_set = {"200", "201", "OK"}
    successes = sum(1 for _, o, _ in rows if o in ok_set)
    by_outcome: dict[str, int] = {}
    by_replica: dict[str, int] = {}
    for _, o, r in rows:
        by_outcome[o] = by_outcome.get(o, 0) + 1
        by_replica[r] = by_replica.get(r, 0) + 1
    lat = [l * 1000 for l, _, _ in rows]
    ok_lat = [l * 1000 for l, o, _ in rows if o in ok_set]
    summary = {
        "label": args.label, "interface": args.interface, "operation": "GET /shops" if args.interface == "http" else "OrderService/PlaceOrder",
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_rate": args.rate, "duration_s": args.duration, "actual_rate": round(sent / args.duration, 2),
        "sent": sent, "answered": len(rows), "successes": successes, "failures": len(rows) - successes + (sent - len(rows)),
        "by_outcome": by_outcome, "by_replica": dict(sorted(by_replica.items())),
        "shares_pct": {r: round(100 * n / max(1, len(rows)), 1) for r, n in sorted(by_replica.items())},
        "p50_ms": round(pct(lat, 50) or 0, 1), "p95_ms": round(pct(lat, 95) or 0, 1), "max_ms": round(max(lat) if lat else 0, 1),
        "p95_ok_ms": round(pct(ok_lat, 95) or 0, 1), "timeout_s": args.timeout,
    }
    print(f"== {args.label}/{args.interface}: sent {sent} ({summary['actual_rate']} req/s over {wall:.1f} s) answered {len(rows)} "
          f"ok {successes} failed {summary['failures']} | p50 {summary['p50_ms']} ms p95 {summary['p95_ms']} ms max {summary['max_ms']} ms")
    print(f"   outcomes {by_outcome} | replicas {summary['by_replica']} -> shares {summary['shares_pct']}")
    print(f"   window {summary['start']} -> {summary['end']}")
    if not args.no_json:
        RESULTS.mkdir(exist_ok=True)
        out = RESULTS / f"a3_{args.label}_{args.interface}.json"
        out.write_text(json.dumps(summary, indent=1) + "\n")
        print(f"   wrote {out}")
    return 0 if summary["failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
