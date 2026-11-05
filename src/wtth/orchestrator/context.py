"""RunContext and the shared run-time gates. Owner: WP4 (stub from WP0)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from wtth.types import FailClass, SlotDirs, TurnClass


@dataclass
class RunContext:
    """The API key is held as `_api_key` (excluded from repr). WP4 may add behaviour, not change these fields."""

    settings: Any
    db: Any
    run_id: str
    mode: str
    toolsets: dict
    texts: dict
    private: dict
    slots: "SlotPool"
    kgate: "KGate"
    launch: "LaunchGate"
    cost: "CostTracker"
    breaker: "Breaker"
    expected_init: dict
    allow: dict
    _api_key: str = field(default="", repr=False)


class SlotPool:
    def acquire(self) -> SlotDirs:
        raise NotImplementedError("WP4")

    def release(self, slot: SlotDirs) -> None:
        raise NotImplementedError("WP4")


class KGate:
    """Resizable semaphore over CLI processes. Use as `async with kgate:`."""

    def resize(self, k: int) -> None:
        raise NotImplementedError("WP4")

    async def __aenter__(self) -> None:
        raise NotImplementedError("WP4")

    async def __aexit__(self, exc_type, exc, tb) -> None:
        raise NotImplementedError("WP4")


class LaunchGate:
    async def wait(self) -> None:
        raise NotImplementedError("WP4")


class CostTracker:
    def can_launch(self, model: str) -> bool:
        raise NotImplementedError("WP4")

    def charge(self, turn_id: str, phase: str, model: str, cost: float, estimated: bool) -> None:
        raise NotImplementedError("WP4")

    def spent(self) -> float:
        raise NotImplementedError("WP4")


class Breaker:
    def record(self, cls: TurnClass, fail: FailClass | None) -> Literal["ok", "tripped", "stop"]:
        raise NotImplementedError("WP4")
