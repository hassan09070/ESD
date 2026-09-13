"""The Docker side of termlab: the `DockerBackend` protocol, the hardened container spec,
the real implementation over docker-py, and an in-memory fake for tests.

Every method here is *blocking*; callers on the event loop wrap them in asyncio.to_thread.
"""
from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from api.config import Settings

SANDBOX_LABEL = "termlab.sandbox"
SESSION_LABEL = "termlab.session_id"
WARM_LABEL = "termlab.warm"
HOG_LABEL = "termlab.hog"


@dataclass
class Sandbox:
    container_id: str
    name: str
    warm: bool = False
    hog: bool = False

    @property
    def short_id(self) -> str:
        return self.container_id[:12]


@dataclass
class ContainerState:
    running: bool
    oom_killed: bool = False
    exit_code: int | None = None


@dataclass
class StatsSample:
    cpu_total_ns: int
    system_ns: int
    online_cpus: int
    mem_usage: int
    mem_limit: int


def sandbox_run_kwargs(settings: Settings, name: str, labels: dict[str, str], hog: bool = False) -> dict:
    """The complete container spec for one sandbox. Quoted verbatim in the report.

    A sandbox is `sleep infinity` under tini; the user's shell is a separate `docker exec`,
    so a closed browser tab never kills the container (the idle reaper does). Hogs are the
    same image running stress-ng *without* the CPU cap - that is the noisy-neighbour fault.
    """
    kwargs = dict(
        image=settings.sandbox_image,
        command=["stress-ng", "--cpu", "0", "--timeout", "0"] if hog else ["sleep", "infinity"],
        init=True,
        detach=True,
        name=name,
        labels={SANDBOX_LABEL: "1", **labels},
        network_mode="none",
        mem_limit=settings.sandbox_memory,
        memswap_limit=settings.sandbox_memory,     # == mem_limit -> no swap
        pids_limit=settings.sandbox_pids,
        user="1000:1000",
        read_only=True,
        tmpfs={"/tmp": "rw,nosuid,size=64m", "/home/user": "rw,nosuid,size=64m"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        environment={"HOME": "/home/user", "TERM": "xterm-256color"},
        working_dir="/home/user",
        auto_remove=False,                          # keep it so inspect() can read OOMKilled; we remove explicitly
    )
    if not hog:
        kwargs["nano_cpus"] = int(settings.sandbox_cpus * 1_000_000_000)
    return kwargs


def new_name(prefix: str) -> str:
    return f"termlab-{prefix}-{secrets.token_hex(4)}"


class DockerBackend(Protocol):
    def image_ready(self) -> bool: ...
    def spawn(self, name: str, labels: dict[str, str], warm: bool = False, hog: bool = False) -> Sandbox: ...
    def remove(self, sandbox: Sandbox) -> None: ...
    def inspect(self, sandbox: Sandbox) -> ContainerState: ...
    def list_sandboxes(self) -> list[Sandbox]: ...
    def exec_create(self, sandbox: Sandbox, cols: int, rows: int) -> tuple[str, socket.socket]: ...
    def exec_resize(self, exec_id: str, cols: int, rows: int) -> None: ...
    def exec_running(self, exec_id: str) -> bool: ...
    def signal_shells(self, sandbox: Sandbox) -> None: ...
    def stats(self, sandbox: Sandbox) -> StatsSample: ...


# ---------------------------------------------------------------------------- real docker
class RealDocker:
    """docker-py over the mounted unix socket. `max_pool_size` matters: each attached PTY
    holds one connection from urllib3's pool (default 10) and the stats sampler needs more."""

    def __init__(self, settings: Settings):
        import docker  # imported lazily so the tests never need the package's transport

        self.settings = settings
        self.client = docker.DockerClient(base_url=settings.docker_host, timeout=60, max_pool_size=64)
        self.api = self.client.api

    def image_ready(self) -> bool:
        import docker.errors

        try:
            self.client.images.get(self.settings.sandbox_image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def spawn(self, name: str, labels: dict[str, str], warm: bool = False, hog: bool = False) -> Sandbox:
        kwargs = sandbox_run_kwargs(self.settings, name, {WARM_LABEL: "1" if warm else "0", HOG_LABEL: "1" if hog else "0", **labels}, hog=hog)
        container = self.client.containers.run(**kwargs)
        return Sandbox(container_id=container.id, name=name, warm=warm, hog=hog)

    def remove(self, sandbox: Sandbox) -> None:
        import docker.errors

        try:
            self.api.remove_container(sandbox.container_id, force=True, v=True)
        except docker.errors.NotFound:
            pass

    def inspect(self, sandbox: Sandbox) -> ContainerState:
        import docker.errors

        try:
            state = self.api.inspect_container(sandbox.container_id)["State"]
        except docker.errors.NotFound:
            return ContainerState(running=False, oom_killed=False, exit_code=None)
        return ContainerState(running=bool(state.get("Running")), oom_killed=bool(state.get("OOMKilled")), exit_code=state.get("ExitCode"))

    def list_sandboxes(self) -> list[Sandbox]:
        out = []
        for c in self.api.containers(all=True, filters={"label": f"{SANDBOX_LABEL}=1"}):
            labels = c.get("Labels") or {}
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            out.append(Sandbox(container_id=c["Id"], name=name, warm=labels.get(WARM_LABEL) == "1", hog=labels.get(HOG_LABEL) == "1"))
        return out

    def exec_create(self, sandbox: Sandbox, cols: int, rows: int) -> tuple[str, socket.socket]:
        exec_id = self.api.exec_create(
            sandbox.container_id, ["/bin/bash", "-l"], stdin=True, tty=True, user="1000:1000",
            environment=["TERM=xterm-256color", "HOME=/home/user", f"COLUMNS={cols}", f"LINES={rows}"],
        )["Id"]
        sock = self.api.exec_start(exec_id, socket=True, tty=True)
        # docker-py returns a SocketIO wrapper; the bridge wants the raw socket for asyncio.
        raw = getattr(sock, "_sock", sock)
        self._resize_when_running(exec_id, cols, rows)
        return exec_id, raw

    def _resize_when_running(self, exec_id: str, cols: int, rows: int) -> None:
        # Docker rejects a resize before the exec process has started (409); poll briefly.
        for _ in range(20):
            if self.exec_running(exec_id):
                try:
                    self.api.exec_resize(exec_id, height=rows, width=cols)
                except Exception:  # noqa: BLE001 - best effort; the client resends on its next resize
                    pass
                return
            time.sleep(0.05)

    def exec_resize(self, exec_id: str, cols: int, rows: int) -> None:
        self.api.exec_resize(exec_id, height=rows, width=cols)

    def exec_running(self, exec_id: str) -> bool:
        return bool(self.api.exec_inspect(exec_id).get("Running"))

    def signal_shells(self, sandbox: Sandbox) -> None:
        try:
            self.api.exec_start(self.api.exec_create(sandbox.container_id, ["pkill", "-HUP", "-x", "bash"], user="1000:1000")["Id"])
        except Exception:  # noqa: BLE001 - container may already be gone
            pass

    def stats(self, sandbox: Sandbox) -> StatsSample:
        s = self.api.stats(sandbox.container_id, stream=False, one_shot=True)
        cpu = s.get("cpu_stats", {})
        mem = s.get("memory_stats", {})
        return StatsSample(
            cpu_total_ns=int(cpu.get("cpu_usage", {}).get("total_usage", 0)),
            system_ns=int(cpu.get("system_cpu_usage", 0)),
            online_cpus=int(cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1),
            mem_usage=int(mem.get("usage", 0)) - int(mem.get("stats", {}).get("inactive_file", 0)),
            mem_limit=int(mem.get("limit", 0)) or 1,
        )


# ---------------------------------------------------------------------------- fake docker
@dataclass
class _FakeContainer:
    container_id: str
    name: str
    labels: dict[str, str]
    running: bool = True
    oom_killed: bool = False
    exit_code: int | None = None
    execs: list[str] = field(default_factory=list)


class FakeDocker:
    """In-memory stand-in with the same protocol. Execs are socketpairs served by a tiny
    echo shell thread: every byte is echoed back, `\\r` also produces a prompt, and `exit`
    closes the shell so the bridge sees EOF."""

    def __init__(self, spawn_delay: float = 0.0, image_present: bool = True):
        self.containers: dict[str, _FakeContainer] = {}
        self.spawn_delay = spawn_delay
        self.image_present = image_present
        self.spawn_calls = 0
        self.remove_calls: list[str] = []
        self.resizes: list[tuple[str, int, int]] = []
        self._shells: dict[str, socket.socket] = {}
        self._tick = 0
        self._lock = threading.Lock()

    def image_ready(self) -> bool:
        return self.image_present

    def spawn(self, name, labels, warm=False, hog=False) -> Sandbox:
        if self.spawn_delay:
            time.sleep(self.spawn_delay)
        with self._lock:
            self.spawn_calls += 1
            cid = secrets.token_hex(32)
            self.containers[cid] = _FakeContainer(cid, name, {SANDBOX_LABEL: "1", WARM_LABEL: "1" if warm else "0", HOG_LABEL: "1" if hog else "0", **labels})
        return Sandbox(container_id=cid, name=name, warm=warm, hog=hog)

    def remove(self, sandbox) -> None:
        with self._lock:
            self.remove_calls.append(sandbox.container_id)
            c = self.containers.pop(sandbox.container_id, None)
        if c:
            for eid in c.execs:
                s = self._shells.pop(eid, None)
                if s:
                    s.close()

    def inspect(self, sandbox) -> ContainerState:
        c = self.containers.get(sandbox.container_id)
        if c is None:
            return ContainerState(running=False)
        return ContainerState(running=c.running, oom_killed=c.oom_killed, exit_code=c.exit_code)

    def list_sandboxes(self) -> list[Sandbox]:
        return [Sandbox(c.container_id, c.name, warm=c.labels.get(WARM_LABEL) == "1", hog=c.labels.get(HOG_LABEL) == "1")
                for c in self.containers.values()]

    # test helpers
    def kill(self, sandbox, oom: bool = False) -> None:
        c = self.containers[sandbox.container_id]
        c.running, c.oom_killed, c.exit_code = False, oom, 137 if oom else 0

    def exec_create(self, sandbox, cols, rows):
        c = self.containers[sandbox.container_id]
        exec_id = secrets.token_hex(8)
        ours, theirs = socket.socketpair()
        c.execs.append(exec_id)
        self._shells[exec_id] = ours
        threading.Thread(target=self._echo_shell, args=(ours,), daemon=True).start()
        self.resizes.append((exec_id, cols, rows))
        return exec_id, theirs

    @staticmethod
    def _echo_shell(sock: socket.socket) -> None:
        try:
            sock.sendall(b"user@termlab:~$ ")
            buf = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                sock.sendall(data)
                buf += data
                if b"\r" in data:
                    line, _, buf = buf.rpartition(b"\r")
                    if line.strip().endswith(b"exit"):
                        sock.sendall(b"\r\nlogout\r\n")
                        break
                    sock.sendall(b"\r\nuser@termlab:~$ ")
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def exec_resize(self, exec_id, cols, rows) -> None:
        self.resizes.append((exec_id, cols, rows))

    def exec_running(self, exec_id) -> bool:
        return exec_id in self._shells

    def signal_shells(self, sandbox) -> None:
        c = self.containers.get(sandbox.container_id)
        if c:
            for eid in c.execs:
                s = self._shells.pop(eid, None)
                if s:
                    s.close()

    def stats(self, sandbox) -> StatsSample:
        # Each call advances a fake clock: 100 ms CPU per 4 s of total system time on 4 cpus -> 0.1 cores.
        self._tick += 1
        return StatsSample(cpu_total_ns=self._tick * 100_000_000, system_ns=self._tick * 1_000_000_000 * 4,
                           online_cpus=4, mem_usage=64 * 2**20, mem_limit=256 * 2**20)
