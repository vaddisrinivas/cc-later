"""Non-blocking file lock for cc-later."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class NonBlockingFileLock:
    """Atomic lock based on O_EXCL file creation."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        payload = {"pid": os.getpid(), "ts": datetime.now(timezone.utc).isoformat()}
        os.write(self.fd, json.dumps(payload).encode("utf-8"))
        return True

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "NonBlockingFileLock":
        if not self.acquire():
            raise RuntimeError(f"could not acquire lock {self.path}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()
