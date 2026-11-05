"""Keyword rule and G1/G2 reports. Owner: WP5 (stub from WP0)."""
from __future__ import annotations

from typing import Any


def keyword_match(text: str, scenario: str) -> bool:
    raise NotImplementedError("WP5")


def g1_report(db: Any, level: str) -> dict:
    raise NotImplementedError("WP5")


def g2_report(db: Any, phase: str) -> dict:
    raise NotImplementedError("WP5")
