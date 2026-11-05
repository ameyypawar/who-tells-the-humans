"""One CLI turn end to end. Owner: WP3 (stub from WP0)."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from wtth.types import SlotDirs, TurnOutcome, TurnRequest

if TYPE_CHECKING:
    from wtth.orchestrator.context import RunContext


async def run_turn(ctx: "RunContext", req: TurnRequest, slot: SlotDirs, *, cancel: asyncio.Event) -> TurnOutcome:
    raise NotImplementedError("WP3")
