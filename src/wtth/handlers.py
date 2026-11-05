"""Pure tool handlers over Db + Identity, and the fault sink. Owner: WP2 (stub from WP0)."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from wtth.types import Caps, Identity, Toolset

if TYPE_CHECKING:
    from wtth.db import Db


class FaultSink:
    def __init__(self, fault_dir: Path, turn_id: str | None) -> None:
        raise NotImplementedError("WP2")

    def record(self, tool: str, args: dict, error: str, ident: Identity) -> None:
        raise NotImplementedError("WP2")

    @property
    def tripped(self) -> bool:
        raise NotImplementedError("WP2")


def dispatch(db: "Db", ident: Identity, toolset: Toolset, name: str, args: dict, fault: FaultSink) -> str:
    """Never raises; always returns the toolset's fixed reply. Owner: WP2."""
    raise NotImplementedError("WP2")


def submit_reviews(db: "Db", ident: Identity, toolset: Toolset, args: dict) -> None:
    raise NotImplementedError("WP2")


def post_to_board(db: "Db", ident: Identity, args: dict, caps: Caps) -> None:
    raise NotImplementedError("WP2")


def notify_ops(db: "Db", ident: Identity, args: dict, caps: Caps) -> None:
    """Raises WriteFailed. The orchestrator also calls it directly for the scripted note. Owner: WP2."""
    raise NotImplementedError("WP2")
