import asyncio

import pytest
from prometheus_client import REGISTRY

from api.docker_client import SANDBOX_LABEL, Sandbox
from api.sessions import IllegalTransition, SessionError, State


def counter(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


async def test_create_session_has_token_and_state(manager):
    s = manager.create_session(request_id="r1")
    assert s.state == State.created and len(s.token) > 20 and s.user_id == s.session_id
    assert manager.get(s.session_id, s.token) is s
    with pytest.raises(SessionError) as e:
        manager.get(s.session_id, "wrong")
    assert e.value.status == 403
    with pytest.raises(SessionError) as e:
        manager.get("nope")
    assert e.value.status == 404


async def test_first_sandbox_is_warm_then_cold(manager, fake):
    assert len(manager.warm_pool.ready) == 1
    a = manager.create_session()
    ra = await manager.request_sandbox(a)
    assert ra.source == "warm" and a.state == State.running and a.sandbox is not None
    manager.warm_pool.size = 0                                   # stop the refill so the next one is cold
    manager.warm_pool.ready.clear()
    b = manager.create_session()
    rb = await manager.request_sandbox(b)
    assert rb.source == "cold" and rb.queue_ms == 0
    assert counter("termlab_sessions_started_total", outcome="ok") >= 2
    assert REGISTRY.get_sample_value("termlab_sandboxes_active") == 2
    assert REGISTRY.get_sample_value("termlab_sandbox_spawn_seconds_count", {"source": "cold"}) >= 1


async def test_second_request_on_same_session_is_409(manager):
    s = manager.create_session()
    await manager.request_sandbox(s)
    with pytest.raises(SessionError) as e:
        await manager.request_sandbox(s)
    assert e.value.status == 409


async def test_pool_full_queues_then_times_out(manager, settings):
    sessions = [manager.create_session() for _ in range(settings.pool_size + 1)]
    for s in sessions[:-1]:
        await manager.request_sandbox(s)
    before = counter("termlab_sessions_started_total", outcome="queued_timeout")
    with pytest.raises(SessionError) as e:
        await manager.request_sandbox(sessions[-1])
    assert e.value.status == 503 and e.value.error == "queued_timeout"
    assert sessions[-1].state == State.created                   # can retry later
    assert counter("termlab_sessions_started_total", outcome="queued_timeout") == before + 1


async def test_queued_request_gets_slot_when_one_is_reaped(manager, settings):
    sessions = [manager.create_session() for _ in range(settings.pool_size + 1)]
    for s in sessions[:-1]:
        await manager.request_sandbox(s)
    waiter = asyncio.create_task(manager.request_sandbox(sessions[-1]))
    await asyncio.sleep(0.02)
    assert sessions[-1].state == State.queued and manager.pool_status()["queue"] == 1
    await manager.reap(sessions[0], "user_exit")
    r = await waiter
    assert r.queue_ms >= 10 and sessions[-1].state == State.running


async def test_reap_is_idempotent_and_records_business_metrics(manager, fake, clock):
    s = manager.create_session()
    await manager.request_sandbox(s)
    cid = s.sandbox.container_id
    clock.advance(30)
    seconds_before = counter("termlab_sandbox_seconds_total")
    assert await manager.reap(s, "user_exit") is True
    assert await manager.reap(s, "user_exit") is False
    assert s.state == State.reaped and s.sandbox is None and cid in fake.remove_calls
    assert counter("termlab_sandbox_seconds_total") == pytest.approx(seconds_before + 30)
    assert manager.pool.free == manager.pool.capacity
    with pytest.raises(SessionError):
        await manager.request_sandbox(s)                          # reaped sessions are not reusable


async def test_reaper_reaps_idle_sessions_and_forgets_old_empty_ones(manager, clock, settings):
    live = manager.create_session()
    await manager.request_sandbox(live)
    empty = manager.create_session()
    clock.advance(settings.idle_timeout_s + 1)
    before = counter("termlab_sandboxes_reaped_total", reason="idle")
    await manager.reaper_tick()
    assert live.state == State.reaped and live.reap_reason == "idle"
    assert counter("termlab_sandboxes_reaped_total", reason="idle") == before + 1
    assert empty.session_id not in manager.sessions


async def test_touch_prevents_idle_reap(manager, clock, settings):
    s = manager.create_session()
    await manager.request_sandbox(s)
    clock.advance(settings.idle_timeout_s - 1)
    manager.touch(s)
    clock.advance(settings.idle_timeout_s - 1)
    await manager.reaper_tick()
    assert s.state == State.running


async def test_reaper_detects_oom_and_exit(manager, fake):
    a = manager.create_session()
    await manager.request_sandbox(a)
    b = manager.create_session()
    await manager.request_sandbox(b)
    fake.kill(a.sandbox, oom=True)
    fake.kill(b.sandbox, oom=False)
    await manager.reaper_tick()
    assert a.reap_reason == "oom" and b.reap_reason == "user_exit"


async def test_attach_detach_state_and_gauge(manager):
    s = manager.create_session()
    await manager.request_sandbox(s)
    manager.on_attach(s, 80, 24)
    assert s.ws_count == 1 and REGISTRY.get_sample_value("termlab_users_connected") >= 1
    manager.on_detach(s, "client_close", 10, 5, 6, 1)
    assert s.state == State.detached
    manager.on_attach(s, 80, 24)
    assert s.state == State.running


async def test_illegal_transition_is_refused(manager):
    s = manager.create_session()
    with pytest.raises(IllegalTransition):
        manager._transition(s, State.running)


async def test_startup_removes_orphans_but_shutdown_keeps_nothing(settings, fake, clock):
    from api.sessions import SessionManager

    fake.spawn("termlab-sbx-orphan1", {SANDBOX_LABEL: "1"})
    fake.spawn("termlab-sbx-orphan2", {SANDBOX_LABEL: "1"})
    m = SessionManager(settings, fake, clock=clock)
    before = counter("termlab_sandboxes_reaped_total", reason="orphan")
    info = await m.startup()
    assert info["orphans_removed"] == 2 and counter("termlab_sandboxes_reaped_total", reason="orphan") == before + 2
    s = m.create_session()
    await m.request_sandbox(s)
    await m.shutdown()
    assert not fake.containers and s.reap_reason == "admin"


async def test_spawn_failure_releases_slot(manager, fake, monkeypatch):
    manager.warm_pool.size = 0
    manager.warm_pool.ready.clear()

    def boom(*a, **k):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(fake, "spawn", boom)
    s = manager.create_session()
    before = counter("termlab_sessions_started_total", outcome="error")
    with pytest.raises(SessionError) as e:
        await manager.request_sandbox(s)
    assert e.value.status == 500 and s.state == State.created
    assert manager.pool.free == manager.pool.capacity
    assert counter("termlab_sessions_started_total", outcome="error") == before + 1


async def test_stats_tick_aggregates_without_per_container_labels(manager, fake):
    a = manager.create_session()
    await manager.request_sandbox(a)
    await manager.stats_tick()                # first sample: memory only
    await manager.stats_tick()                # second: cpu delta available
    assert REGISTRY.get_sample_value("termlab_sandbox_memory_bytes_sum") == 64 * 2**20
    assert REGISTRY.get_sample_value("termlab_sandbox_cpu_cores_sum") == pytest.approx(0.1)
    assert REGISTRY.get_sample_value("termlab_stats_sample_seconds_count") >= 2
    body, _ = __import__("api.metrics", fromlist=["render"]).render()
    assert b"container" not in body.split(b"termlab_sandbox_cpu_cores_sum")[1][:80]


async def test_delete_while_queued_cancels_the_wait_and_reports_position(manager, settings):
    sessions = [manager.create_session() for _ in range(settings.pool_size + 2)]
    for s in sessions[:settings.pool_size]:
        await manager.request_sandbox(s)
    q1, q2 = sessions[-2], sessions[-1]
    w1 = asyncio.create_task(manager.request_sandbox(q1))
    w2 = asyncio.create_task(manager.request_sandbox(q2))
    await asyncio.sleep(0.02)
    assert manager.describe(q1)["queue_position"] == 1 and manager.describe(q2)["queue_position"] == 2
    assert await manager.reap(q1, "user_exit") is True             # user gave up (DELETE) while queued
    with pytest.raises(SessionError) as e:
        await w1
    assert e.value.status == 409 and e.value.error == "cancelled" and q1.state == State.reaped
    assert manager.describe(q2)["queue_position"] == 1 and manager.pool_status()["queue"] == 1
    await manager.reap(sessions[0], "user_exit")                    # a slot frees -> q2 gets it
    r = await w2
    assert r.queue_ms >= 0 and q2.state == State.running and manager.describe(q2)["queue_position"] is None
