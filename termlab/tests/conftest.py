import pytest

from api.config import Settings
from api.docker_client import FakeDocker
from api.sessions import SessionManager


class FakeClock:
    """Monotonic clock the tests can advance by hand (idle timeouts without sleeping)."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake():
    return FakeDocker()


@pytest.fixture
def settings():
    return Settings(pool_size=3, warm_pool_size=1, idle_timeout_s=60, queue_timeout_s=0.3, reaper_interval_s=0.05, stats_interval_s=0.05)


@pytest.fixture
async def manager(settings, fake, clock):
    m = SessionManager(settings, fake, clock=clock)
    await m.startup()
    yield m
    await m.shutdown()
