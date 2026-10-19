"""SU STORE order-api entrypoint: `python order_api.py` (see server/CLAUDE.md for the layout)."""
from __future__ import annotations

import os
import threading
from http.server import ThreadingHTTPServer

from fp28.backups import run_scheduled_backup
from fp28.config import HOST, PORT
from fp28.database import ensure_db
from fp28.web.handler import OrderRequestHandler


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

    # Only the primary process runs the scheduled backup thread — otherwise
    # every worker would race to write the same daily snapshot.
    if is_primary:
        threading.Thread(target=run_scheduled_backup, daemon=True, name="scheduled-backup").start()

    server = _ReusePortHTTPServer((HOST, PORT), OrderRequestHandler)
    role = "primary" if is_primary else "worker"
    print(f"Order API [{role} pid={os.getpid()}] listening on http://{HOST}:{PORT}")
    server.serve_forever()

