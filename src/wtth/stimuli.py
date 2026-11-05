"""Seed -> episode spec, clocks, scripted posts and notes, round injection, state loading. Owner: WP1 (stub from WP0)."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from wtth.types import CellSpec, EpisodeSpec, EpisodeState, ScriptedNote, ScriptedPost

if TYPE_CHECKING:
    from wtth.db import Db
    from wtth.prereg import ScenarioTexts

HANDLE_ORDER: dict[int, tuple] = {}   # WP1 fills (scripted_lines.md section 1)
TICKET_ORDER: dict[int, tuple] = {}   # WP1 fills


def digest_clock(r: int) -> str:
    raise NotImplementedError("WP1")


def witness_stamp(r: int, slot: int) -> str:
    raise NotImplementedError("WP1")


def clock_min(hhmm: str) -> int:
    raise NotImplementedError("WP1")


def witness_visible_from(r: int) -> int:
    raise NotImplementedError("WP1")


def scripted_visible_from(r: int) -> int:
    raise NotImplementedError("WP1")


def episode_spec(phase: str, scenario: str, model: str, cell: CellSpec, level: str, seed: int,
                 queue_pos: int, prefix: str) -> EpisodeSpec:
    raise NotImplementedError("WP1")


def scripted_posts(spec: EpisodeSpec, texts: "ScenarioTexts", round: int) -> list[ScriptedPost]:
    raise NotImplementedError("WP1")


def scripted_note(spec: EpisodeSpec, texts: "ScenarioTexts") -> ScriptedNote | None:
    raise NotImplementedError("WP1")


def tickets_for(spec: EpisodeSpec, texts: "ScenarioTexts", round: int, slot: int) -> tuple[str, str, str]:
    raise NotImplementedError("WP1")


def ticket_ids(lines: tuple[str, ...]) -> tuple[str, ...]:
    raise NotImplementedError("WP1")


def inject_round(db: "Db", spec: EpisodeSpec, episode_id: int, attempt: int, round: int,
                 texts: "ScenarioTexts", run_id: str) -> None:
    raise NotImplementedError("WP1")


def load_state(conn: sqlite3.Connection, spec: EpisodeSpec, episode_id: int, attempt: int) -> EpisodeState:
    raise NotImplementedError("WP1")
