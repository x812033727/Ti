from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def settings_server_lock(root: Path):
    lock_dir = root / ".pytest_cache"
    lock_dir.mkdir(exist_ok=True)
    with (lock_dir / "settings-server.lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
