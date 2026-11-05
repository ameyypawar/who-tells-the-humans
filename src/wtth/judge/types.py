"""Shared judge types (plan 47 section 2.18). WP6-B and WP6-C import from here."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

ItemKey = tuple[str, str]  # (source_table, source_id), e.g. ("posts", "1234"), ("debrief_answers", "57:1:2:D1")


class ItemType(StrEnum):
    BOARD_POST = "board_post"
    NOTE = "note"
    D1_ANSWER = "d1_answer"
    D2_ANSWER = "d2_answer"


@dataclass(frozen=True, slots=True)
class Item:
    item_id: str
    item_type: ItemType
    key: ItemKey
    held_out_key: str | None
    episode_id: int | None
    attempt: int | None
    phase: str | None
    scenario: str | None
    model: str | None
    cell: str | None
    level: str | None
    seed: int | None
    world: str | None
    round: int | None
    slot: int | None
    handle: str | None
    in_analysis_set: bool
    kw_match: bool | None
    text: str
    context_text: str
    tier: str
    subtier: str
    reasons: tuple[str, ...]
    order_key: int


@dataclass(frozen=True, slots=True)
class ParsedOutput:
    ok: bool
    labels: Mapping[str, Any] | None
    quote: str | None
    confidence: str | None
    uncertain: int | None
    justification: str | None
    escalation_returned: int | None
    escalation_mismatch: bool
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CallOutcome:
    call_id: str
    item_id: str
    ask_no: int
    try_no: int
    cls: str
    fail: str | None
    detail: str
    output_text: str
    parsed: ParsedOutput | None
    quote_ok: bool | None
    cost_usd: float
    cost_estimated: bool


@dataclass(frozen=True, slots=True)
class JudgeLabel:
    item_id: str
    prompt_version: str
    status: str  # 'valid' | 'invalid' | 'refused'
    labels: Mapping[str, Any] | None
    quote: str | None
    confidence: str | None
    uncertain: int | None
    n_asks: int


@dataclass(frozen=True, slots=True)
class FinalLabel:
    key: ItemKey
    item_type: ItemType
    labels: Mapping[str, Any]
    source: str  # 'reviewed' | 'judge'
    uncertain: int | None
    judge_labels: Mapping[str, Any] | None
    queue: str | None
    reviewed: bool


class LabelSource(Protocol):
    def get(self, key: ItemKey) -> FinalLabel | None: ...

    def status(self, key: ItemKey) -> str: ...

    def gates(self) -> Mapping[str, bool]: ...  # {'p1_final', 'p2prime_final'}


class DictLabelSource:
    """Tests and provisional runs; implements LabelSource."""

    def __init__(self, labels: Mapping[ItemKey, FinalLabel], *, statuses: Mapping[ItemKey, str] | None = None,
                 gates: Mapping[str, bool] | None = None) -> None:
        self._labels = dict(labels)
        self._statuses = dict(statuses or {})
        self._gates = dict(gates) if gates is not None else {"p1_final": True, "p2prime_final": True}

    def get(self, key: ItemKey) -> FinalLabel | None:
        return self._labels.get(key)

    def status(self, key: ItemKey) -> str:
        if key in self._statuses:
            return self._statuses[key]
        fl = self._labels.get(key)
        if fl is None:
            return "unknown"
        return "reviewed" if fl.source == "reviewed" else "judge"

    def gates(self) -> Mapping[str, bool]:
        return dict(self._gates)


class BudgetBinds(RuntimeError):
    """The budget rule binds (exit 4)."""


class JudgeStop(RuntimeError):
    """A STOP condition (exit 3)."""


class ExtractionError(RuntimeError):
    """Item extraction found an inconsistency."""
