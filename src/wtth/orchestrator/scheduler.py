"""Queue building, resume, scheduling loop. Owner: WP4 (stub from WP0)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wtth.types import EpisodeSpec

if TYPE_CHECKING:
    from wtth.db import Db
    from wtth.orchestrator.context import RunContext


def build_queue(settings: Any, phase: str, level: str | None) -> list[EpisodeSpec]:
    raise NotImplementedError("WP4")


def enqueue(db: "Db", specs: list[EpisodeSpec]) -> int:
    raise NotImplementedError("WP4")


def resume_recover(ctx: "RunContext") -> None:
    raise NotImplementedError("WP4")


class Scheduler:
    def __init__(self, ctx: "RunContext", phase: str) -> None:
        raise NotImplementedError("WP4")

    async def run(self) -> int:
        """Returns the exit code (0 ok, 3 STOP, 4 CAP, 5 lock held, 130 operator). Owner: WP4."""
        raise NotImplementedError("WP4")
