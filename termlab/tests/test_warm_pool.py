import asyncio

from prometheus_client import REGISTRY

from api.docker_client import FakeDocker
from api.warm_pool import WarmPool


async def test_fill_creates_size_containers():
    fake = FakeDocker()
    wp = WarmPool(2, fake)
    assert await wp.fill() == 2
    assert len(wp.ready) == 2 and fake.spawn_calls == 2
    assert all(sb.warm for sb in wp.ready)
    assert REGISTRY.get_sample_value("termlab_warm_pool_size") == 2


async def test_claim_pops_and_refills():
    fake = FakeDocker()
    wp = WarmPool(1, fake)
    await wp.fill()
    sb = wp.claim()
    assert sb is not None and sb.warm
    await asyncio.sleep(0.05)          # refill task runs
    assert len(wp.ready) == 1 and fake.spawn_calls == 2


async def test_claim_on_empty_returns_none_and_size_zero_never_fills():
    fake = FakeDocker()
    wp = WarmPool(0, fake)
    assert await wp.fill() == 0
    assert wp.claim() is None and fake.spawn_calls == 0


async def test_concurrent_fill_does_not_overfill():
    fake = FakeDocker(spawn_delay=0.02)
    wp = WarmPool(2, fake)
    await asyncio.gather(wp.fill(), wp.fill(), wp.fill())
    assert len(wp.ready) == 2 and fake.spawn_calls == 2


async def test_drain_removes_all():
    fake = FakeDocker()
    wp = WarmPool(2, fake)
    await wp.fill()
    assert await wp.drain() == 2
    assert not fake.containers and REGISTRY.get_sample_value("termlab_warm_pool_size") == 0
