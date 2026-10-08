#!/usr/bin/env python3
"""A2 Part A: a Python caller of POST /orders that behaves like another service. Stdlib only.

    python3 scripts/caller.py                 # place 3 orders, one call each, and print the outcomes
    python3 scripts/caller.py --demo          # the reliability demo: success, retries on a server
                                              # failure, calls blocked while the breaker is open, recovery

Settings (also printed at start):
  TIMEOUT       2.0 s per HTTP attempt
  RETRIES       3 retries per call (4 attempts), exponential backoff 0.25 s, 0.5 s, 1.0 s
  BREAKER       opens after 3 consecutive failed calls, stays open 5 s, then lets one trial call
                through (half-open); a success closes it, a failure re-opens it

Retry only on transient failures: a timeout, a connection error or a 5xx. Never on a 4xx (the
request itself is wrong) and never on 429 (rate limiting is demonstrated separately). A retried
POST is safe because every logical order carries one Idempotency-Key for all its attempts: if an
earlier attempt did reach the server, the retry gets that same order back, not a second one.

The demo uses the app's chaos resource to make one shop answer 503 (PUT /chaos) and to recover
(DELETE /chaos); the three breaker phases are visible in the output.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib import error, request

URL = os.environ.get("CANTEEN_URL", "http://localhost:8000")
TIMEOUT_S = 2.0
RETRIES = 3
BACKOFF_S = 0.25                      # doubles each retry
BREAKER_FAILURES = 3
BREAKER_OPEN_S = 5.0


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


class CircuitBreaker:
    """CLOSED -> (failures >= threshold) -> OPEN -> (open period over) -> HALF_OPEN -> success: CLOSED / failure: OPEN."""

    def __init__(self, failures: int, open_s: float):
        self.threshold, self.open_s = failures, open_s
        self.state, self.failures, self.opened_at = "CLOSED", 0, 0.0

    def allow(self) -> bool:
        if self.state == "OPEN":
            if time.monotonic() - self.opened_at >= self.open_s:
                self.state = "HALF_OPEN"                      # one trial call may pass
                return True
            return False
        return True

    def record_success(self) -> None:
        if self.state != "CLOSED":
            print(f"[{now()}] breaker: trial call succeeded -> CLOSED")
        self.state, self.failures = "CLOSED", 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "HALF_OPEN" or self.failures >= self.threshold:
            self.state, self.opened_at = "OPEN", time.monotonic()
            print(f"[{now()}] breaker: {self.failures} consecutive failures -> OPEN for {self.open_s:.0f} s")


class TransientError(Exception):
    pass


def attempt(shop: str, item: str, key: str) -> dict:
    """One HTTP attempt. Raises TransientError for timeout / connection error / 5xx, ValueError for 4xx."""
    body = json.dumps({"shop": shop, "item": item}).encode()
    req = request.Request(f"{URL}/orders", method="POST", data=body,
                          headers={"Content-Type": "application/json", "Idempotency-Key": key, "X-Client-ID": "caller.py"})
    try:
        with request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
            data["_replayed"] = resp.headers.get("Idempotent-Replayed") == "true"
            return data
    except error.HTTPError as e:
        detail = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        if e.code >= 500:
            raise TransientError(f"HTTP {e.code} {detail}") from None
        raise ValueError(f"HTTP {e.code} {detail}") from None
    except (socket.timeout, TimeoutError):
        raise TransientError(f"timeout after {TIMEOUT_S} s") from None
    except error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise TransientError(f"timeout after {TIMEOUT_S} s") from None
        raise TransientError(f"connection error: {e.reason}") from None


def place_order(shop: str, item: str, breaker: CircuitBreaker) -> dict | None:
    """One logical order: up to 1 + RETRIES attempts with the same Idempotency-Key, guarded by the breaker."""
    if not breaker.allow():
        print(f"[{now()}] {shop:10s} BLOCKED  breaker is OPEN, not calling")
        return None
    key = uuid.uuid4().hex
    for n in range(RETRIES + 1):
        try:
            order = attempt(shop, item, key)
            tag = "REPLAYED" if order["_replayed"] else "OK      "
            print(f"[{now()}] {shop:10s} {tag} order {order['order_id']} ({order['state']}) on attempt {n + 1}")
            breaker.record_success()
            return order
        except ValueError as e:                                # 4xx: our fault, retrying cannot help
            print(f"[{now()}] {shop:10s} FAILED   {e} (no retry: client error)")
            breaker.record_failure()
            return None
        except TransientError as e:
            if n < RETRIES:
                wait = BACKOFF_S * 2 ** n
                print(f"[{now()}] {shop:10s} attempt {n + 1}/{RETRIES + 1} failed: {e}; retrying in {wait:.2f} s")
                time.sleep(wait)
            else:
                print(f"[{now()}] {shop:10s} FAILED   after {RETRIES + 1} attempts: {e}")
    breaker.record_failure()
    return None


def chaos(method: str, body: dict | None = None) -> None:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(f"{URL}/chaos", method=method, data=data, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=TIMEOUT_S):
        pass


def demo(breaker: CircuitBreaker) -> None:
    print("\n--- 1. normal: three orders at sky_dhaba")
    for _ in range(3):
        place_order("sky_dhaba", "chai", breaker)

    print("\n--- 2. server failure: PUT /chaos closes tapal (every new tapal order -> 503); the caller retries, then the breaker opens")
    chaos("PUT", {"fail_shop": "tapal"})
    for _ in range(3):
        place_order("tapal", "biryani", breaker)

    print("\n--- 3. breaker OPEN: calls are blocked without touching the server (sky_dhaba is healthy but still blocked)")
    for _ in range(2):
        place_order("sky_dhaba", "chai", breaker)
        time.sleep(0.5)

    print(f"\n--- 4. recovery: DELETE /chaos re-opens tapal; wait for the {BREAKER_OPEN_S:.0f} s open period, then one trial call")
    chaos("DELETE")
    time.sleep(BREAKER_OPEN_S)
    for _ in range(3):
        place_order("tapal", "biryani", breaker)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="run the failure / breaker / recovery demo (toggles /chaos)")
    ap.add_argument("--shop", default="sky_dhaba")
    ap.add_argument("--item", default="chai")
    ap.add_argument("--count", type=int, default=3)
    args = ap.parse_args()
    print(f"caller -> {URL}/orders | timeout {TIMEOUT_S} s | retries {RETRIES} (backoff {BACKOFF_S} s x2) | "
          f"breaker opens after {BREAKER_FAILURES} failures for {BREAKER_OPEN_S:.0f} s")
    breaker = CircuitBreaker(BREAKER_FAILURES, BREAKER_OPEN_S)
    if args.demo:
        demo(breaker)
    else:
        for _ in range(args.count):
            place_order(args.shop, args.item, breaker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
