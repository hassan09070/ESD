"""TERMLAB_FAULT handling. Two reversible faults, both toggled by re-creating the api container:

- cold_start: the warm pool is disabled and every spawn sleeps `fault_spawn_delay_s` in the
  worker thread (the daemon being slow / image cache lost). Deterministic -> the primary
  Part E experiment.
- cpu_hog: `hog_count` containers from the sandbox image run `stress-ng --cpu 0` with NO CPU
  cap, so user sandboxes (0.5 CPU each) and the api itself fight for the machine: the
  noisy-neighbour story. They carry termlab.sandbox=1 + termlab.hog=1 so orphan cleanup on
  the next restart removes them.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from api.config import FAULT_MODES, Settings
from api.docker_client import HOG_LABEL, DockerBackend, Sandbox, new_name

log = structlog.get_logger()


@dataclass(frozen=True)
class Fault:
    mode: str
    spawn_delay_s: float = 0.0
    hog_count: int = 0

    @classmethod
    def from_settings(cls, s: Settings) -> "Fault":
        if s.fault not in FAULT_MODES:
            raise ValueError(f"unknown fault mode {s.fault!r}")
        return cls(
            mode=s.fault,
            spawn_delay_s=s.fault_spawn_delay_s if s.fault == "cold_start" else 0.0,
            hog_count=s.hog_count if s.fault == "cpu_hog" else 0,
        )

    def warm_pool_size(self, configured: int) -> int:
        return 0 if self.mode == "cold_start" else configured

    def before_spawn(self) -> None:
        """Runs inside the spawn worker thread, never on the event loop."""
        if self.spawn_delay_s > 0:
            time.sleep(self.spawn_delay_s)


async def start_hogs(fault: Fault, backend: DockerBackend) -> list[Sandbox]:
    hogs: list[Sandbox] = []
    for _ in range(fault.hog_count):
        sb = await asyncio.to_thread(backend.spawn, new_name("hog"), {HOG_LABEL: "1"}, False, True)
        hogs.append(sb)
    if hogs:
        log.warning("fault", msg=f"cpu_hog: started {len(hogs)} unlimited stress-ng containers", fault_mode=fault.mode, hogs=len(hogs))
    return hogs


async def stop_hogs(hogs: list[Sandbox], backend: DockerBackend) -> None:
    for sb in hogs:
        await asyncio.to_thread(backend.remove, sb)
    hogs.clear()
