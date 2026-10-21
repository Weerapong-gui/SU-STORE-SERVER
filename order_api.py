"""SU STORE order-api entrypoint: `python order_api.py` (see README.md for the layout)."""
from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

from core import config
from core.config import HOST, PORT
from core.database import ensure_db, open_db
from core.handler import OrderRequestHandler
from store.demo_seed import seed_demo


class _ReusePortHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that enables SO_REUSEPORT so multiple worker
    processes can listen on the same port. Kernel load-balances accepts."""

    def server_bind(self) -> None:
        import socket as _sk
        try:
            self.socket.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEADDR, 1)
            if hasattr(_sk, "SO_REUSEPORT"):
                self.socket.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEPORT, 1)
        except OSError:
            pass
        super().server_bind()


if __name__ == "__main__":
    ensure_db()

    workers = max(1, int(os.environ.get("ORDER_API_WORKERS", "1")))
    # Fork workers-1 children *before* opening the socket so each process
    # binds independently with SO_REUSEPORT.
    is_primary = True
    for _ in range(workers - 1):
        pid = os.fork()
        if pid == 0:
            is_primary = False
            break

    if is_primary and config.DEMO_SEED:
        with open_db() as connection:
            seed_demo(connection)

    server = _ReusePortHTTPServer((HOST, PORT), OrderRequestHandler)
    role = "primary" if is_primary else "worker"
    print(f"Order API [{role} pid={os.getpid()}] listening on http://{HOST}:{PORT}")
    server.serve_forever()

