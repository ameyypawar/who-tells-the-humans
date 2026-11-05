"""Shared enums and dataclasses (research/41 section 2.9). No logic.

All dataclasses are frozen with slots unless marked mutable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class World(StrEnum):
    R0 = "R0"
    R1 = "R1"


class TurnType(StrEnum):
    WITNESS = "witness"
    D1 = "d1"
    D2 = "d2"
    PROBE = "probe"
    JUDGE = "judge"


class TurnClass(StrEnum):
    OK = "ok"
    REFUSAL = "refusal"
    MAX_TURNS = "max_turns"
    MAX_BUDGET = "max_budget"
    TECHNICAL = "technical"
    STOP = "stop"
    PROBE = "probe"


class FailClass(StrEnum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SERVER_FAULT = "server_fault"
    NO_RESULT = "no_result"
    EXIT = "exit"
    CORRUPT_STREAM = "corrupt_stream"
    BILLING_ROUTE = "billing_route"
    BILLING = "billing"
    API_TRANSIENT = "api_transient"
    INIT_MISMATCH = "init_mismatch"
    MODEL_MISMATCH = "model_mismatch"
    PERMISSION_DENIED = "permission_denied"
    REPLY_MISMATCH = "reply_mismatch"
    MCP_TRANSPORT = "mcp_transport"
    RECONCILE = "reconcile"
    UNKNOWN_SUBTYPE = "unknown_subtype"
    API_OTHER = "api_other"
    ORCH_RESTART = "orch_restart"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    reply: str


@dataclass(frozen=True, slots=True)
class Toolset:
    name: str
    id_field: str
    tools: tuple[ToolSpec, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class Caps:
    """Length caps from tools.yaml `caps`. Also readable as a mapping: caps["post_chars"]."""

    post_chars: int = 600
    subject_chars: int = 60
    message_chars: int = 2000

    def __getitem__(self, key: str) -> int:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class CellSpec:
    name: str
    world: World
    runbook: str
    system: str
    toolset: str
    c2: str  # 'C2.R' | 'C2.CN' | 'CLAIM' | 'CLAIM+FORGED' | 'C2.OBJ'
    c_note: bool
    v_lines: str  # 'F' | 'BN'
    header_owner: bool = False
    owner: str | None = None  # 'slot1' | handle


@dataclass(frozen=True, slots=True)
class EpisodeSpec:
    episode_key: str
    phase: str
    scenario: str
    model: str
    cell: CellSpec
    level: str
    seed: int
    handle_idx: int
    ticket_idx: int
    paraphrase_idx: int
    handles: tuple[str, str, str]
    ticket_sets: tuple[str, str, str]
    queue_pos: int


@dataclass(frozen=True, slots=True)
class Identity:
    run_id: str
    episode_id: int
    attempt: int
    round: int
    slot: int
    sender: str
    clock: str
    clock_min: int
    visible_from_round: int
    world: World
    source: str  # 'tool' | 'script'
    turn_id: str | None
    toolset: str
    ticket_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScriptedPost:
    round: int
    sender: str
    clock: str
    line_id: str
    text: str
    is_private: bool


@dataclass(frozen=True, slots=True)
class ScriptedNote:
    round: int
    sender: str
    clock: str
    subject: str
    message: str
    visible_from_round: int


@dataclass(frozen=True, slots=True)
class PostRow:
    id: int
    round: int
    sender: str
    slot: int | None
    clock: str
    clock_min: int
    kind: str  # 'scripted' | 'live'
    line_id: str | None
    text: str
    visible_from_round: int


@dataclass(frozen=True, slots=True)
class NoteRow:
    id: int
    round: int
    sender: str
    slot: int | None
    clock: str
    clock_min: int
    subject: str
    message: str
    source: str  # 'tool' | 'script'
    visible_from_round: int


@dataclass(frozen=True, slots=True)
class LogRow:
    id: int
    note_id: int
    sender: str
    clock: str
    clock_min: int
    subject_display: str
    visible_from_round: int


@dataclass(frozen=True, slots=True)
class ReviewRow:
    id: int
    round: int
    sender: str
    entries: tuple[tuple[str, str, str], ...]
    used: bool


@dataclass(frozen=True, slots=True)
class EpisodeState:
    spec: EpisodeSpec
    episode_id: int
    attempt: int
    posts: tuple[PostRow, ...]
    notes: tuple[NoteRow, ...]
    log: tuple[LogRow, ...]
    reviews: tuple[ReviewRow, ...]
    d1_answers: dict[int, str]


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    text: str
    sha256: str
    visible_post_ids: tuple[int, ...]
    visible_note_ids: tuple[int, ...]
    visible_log_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SlotDirs:
    index: int
    root: Path
    home: Path
    config: Path
    tmp: Path


@dataclass(frozen=True, slots=True)
class TurnRequest:
    turn_id: str
    episode_id: int
    attempt: int
    turn_type: TurnType
    round: int | None
    slot: int | None
    handle: str | None
    model: str
    system_path: Path
    system_sha: str
    prompt: RenderedPrompt
    toolset: Toolset | None
    identity: Identity | None
    max_turns: int
    max_budget_usd: float
    timeout_s: float
    audit_bodies: bool
    settings_path: Path
    turn_dir: Path


@dataclass(frozen=True, slots=True)
class ProcResult:
    exit_code: int | None
    term_signal: int | None
    timed_out: bool
    kill_stage: str | None
    wall_ms: int
    stdout_path: Path
    stderr_path: Path
    pgid: int
    peak_rss_kb: int | None


@dataclass(slots=True)
class ToolUse:  # mutable
    seq: int
    tool_use_id: str | None
    name: str
    input: dict
    has_result: bool = False
    result_text: str | None = None
    result_is_error: bool | None = None


@dataclass(slots=True)
class StreamRecord:  # mutable
    n_lines: int = 0
    n_nonjson: int = 0
    n_bad_json: int = 0
    truncated_tail: bool = False
    n_init: int = 0
    n_result: int = 0
    init: dict | None = None
    result: dict | None = None
    assistant_texts: list[str] = field(default_factory=list)
    tool_uses: list[ToolUse] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)
    session_id: str | None = None
    api_retries: list[dict] = field(default_factory=list)
    rate_limit_events: int = 0
    unknown_events: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefusalFlags:
    refusal_stop: bool
    refusal_text: bool


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    turn_id: str
    cls: TurnClass
    fail: FailClass | None
    detail: str
    violations: tuple[str, ...]
    record: StreamRecord
    proc: ProcResult
    cost_usd: float
    cost_estimated: bool
    final_text: str
    refusal: RefusalFlags


@dataclass(frozen=True, slots=True)
class AttemptResult:
    episode_id: int
    attempt: int
    status: str
    fail: FailClass | None
    detail: str
    root_turn_id: str | None


# Type alias used by signatures that take parsed JSON.
Json = Any
