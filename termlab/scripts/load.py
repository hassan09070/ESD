#!/usr/bin/env python3
"""Load generator: N virtual users, each opening a real WebSocket terminal.

    uv run scripts/load.py --users 20 --concurrency 4 --min-duration 60 --json --label smoke

Each virtual user: POST /sessions -> POST /sessions/{id}/sandbox (queue + spawn measured
server-side and returned) -> WebSocket attach -> `--commands` shell commands with a
client-side keystroke->output roundtrip measured on each -> `exit` (which destroys the
sandbox). --min-duration keeps launching users until the stage has lasted that long, so an
experiment stage covers several Prometheus scrapes. Results go to scripts/results/ as JSON.
Only stdlib + the `websockets` package (a dev dependency: run with `uv run`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

import websockets

RESULTS = Path(__file__).resolve().parent / "results"
COMMANDS = [
    (b"echo $((40+2))\r", b"42"),
    (b"ls -la / | tail -n +2 | wc -l\r", b"\r\n"),
    (b"python3 -c 'print(sum(range(10**5)))'\r", b"4999950000"),
    (b"stress-ng --cpu 1 --timeout 1s >/dev/null 2>&1; echo cpu_$((1+1))\r", b"cpu_2"),
    (b"head -c 200000 /dev/urandom | base64 | wc -c\r", b"\r\n"),
]


def http(method: str, url: str, token: str | None = None, rid: str | None = None) -> tuple[int, dict]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if rid:
        headers["X-Request-ID"] = rid
    req = request.Request(url, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


async def one_user(i: int, base: str, ws_base: str, n_commands: int, think: float, session_seconds: float) -> dict:
    rid = f"load-{os.getpid()}-{i}"
    row = {"i": i, "request_id": rid, "http_status": None, "outcome": "error", "source": None, "queue_ms": None,
           "spawn_ms": None, "rtt_ms": [], "duration_s": 0.0}
    t_start = time.perf_counter()
    status, s = http("POST", f"{base}/sessions", rid=rid)
    if status != 201:
        row.update(http_status=status, outcome="session_error")
        return row
    row["session_id"] = s["session_id"]
    status, r = http("POST", f"{base}/sessions/{s['session_id']}/sandbox", token=s["token"], rid=rid + "-sb")
    row["http_status"] = status
    if status != 200:
        row["outcome"] = r.get("error", f"http_{status}")
        row["queue_ms"] = r.get("waited_ms")
        row["duration_s"] = round(time.perf_counter() - t_start, 3)
        return row
    row.update(source=r["source"], queue_ms=r["queue_ms"], spawn_ms=r["spawn_ms"])
    buf = b""
    try:
        async with websockets.connect(f"{ws_base}/ws/{s['session_id']}?token={s['token']}&cols=120&rows=40", max_size=None, open_timeout=30) as ws:
            async def until(needle: bytes, timeout: float = 30.0) -> None:
                nonlocal buf
                deadline = time.perf_counter() + timeout
                while needle not in buf:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise TimeoutError(f"waiting for {needle!r}")
                    m = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    buf += m if isinstance(m, bytes) else m.encode()

            await until(b"$ ")
            t_session = time.perf_counter()
            k = 0
            while k < n_commands or (session_seconds and time.perf_counter() - t_session < session_seconds):
                cmd, needle = COMMANDS[k % len(COMMANDS)]
                buf = b""
                t0 = time.perf_counter()
                await ws.send(cmd)
                await until(cmd.strip())          # the PTY echo: keystroke -> first output = roundtrip
                row["rtt_ms"].append(round((time.perf_counter() - t0) * 1000, 2))
                await until(needle)
                await until(b"$ ")
                k += 1
                if think:
                    await asyncio.sleep(think)
            buf = b""
            await ws.send(b"exit\r")
            await until(b'"exit"', timeout=15)
        row["outcome"] = "ok"
    except Exception as e:  # noqa: BLE001
        row["outcome"] = "ws_error"
        row["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        http("DELETE", f"{base}/sessions/{s['session_id']}", token=s["token"])
    row["duration_s"] = round(time.perf_counter() - t_start, 3)
    return row


def run_user(i: int, args) -> dict:
    ws_base = args.url.replace("http://", "ws://").replace("https://", "wss://")
    return asyncio.run(one_user(i, args.url, ws_base, args.commands, args.think, args.session_seconds))


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=10, help="virtual users to run (at least)")
    ap.add_argument("--concurrency", type=int, default=2, help="users in flight at once")
    ap.add_argument("--min-duration", type=float, default=0, help="keep launching users until this many seconds have elapsed")
    ap.add_argument("--commands", type=int, default=len(COMMANDS), help="commands per user before exit")
    ap.add_argument("--session-seconds", type=float, default=0, help="keep typing commands until the session is this old (0 = just --commands)")
    ap.add_argument("--think", type=float, default=0.5, help="pause between commands (s)")
    ap.add_argument("--url", default=os.environ.get("TERMLAB_URL", "http://localhost:8000"))
    ap.add_argument("--json", action="store_true", help="write scripts/results/load_<start>_<label>.json")
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    start = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    rows: list[dict] = []
    lock = threading.Lock()
    print(f"[{start:%H:%M:%S}Z] load: users>={args.users} concurrency={args.concurrency} min_duration={args.min_duration}s "
          f"commands={args.commands} session_seconds={args.session_seconds} -> {args.url}", flush=True)
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        pending = set()
        launched = 0

        def more() -> bool:
            return launched < args.users or (time.perf_counter() - t0) < args.min_duration

        while more() or pending:
            while more() and len(pending) < args.concurrency:
                pending.add(ex.submit(run_user, launched, args))
                launched += 1
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                row = f.result()
                with lock:
                    rows.append(row)
                rtt = f"rtt p50={pct(row['rtt_ms'], 50)}ms" if row["rtt_ms"] else ""
                print(f"  user {row['i']:3d} {row['outcome']:15s} {row.get('source') or '-':4s} queue={row['queue_ms']} spawn={row['spawn_ms']} {rtt} {row.get('error', '')}", flush=True)
    end = datetime.now(timezone.utc)

    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    spawn = {src: [r["spawn_ms"] for r in rows if r["source"] == src] for src in ("warm", "cold")}
    queue = [r["queue_ms"] for r in rows if r["queue_ms"] is not None]
    rtts = [x for r in rows for x in r["rtt_ms"]]
    print(f"\n== {args.label}: {len(rows)} users {start:%Y-%m-%dT%H:%M:%S}Z -> {end:%H:%M:%S}Z ({(end - start).total_seconds():.0f}s) ==")
    print(f"outcomes: {by_outcome}")
    for src, v in spawn.items():
        if v:
            print(f"spawn {src}: n={len(v)} mean={statistics.mean(v):.0f}ms p95={pct(v, 95)}ms max={max(v)}ms")
    if queue:
        print(f"queue: mean={statistics.mean(queue):.0f}ms p95={pct(queue, 95)}ms max={max(queue)}ms")
    if rtts:
        print(f"terminal roundtrip (client): n={len(rtts)} p50={pct(rtts, 50)}ms p95={pct(rtts, 95)}ms p99={pct(rtts, 99)}ms")
    if args.json:
        RESULTS.mkdir(exist_ok=True)
        out = RESULTS / f"load_{start:%Y-%m-%dT%H%M%S}Z_{args.label}.json"
        out.write_text(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "label": args.label, "args": vars(args), "rows": rows}, indent=1))
        print(f"wrote {out}")
    return 0 if by_outcome.get("ok", 0) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
