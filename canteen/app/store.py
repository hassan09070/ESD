"""The single source of truth for orders, shared by the HTTP API (app/main.py) and the gRPC
service (grpc_service/server.py), which run as separate processes.

Assignment 2 Part B needs both to apply the same business rules to the same data, so the
state machine and the storage moved out of main.py into this module. The storage is a
SQLite file (CANTEEN_DB) on a volume that every replica mounts; every function opens a
short-lived connection and SQLite's file lock serialises writers across processes.

Assignment 3 (three replicas, each with many threads writing): two writers in the SAME
process that collide on the file lock make SQLite's busy handler sleep in coarse steps
(1, 2, 5 ... 100 ms), which at 40 writes/s on one replica cascaded into multi-second
latencies. So (1) writers in one process queue on _WRITE_LOCK first and never see
SQLITE_BUSY from a sibling thread, (2) WAL + synchronous=NORMAL skips the fsync per commit
(durable against a crash of the process, not against power loss - fine for a canteen), and
(3) orders(state, shop) is a covering index because waiting_counts() runs on every placed order
and on every GET /shops, and the A3 load tests leave tens of thousands of orders waiting,
and (4) init() keeps one connection open for the life of the process: when the LAST connection
to a WAL database closes, SQLite checkpoints and fsyncs the whole WAL, and with one short-lived
connection per call that happened on almost every call (the WAL file was always empty).
With the anchor connection open, checkpoints run only every wal_autocheckpoint (1000) pages.

    place_order  ->  waiting --transition("ready")--> ready --transition("picked_up")--> picked_up
                        |                              |
                        +-------- transition("cancelled") ---------> cancelled
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from app.metrics import SHOPS

DB_PATH = os.environ.get("CANTEEN_DB", "canteen.db")
STATES = ("waiting", "ready", "picked_up", "cancelled")
TRANSITIONS = {"ready": ("waiting",), "picked_up": ("ready",), "cancelled": ("waiting", "ready")}
STAMP = {"ready": "ready_at", "picked_up": "picked_up_at", "cancelled": "cancelled_at"}
ITEM_MAX_LEN = 40
_WRITE_LOCK = threading.Lock()         # A3: one writer at a time per process (see module docstring)
_ANCHOR: sqlite3.Connection | None = None   # A3: kept open so per-call connections are never "the last one"
_ANCHOR_PATH: str | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, shop TEXT NOT NULL, item TEXT NOT NULL, state TEXT NOT NULL,
    placed_at REAL NOT NULL, ready_at REAL, picked_up_at REAL, cancelled_at REAL);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id), created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS orders_state_shop ON orders(state, shop);
DROP INDEX IF EXISTS orders_state;
"""


# ----------------------------------------------------------------------------- errors
class DomainError(Exception):
    """A business-rule violation. The HTTP layer maps subclasses to 4xx, the gRPC layer to status codes."""
    code = "domain_error"


class UnknownShop(DomainError):
    code = "unknown_shop"


class InvalidItem(DomainError):
    code = "invalid_item"


class NotFound(DomainError):
    code = "not_found"


class IllegalTransition(DomainError):
    code = "conflict"

    def __init__(self, current: str, target: str):
        super().__init__(f"order is {current}, cannot become {target}")
        self.current, self.target = current, target


# ----------------------------------------------------------------------------- model
@dataclass
class Order:
    order_id: str
    shop: str
    item: str
    state: str = "waiting"                 # one of STATES
    placed_at: float = 0.0                 # time.time() when each transition happened
    ready_at: float | None = None
    picked_up_at: float | None = None
    cancelled_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(**{k: row[k] for k in Order.__dataclass_fields__})


# ----------------------------------------------------------------------------- storage
@contextmanager
def connect(write: bool = False):
    """A connection per call; `write=True` takes the process-wide _WRITE_LOCK and then SQLite's
    write lock (BEGIN IMMEDIATE) so a read-check-update sequence from any thread of any replica
    cannot interleave with another's."""
    if write:
        _WRITE_LOCK.acquire()
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if write and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
        if write:
            _WRITE_LOCK.release()


