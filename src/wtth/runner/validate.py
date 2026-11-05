"""Init/result assertions, reply bytes, refusal detection, log scan, reconcile. Owner: WP3 (stub from WP0)."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from wtth.types import RefusalFlags, StreamRecord, Toolset, TurnRequest


def validate_init(rec: StreamRecord, exp: dict, req: TurnRequest) -> list[str]:
    raise NotImplementedError("WP3")


def validate_result(rec: StreamRecord, exp: dict, req: TurnRequest) -> list[str]:
    raise NotImplementedError("WP3")


def check_replies(rec: StreamRecord, toolset: Toolset | None) -> list[str]:
    raise NotImplementedError("WP3")


def detect_refusal(rec: StreamRecord, rules: Any) -> RefusalFlags:
    raise NotImplementedError("WP3")


def scan_logs(stderr_path: Path, mcp_logs: Sequence[Path], allow: dict) -> tuple[list[str], list[str], int | None]:
    """Returns (stderr_unexpected, mcp_log_unexpected, mcp_connect_ms). Owner: WP3."""
    raise NotImplementedError("WP3")


def reconcile(rec: StreamRecord, server_rows: list[sqlite3.Row], mcp_log_calls: int | None) -> list[str]:
    raise NotImplementedError("WP3")
