"""Runs only when a Docker daemon is reachable: real container, real exec PTY."""
import os
import socket
import time

import pytest

from api.config import Settings
from api.docker_client import RealDocker

pytestmark = pytest.mark.skipif(not os.path.exists("/var/run/docker.sock"), reason="no docker socket")


@pytest.fixture(scope="module")
def real():
    settings = Settings()
    try:
        rd = RealDocker(settings)
        rd.client.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"docker daemon not reachable: {e}")
    if not rd.image_ready():
        pytest.skip("termlab-sandbox:local not built (docker build -t termlab-sandbox:local sandbox)")
    return rd


def read_until(sock: socket.socket, needle: bytes, timeout=10.0) -> bytes:
    sock.settimeout(timeout)
    buf = b""
    deadline = time.time() + timeout
    while needle not in buf and time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    assert needle in buf, buf
    return buf


def test_spawn_exec_resize_stats_reap(real):
    sb = real.spawn("termlab-sbx-test", {"termlab.session_id": "test"})
    try:
        st = real.inspect(sb)
        assert st.running
        exec_id, sock = real.exec_create(sb, 100, 30)
        read_until(sock, b"$ ")
        sock.sendall(b"echo $((40+2)); id -u; hostname; ls / | head -3\r")
        out = read_until(sock, b"42")
        read_until(sock, b"1000")
        real.exec_resize(exec_id, 120, 40)
        sock.sendall(b"stty size\r")
        read_until(sock, b"40 120")
        sock.sendall(b"touch /etc/x 2>&1; echo RO=$?\r")
        read_until(sock, b"RO=1")                                 # read-only rootfs
        sock.sendall(b"touch ~/mine /tmp/mine; echo HOME_$?\r")
        read_until(sock, b"HOME_0")                               # tmpfs home is owned by uid 1000
        sock.sendall(b"python3 -c 'import urllib.request as u; u.urlopen(\"http://example.com\", timeout=2)' 2>&1 | tail -c 60; echo NET_$((1+1))\r")
        out = read_until(sock, b"NET_2", timeout=15)
        assert b"Error" in out or b"error" in out or b"unreachable" in out or b"resolve" in out   # network none
        s1 = real.stats(sb)
        assert s1.mem_limit == 256 * 2**20 and s1.online_cpus >= 1
        sock.sendall(b"exit\r")
        read_until(sock, b"logout")
        sock.close()
        assert real.inspect(sb).running                            # container survives the shell exiting
    finally:
        real.remove(sb)
    assert not real.inspect(sb).running
    assert all(x.name != "termlab-sbx-test" for x in real.list_sandboxes())

