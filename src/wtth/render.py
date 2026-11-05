"""Prompt rendering: system prompt, round prompt, debrief prompts, log numbering. Owner: WP1 (stub from WP0)."""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from wtth.types import CellSpec, EpisodeState, LogRow, PostRow, RenderedPrompt

if TYPE_CHECKING:
    from wtth.prereg import ScenarioTexts


def system_prompt(texts: "ScenarioTexts", cell: CellSpec) -> str:
    raise NotImplementedError("WP1")


def round_prompt(state: EpisodeState, texts: "ScenarioTexts", round: int, slot: int) -> RenderedPrompt:
    raise NotImplementedError("WP1")


def debrief_prompt(state: EpisodeState, texts: "ScenarioTexts", slot: int, which: Literal["D1", "D2"],
                   d1_answer: str | None) -> RenderedPrompt:
    raise NotImplementedError("WP1")


def number_log(rows: Sequence[LogRow]) -> list[tuple[int, LogRow]]:
    raise NotImplementedError("WP1")


def board_lines(posts: Sequence[PostRow]) -> str:
    raise NotImplementedError("WP1")


def log_block(clock: str, numbered: Sequence[tuple[int, LogRow]]) -> str:
    raise NotImplementedError("WP1")


def record_lines(state: EpisodeState, slot: int, upto_round: int, *, debrief: bool) -> str:
    raise NotImplementedError("WP1")
