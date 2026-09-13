"""All runtime configuration comes from TERMLAB_* environment variables and is read once here.

Nothing else in the package calls os.environ (except metrics.py for the cardinality demo flag,
which has to be known at import time because it changes a metric's label set).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

FAULT_MODES = ("none", "cold_start", "cpu_hog")


def _env(name: str, default: str) -> str:
    return os.environ.get(f"TERMLAB_{name}", default)


@dataclass(frozen=True)
class Settings:
    fault: str = "none"
    pool_size: int = 10                      # max concurrently running user sandboxes
    warm_pool_size: int = 2                  # pre-created sandboxes waiting to be claimed
    idle_timeout_s: float = 900.0            # 15 min without keystrokes -> reaped
    queue_timeout_s: float = 60.0            # how long POST /sandbox waits for a free slot (0 = refuse immediately)
    reaper_interval_s: float = 10.0
    stats_interval_s: float = 10.0
    sandbox_image: str = "termlab-sandbox:local"
    sandbox_cpus: float = 0.5
    sandbox_memory: str = "256m"
    sandbox_pids: int = 100
    docker_host: str = "unix:///var/run/docker.sock"
    fault_spawn_delay_s: float = 2.0         # cold_start fault: extra sleep inside every spawn
    hog_count: int = 4                       # cpu_hog fault: number of unlimited stress-ng containers
    demo_cardinality: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        fault = _env("FAULT", "none")
        if fault not in FAULT_MODES:
            raise ValueError(f"TERMLAB_FAULT must be one of {FAULT_MODES}, got {fault!r}")
        return cls(
            fault=fault,
            pool_size=int(_env("POOL_SIZE", "10")),
            warm_pool_size=int(_env("WARM_POOL_SIZE", "2")),
            idle_timeout_s=float(_env("IDLE_TIMEOUT_S", "900")),
            queue_timeout_s=float(_env("QUEUE_TIMEOUT_S", "60")),
            reaper_interval_s=float(_env("REAPER_INTERVAL_S", "10")),
            stats_interval_s=float(_env("STATS_INTERVAL_S", "10")),
            sandbox_image=_env("SANDBOX_IMAGE", "termlab-sandbox:local"),
            sandbox_cpus=float(_env("SANDBOX_CPUS", "0.5")),
            sandbox_memory=_env("SANDBOX_MEMORY", "256m"),
            sandbox_pids=int(_env("SANDBOX_PIDS", "100")),
            docker_host=_env("DOCKER_HOST", "unix:///var/run/docker.sock"),
            fault_spawn_delay_s=float(_env("FAULT_SPAWN_DELAY_S", "2.0")),
            hog_count=int(_env("HOG_COUNT", "4")),
            demo_cardinality=_env("DEMO_CARDINALITY", "0") == "1",
        )
