"""Single-run lock (fcntl.flock). Owner: WP4."""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path


class LockHeld(Exception):
    """Another orchestrator holds runs/wtth.lock."""


class RunLock:
    """flock(LOCK_EX | LOCK_NB) held for the process lifetime; the kernel drops it when the process dies."""

    def __init__(self, path: Path, run_id: str = "") -> None:
        self.path = Path(path)
        self.run_id = run_id
        self._fh = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise LockHeld(f"lock held: {self.path}") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} run_id={self.run_id} started={time.time():.0f}\n")
        fh.flush()
        self._fh = fh
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
