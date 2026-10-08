"""The single source of truth for orders, shared by the HTTP API (app/main.py) and the gRPC
service (grpc_service/server.py), which run as separate processes.

Assignment 2 Part B needs both to apply the same business rules to the same data, so the
state machine and the storage moved out of main.py into this module. The storage is a
SQLite file (CANTEEN_DB) on a volume that both containers mount; every function opens a
short-lived connection and SQLite's file lock serialises the two writers.

    place_order  ->  waiting --transition("ready")--> ready --transition("picked_up")--> picked_up
                        |                              |
                        +-------- transition("cancelled") ---------> cancelled
"""
from __future__ import annotations

import os
import sqlite3
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, shop TEXT NOT NULL, item TEXT NOT NULL, state TEXT NOT NULL,
    placed_at REAL NOT NULL, ready_at REAL, picked_up_at REAL, cancelled_at REAL);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id), created_at REAL NOT NULL);
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
    """A connection per call; `write=True` takes the write lock up front (BEGIN IMMEDIATE) so a
    read-check-update sequence from either process cannot interleave with the other's."""
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
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


def init() -> None:
    """Create the tables (idempotent). Both processes call this at startup."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        conn.execute("PRAGMA journal_mode = WAL")      # readers do not block the writer across processes
        conn.executescript(SCHEMA)
    finally:
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
        order = Order(order_id=uuid.uuid4().hex[:8], shop=shop, item=item, placed_at=time.time())
        c.execute("INSERT INTO orders (order_id, shop, item, state, placed_at) VALUES (?, ?, ?, ?, ?)",
                  (order.order_id, order.shop, order.item, order.state, order.placed_at))
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
