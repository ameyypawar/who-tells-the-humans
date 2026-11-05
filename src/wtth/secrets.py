"""API key loading. Owner: WP3 (stub from WP0). No literal key prefix anywhere in this repo (compose at runtime)."""
from __future__ import annotations

from pathlib import Path


class KeyFileError(Exception):
    """Key file problem. Messages never contain file content."""


def load_api_key(path: Path) -> str:
    raise NotImplementedError("WP3")
