import asyncio

import pytest
from prometheus_client import REGISTRY

from api.pool import Pool, QueueTimeout


def gauge(name):
    return REGISTRY.get_sample_value(name)


async def test_acquire_free_slot_is_immediate():
    p = Pool(2)
    assert await p.acquire(timeout=1) == 0.0
    assert p.free == 1 and p.active == 1
    assert gauge("termlab_pool_free") == 1 and gauge("termlab_queue_length") == 0


async def test_waiter_is_handed_slot_on_release():
    p = Pool(1)
    await p.acquire(1)
    waiter = asyncio.create_task(p.acquire(timeout=5))
    await asyncio.sleep(0.01)
    assert len(p.waiters) == 1 and gauge("termlab_queue_length") == 1
    p.release()
    waited = await waiter
    assert waited >= 0 and p.free == 0 and len(p.waiters) == 0     # slot passed straight through


async def test_timeout_removes_waiter_and_raises():
    p = Pool(1)
    await p.acquire(1)
    with pytest.raises(QueueTimeout) as e:
        await p.acquire(timeout=0.05)
    assert not e.value.immediate and e.value.waited_s >= 0.05
    assert len(p.waiters) == 0 and gauge("termlab_queue_length") == 0


async def test_zero_timeout_refuses_immediately():
    p = Pool(1)
    await p.acquire(1)
    with pytest.raises(QueueTimeout) as e:
        await p.acquire(timeout=0)
    assert e.value.immediate


async def test_release_skips_timed_out_waiters():
    p = Pool(1)
    await p.acquire(1)
    with pytest.raises(QueueTimeout):
        await p.acquire(timeout=0.01)
    p.release()
    assert p.free == 1


async def test_fifo_order():
    p = Pool(1)
    await p.acquire(1)
    order = []

    async def w(i):
        await p.acquire(5)
        order.append(i)

    tasks = [asyncio.create_task(w(i)) for i in range(3)]
    await asyncio.sleep(0.01)
    for _ in range(3):
        p.release()
        await asyncio.sleep(0.01)
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2]
