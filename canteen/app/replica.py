"""One canteen replica = one process serving both interfaces (Assignment 3 Part A).

    python -m app.replica         (CANTEEN_REPLICA=A, PORT=8000, GRPC_PORT=50051, CANTEEN_DB)

Until A3 the HTTP API (uvicorn, app/main.py) and the gRPC service (grpc_service/server.py)
were two containers. A replica in A3 must expose *both*, so this entrypoint starts the gRPC
server (its own thread pool) and then runs uvicorn in the main thread. Consequences that the
rest of the stack relies on:

  - one Prometheus registry: GET /metrics on the HTTP port carries the gRPC metrics too, so
    Prometheus scrapes one target per replica (prometheus.yml labels it replica=A|B|C);
  - one stdout: every log line of either interface carries the same `replica` field;
  - one chaos object (app/chaos.py): PUT /chaos on this replica delays its gRPC calls as well.

Shutdown (Part D.3, `docker compose stop app-b`): on SIGTERM the gRPC health service is set to
NOT_SERVING, the gRPC server stops accepting and finishes in-flight calls within 1 s, and
uvicorn is told to exit with a 2 s graceful limit. Without this, uvicorn alone waited for
Envoy's busy keep-alive connections until Docker's 10 s SIGKILL, with the gRPC port open but
idle in between: the replica was "half dead" for 10 s instead of gone.

The two modules still work on their own (`uvicorn app.main:app`, `python -m grpc_service.server`);
the tests use them that way.
"""
from __future__ import annotations

import os

import uvicorn

from app.main import app
from grpc_service import server as grpc_server


def main() -> None:
    srv, _ = grpc_server.serve(metrics_port=None)             # metrics come out of app.main's /metrics instead
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False,
                            timeout_graceful_shutdown=2)
    http = uvicorn.Server(config)
    uvicorn_exit = http.handle_exit

    def stop(signum: int, frame) -> None:                    # uvicorn installs *this* for SIGTERM/SIGINT (it reads self.handle_exit)
        grpc_server.set_not_serving()                         # Envoy's next grpc health check fails
        srv.stop(grace=1)                                     # no new RPCs; in-flight ones get 1 s
        uvicorn_exit(signum, frame)                           # should_exit = True; graceful limit 2 s (config above)

    http.handle_exit = stop
    http.run()
    srv.stop(grace=1).wait()


if __name__ == "__main__":
    main()
