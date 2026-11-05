"""Single-run lock (fcntl.flock). Owner: WP4 (stub from WP0)."""
from __future__ import annotations

from pathlib import Path


class LockHeld(Exception):
    """Another orchestrator holds runs/wtth.lock."""


class RunLock:
    def __init__(self, path: Path) -> None:
        raise NotImplementedError("WP4")

    def __enter__(self) -> "RunLock":
        raise NotImplementedError("WP4")

    def __exit__(self, exc_type, exc, tb) -> None:
        raise NotImplementedError("WP4")
