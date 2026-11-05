"""MCP server `workspace` (FastMCP subclass overriding list_tools/call_tool). Owner: WP2 (stub from WP0).

The stub does NOT run assert_no_secret_env at import; WP2 adds that.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, Tool

from wtth.types import Identity


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """WP2 defines the fields (runs root, db path, fault dir, tools path/sha, ...)."""


def assert_no_secret_env(environ: Mapping[str, str]) -> None:
    raise NotImplementedError("WP2")


def identity_from_env(environ: Mapping[str, str], argv: Sequence[str]) -> tuple[Identity, ServerConfig]:
    raise NotImplementedError("WP2")


class WorkspaceServer(FastMCP):
    async def list_tools(self) -> list[Tool]:  # type: ignore[override]
        raise NotImplementedError("WP2")

    async def call_tool(self, name: str, arguments: dict) -> list[TextContent]:  # type: ignore[override]
        raise NotImplementedError("WP2")


def main() -> None:
    raise NotImplementedError("WP2")
