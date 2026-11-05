"""Startup verification and run manifest. Owner: WP4 (stub from WP0)."""
from __future__ import annotations

from typing import Any


def verify_startup(settings: Any, mode: str, phase: str) -> list[str]:
    raise NotImplementedError("WP4")


def build_manifest(settings: Any, run_id: str, phase: str, mode: str) -> dict:
    raise NotImplementedError("WP4")