def init() -> None:
    """Create the tables (idempotent) and open the anchor connection. Every replica calls this at startup."""
    global _ANCHOR, _ANCHOR_PATH
    conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")          # readers do not block the writer across processes
    conn.executescript(SCHEMA)
    if _ANCHOR is not None and _ANCHOR_PATH != DB_PATH:
        _ANCHOR.close()
        _ANCHOR = None
    if _ANCHOR is None:
        _ANCHOR, _ANCHOR_PATH = conn, DB_PATH          # stays open (see module docstring); never used for queries
    else:
        conn.close()


def reset() -> None:
    """Tests only: empty both tables."""
    with connect(write=True) as c:
        c.execute("DELETE FROM idempotency_keys")
        c.execute("DELETE FROM orders")


# ----------------------------------------------------------------------------- business rules
def validate_new_order(shop: str, item: str) -> None:
    if shop not in SHOPS:
        raise UnknownShop(f"unknown shop {shop!r}; choose one of {list(SHOPS)}")
    if not isinstance(item, str) or not 1 <= len(item) <= ITEM_MAX_LEN:
        raise InvalidItem(f"item must be 1-{ITEM_MAX_LEN} characters")


def place_order(shop: str, item: str, idempotency_key: str | None = None) -> tuple[Order, bool]:
    """Create an order. With an idempotency key, a repeat returns the order created the first
    time and `replayed=True` instead of a second order (A2 Part A: safe repeats)."""
    validate_new_order(shop, item)
    with connect(write=True) as c:
        if idempotency_key:
            row = c.execute("SELECT o.* FROM idempotency_keys k JOIN orders o ON o.order_id = k.order_id WHERE k.key = ?",
                            (idempotency_key,)).fetchone()
            if row is not None:
                return _row_to_order(row), True
        # A3 finding: an 8-hex order id is 32 bits; with 60k orders in the table the load tests hit
        # two collisions in 24k inserts (IntegrityError -> gRPC INTERNAL). Keep the short id, but
        # try a fresh one on a collision instead of failing the request.
        for attempt in range(5):
            order = Order(order_id=uuid.uuid4().hex[:8], shop=shop, item=item, placed_at=time.time())
            try:
                c.execute("INSERT INTO orders (order_id, shop, item, state, placed_at) VALUES (?, ?, ?, ?, ?)",
                          (order.order_id, order.shop, order.item, order.state, order.placed_at))
                break
            except sqlite3.IntegrityError:
                if attempt == 4:
                    raise
        if idempotency_key:
            c.execute("INSERT INTO idempotency_keys (key, order_id, created_at) VALUES (?, ?, ?)",
                      (idempotency_key, order.order_id, order.placed_at))
    return order, False


def get_order(order_id: str) -> Order:
    with connect() as c:
        row = c.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if row is None:
        raise NotFound("unknown order")
    return _row_to_order(row)


def transition(order_id: str, new_state: str) -> Order:
    """Move an order to `new_state` if its current state permits it, else IllegalTransition.
    Atomic: the row is read and updated under the write lock."""
    allowed_from = TRANSITIONS[new_state]
    with connect(write=True) as c:
        row = c.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise NotFound("unknown order")
        order = _row_to_order(row)
        if order.state not in allowed_from:
            raise IllegalTransition(order.state, new_state)
        order.state = new_state
        setattr(order, STAMP[new_state], time.time())
        c.execute(f"UPDATE orders SET state = ?, {STAMP[new_state]} = ? WHERE order_id = ?",
                  (new_state, getattr(order, STAMP[new_state]), order_id))
    return order


def waiting_counts() -> dict[str, int]:
    """Queue length per shop (orders placed but not yet ready); the source of the waiting gauge."""
    with connect() as c:
        rows = c.execute("SELECT shop, COUNT(*) AS n FROM orders WHERE state = 'waiting' GROUP BY shop").fetchall()
    counts = {shop: 0 for shop in SHOPS}
    counts.update({r["shop"]: r["n"] for r in rows})
    return counts


def count() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def cancel_waiting(item: str) -> int:
    """Maintenance (A3 runner): cancel every waiting order of `item`. The fixed-rate test places
    orders nobody cooks; left waiting they make /shops and PlaceOrder slower run after run, so
    scripts/experiment_lb.sh clears them before each run to keep the workload's cost constant."""
    with connect(write=True) as c:
        cur = c.execute("UPDATE orders SET state = 'cancelled', cancelled_at = ? WHERE state = 'waiting' AND item = ?", (time.time(), item))
        return cur.rowcount
