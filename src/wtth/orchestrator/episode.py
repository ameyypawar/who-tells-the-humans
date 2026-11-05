"""Episode protocol and attempt state machine. Owner: WP4 (stub from WP0)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from wtth.types import AttemptResult, EpisodeSpec

if TYPE_CHECKING:
    from wtth.orchestrator.context import RunContext


async def run_attempt(ctx: "RunContext", spec: EpisodeSpec, episode_id: int) -> AttemptResult:
    raise NotImplementedError("WP4")
