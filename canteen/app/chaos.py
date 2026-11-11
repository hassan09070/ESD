"""The fault settings, shared by the HTTP API and the gRPC server of one replica (A3).

Until A3 the settings lived in app/main.py and only the HTTP middleware read them. A replica
now runs both servers in one process (app/replica.py), so the settings moved here: PUT /chaos
on a replica changes the behaviour of *both* its interfaces, which is what Part D's "add a
fixed delay to B" needs. Each replica has its own copy, in memory: a fault set on B (port
8002 on the host) is invisible to A and C, exactly as intended for the experiments.

    slow_every_n / delay_ms   A1 Part E.1: every n-th /orders request sleeps delay_ms
    fail_shop                 A1 Part E.1: that shop answers 503 "closed" to every new order
    fixed_delay_ms            A3 Part D: every request and every RPC sleeps this long first
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.metrics import SHOPS

Shop = Literal[SHOPS]


class Chaos(BaseModel):
    """Faults toggled over HTTP so an experiment needs no restart."""
    slow_every_n: int = Field(0, ge=0, description="delay every n-th /orders request (0 = off); the A1 brief's example is 5")
    delay_ms: int = Field(500, ge=0, le=5000, description="how long the delayed requests sleep")
    fail_shop: Shop | None = Field(None, description="this shop answers 503 'closed' to every new order")
    fixed_delay_ms: int = Field(0, ge=0, le=5000, description="A3: every HTTP request and every gRPC call on this replica sleeps this long (0 = off)")


CURRENT = Chaos()


def get() -> Chaos:
    return CURRENT


def replace(settings: Chaos) -> Chaos:
    global CURRENT
    CURRENT = settings
    return CURRENT


def reset() -> Chaos:
    return replace(Chaos())
