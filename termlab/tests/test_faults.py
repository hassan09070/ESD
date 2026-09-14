import time

import pytest

from api.config import Settings
from api.docker_client import SANDBOX_LABEL, FakeDocker, sandbox_run_kwargs
from api.faults import Fault, start_hogs, stop_hogs
from api.sessions import SessionManager


def test_none_is_a_noop():
    f = Fault.from_settings(Settings(fault="none"))
    assert f.spawn_delay_s == 0 and f.hog_count == 0 and f.warm_pool_size(2) == 2


def test_cold_start_disables_warm_pool_and_delays_spawn():
    f = Fault.from_settings(Settings(fault="cold_start", fault_spawn_delay_s=0.05, warm_pool_size=2))
    assert f.warm_pool_size(2) == 0
    t0 = time.perf_counter()
    f.before_spawn()
    assert time.perf_counter() - t0 >= 0.05


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        Fault.from_settings(Settings(fault="explode"))


async def test_cpu_hog_starts_labelled_hogs():
    fake = FakeDocker()
    f = Fault.from_settings(Settings(fault="cpu_hog", hog_count=3))
    hogs = await start_hogs(f, fake)
    assert len(hogs) == 3 and all(h.hog for h in hogs)
    assert sum(1 for sb in fake.list_sandboxes() if sb.hog) == 3
    await stop_hogs(hogs, fake)
    assert not fake.containers


async def test_manager_under_cold_start_spawns_cold(clock):
    fake = FakeDocker()
    m = SessionManager(Settings(fault="cold_start", fault_spawn_delay_s=0.02, warm_pool_size=2, pool_size=2), fake, clock=clock)
    await m.startup()
    assert len(m.warm_pool.ready) == 0
    s = m.create_session()
    r = await m.request_sandbox(s)
    assert r.source == "cold" and r.spawn_ms >= 20
    await m.shutdown()


async def test_manager_under_cpu_hog_excludes_hogs_from_stats(clock):
    fake = FakeDocker()
    m = SessionManager(Settings(fault="cpu_hog", hog_count=2, warm_pool_size=0, pool_size=2), fake, clock=clock)
    info = await m.startup()
    assert info["hogs"] == 2
    await m.stats_tick()                     # no user sandboxes -> stats calls only for user sandboxes
    assert fake._tick == 0
    await m.shutdown()
    assert not fake.containers               # hogs removed on shutdown


def test_run_kwargs_are_hardened():
    k = sandbox_run_kwargs(Settings(), "n", {})
    assert k["network_mode"] == "none" and k["read_only"] and k["cap_drop"] == ["ALL"] and k["user"] == "1000:1000"
    assert k["nano_cpus"] == 500_000_000 and k["mem_limit"] == "256m" and k["pids_limit"] == 100 and k["labels"][SANDBOX_LABEL] == "1"
    assert "uid=1000" in k["tmpfs"]["/home/user"]                   # the root-owned-tmpfs bug (REPORT D.4)
    assert "nano_cpus" not in sandbox_run_kwargs(Settings(), "h", {}, hog=True)
