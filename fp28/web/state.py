"""Process-wide state shared by the route functions."""
from __future__ import annotations

import threading

test_warning_until: float = 0
lock = threading.Lock()
