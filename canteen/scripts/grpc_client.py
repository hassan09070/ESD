#!/usr/bin/env python3
"""A2 Part B: a Python client that calls the gRPC PlaceOrder service directly.

    uv run python scripts/grpc_client.py --shop tapal --item biryani
    uv run python scripts/grpc_client.py --shop pizza --item x                 # -> INVALID_ARGUMENT
    uv run python scripts/grpc_client.py --shop grito --item corn --key k1     # repeat -> replayed=True
    uv run python scripts/grpc_client.py --shop tapal --item pulao --count 20  # Part C: repeatable calls

Each call sets a deadline (--deadline, default 2 s) and an x-request-id metadata entry (--request-id,
else generated) so the server's log lines and the trailing metadata carry the same id as the
client's output: that is the A1 trace correlation across the gRPC boundary. Results or the gRPC
status code and details are printed; the exit code is 1 if any call failed.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))   # the generated modules live in grpc_service/

import grpc  # noqa: E402

from grpc_service import canteen_pb2, canteen_pb2_grpc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("CANTEEN_GRPC", "localhost:50051"))
    ap.add_argument("--shop", required=True)
    ap.add_argument("--item", required=True)
    ap.add_argument("--key", default="", help="idempotency key; the same key returns the same order")
    ap.add_argument("--deadline", type=float, default=2.0, help="seconds the call may take before DEADLINE_EXCEEDED")
    ap.add_argument("--count", type=int, default=1, help="repeat the call this many times")
    ap.add_argument("--request-id", default=None, help="x-request-id metadata (default: generated per call)")
    args = ap.parse_args()

    failures = 0
    with grpc.insecure_channel(args.server) as channel:
        stub = canteen_pb2_grpc.OrderServiceStub(channel)
        for _ in range(args.count):
            rid = args.request_id or uuid.uuid4().hex[:12]
            req = canteen_pb2.PlaceOrderRequest(shop=args.shop, item=args.item, idempotency_key=args.key)
            t0 = time.perf_counter()
            try:
                order, call = stub.PlaceOrder.with_call(req, timeout=args.deadline, metadata=(("x-request-id", rid),))
                ms = (time.perf_counter() - t0) * 1000
                print(f"OK     order_id={order.order_id} shop={order.shop} state={order.state} replayed={order.replayed} "
                      f"({ms:.1f} ms) request_id={rid}")
            except grpc.RpcError as e:
                ms = (time.perf_counter() - t0) * 1000
                failures += 1
                print(f"ERROR  {e.code().name}: {e.details()} ({ms:.1f} ms) request_id={rid}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
