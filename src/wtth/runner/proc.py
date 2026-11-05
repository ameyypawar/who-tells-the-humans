"""Subprocess handling: process groups, timeout ladder, orphan sweep, log harvest. Owner: WP3 (stub from WP0)."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from wtth.types import ProcResult, SlotDirs


async def run_process(argv: Sequence[str], env: Mapping[str, str], cwd: str | Path, stdout_path: Path,
                      stderr_path: Path, timeout_s: float, grace_int_s: float = 10.0,
                      grace_term_s: float = 5.0, rss_sampler: bool = False) -> ProcResult:
    raise NotImplementedError("WP3")


def kill_group(pgid: int, grace_int_s: float, grace_term_s: float) -> str:
    raise NotImplementedError("WP3")


def orphan_sweep(binary_path: Path, runs_root: Path, live_pgids: set[int]) -> list[int]:
    raise NotImplementedError("WP3")


def harvest_mcp_log(slot: SlotDirs, cwd: str, dest_dir: Path, since_ts: float) -> list[Path]:
    raise NotImplementedError("WP3")


def sample_rss_kb(pgids: Iterable[int]) -> dict[int, int]:
    raise NotImplementedError("WP3")


def memory_pressure_level() -> int:
    raise NotImplementedError("WP3")
