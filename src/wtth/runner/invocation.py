"""argv / env / mcp-config construction. Owner: WP3 (stub from WP0)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from wtth.types import Identity, SlotDirs, Toolset, TurnRequest


def build_argv(settings: Any, req: TurnRequest, mcp_config_json: str | None) -> list[str]:
    raise NotImplementedError("WP3")


def build_env(settings: Any, slot: SlotDirs, api_key: str, bodies_dir: Path | None) -> dict[str, str]:
    raise NotImplementedError("WP3")


def build_mcp_config(settings: Any, ident: Identity, toolset: Toolset) -> str:
    raise NotImplementedError("WP3")


def redact_argv(argv: list[str]) -> list[str]:
    raise NotImplementedError("WP3")
