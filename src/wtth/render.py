"""Prompt rendering: system prompt, round prompt, debrief prompts, log numbering. Owner: WP1.

Single-pass template filling only (textnorm.fill, plan decision D11): model-authored text that contains a
placeholder such as {record_lines} is never substituted. Rendering never re-normalizes stored live post text
(plan D3); scripted text is printed verbatim. World R0 never reads the escalation log.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from wtth import stimuli
from wtth.prereg import RUNBOOK_VARIANTS
from wtth.textnorm import fill, one_line, sha256_text
from wtth.types import CellSpec, EpisodeState, LogRow, NoteRow, PostRow, RenderedPrompt, World

if TYPE_CHECKING:
    from wtth.prereg import ScenarioTexts

# tools.md section 5 (checked against the parsed log template by tests/test_wp1_render.py).
LOG_HEADER = "#escalation-log (as of {clock})"
NO_ENTRIES = "No entries."
NOTHING_YET = "Nothing yet."
NO_REVIEWS = "no reviews recorded"
NO_POST = "No post."
FIRST_REF = 200  # E-0200 is the first line (ref = 199 + rank, rank from 1)

_LOG_BLOCK_GAP = "{log_block}\n\n"
_LOG_BLOCK_FINAL_GAP = "{log_block_final}\n\n"


# ---- system prompt --------------------------------------------------------------------------------------

def system_prompt(texts: "ScenarioTexts", cell: CellSpec) -> str:
    """SB-1 (BK-1 / BO-1) + blank line + base runbook + one blank-line-separated paragraph per variant extra."""
    try:
        ids = RUNBOOK_VARIANTS[cell.runbook]
    except KeyError:
        raise ValueError(f"unknown runbook variant {cell.runbook!r}") from None
    parts = [texts.system[cell.system], texts.runbook_base[ids[0]]]
    parts.extend(texts.runbook_paras[p] for p in ids[1:])
    return "\n\n".join(parts)


def system_file_name(scenario: str, cell: CellSpec) -> str:
    return f"system_{scenario}_{cell.system}_{cell.runbook}.txt"


def write_system_prompt(stimuli_dir: Path, texts: "ScenarioTexts", cell: CellSpec) -> tuple[Path, str]:
    """WP1-ADD helper (not in the frozen list): write the system prompt once per (scenario, system, variant) to
    `<stimuli_dir>/system_<scenario>_<sys>_<variant>.txt` (UTF-8, no trailing newline, mode 0444).
    Returns (path, sha256). An existing file with different bytes is an error (never overwritten)."""
    text = system_prompt(texts, cell)
    data = text.encode("utf-8")
    d = Path(stimuli_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / system_file_name(texts.scenario, cell)
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"{path.name} exists with different content")
        return path, sha256_text(text)
    tmp = d / (path.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    os.replace(tmp, path)
    return path, sha256_text(text)


# ---- building blocks ------------------------------------------------------------------------------------

def number_log(rows: Sequence[LogRow]) -> list[tuple[int, LogRow]]:
    """(ref, row) oldest first. Rank by (clock_min, note_id); ref = 199 + rank (identical to v_log_numbered)."""
    ordered = sorted(rows, key=lambda r: (r.clock_min, r.note_id))
    return [(FIRST_REF + i, r) for i, r in enumerate(ordered)]


def board_lines(posts: Sequence[PostRow]) -> str:
    """`[HH:MM] sender: text` per post, sorted by (clock_min, id), joined with \\n. Text exactly as stored."""
    ordered = sorted(posts, key=lambda p: (p.clock_min, p.id))
    return "\n".join(f"[{p.clock}] {p.sender}: {p.text}" for p in ordered)


def log_block(clock: str, numbered: Sequence[tuple[int, LogRow]]) -> str:
    """Header with the digest clock, then `E-nnnn | HH:MM | sender | subject` lines or `No entries.`"""
    entries = "\n".join(f"E-{ref:04d} | {r.clock} | {r.sender} | {r.subject_display}" for ref, r in numbered)
    return fill(LOG_HEADER, {"clock": clock}) + "\n" + (entries if entries else NO_ENTRIES)


def _reviews_clause(state: EpisodeState, handle: str, k: int) -> str:
    row = next((r for r in state.reviews if r.used and r.round == k and r.sender == handle), None)
    items: list[str] = []
    if row is not None:
        for entry in row.entries:
            parts = [x for x in (one_line(entry[0]), one_line(entry[1])) if x]
            if parts:
                items.append(" ".join(parts))
    if not items:
        return NO_REVIEWS
    return "reviews recorded (" + ", ".join(items) + ")"


def _own_posts(state: EpisodeState, handle: str, k: int) -> list[PostRow]:
    return sorted((p for p in state.posts if p.kind == "live" and p.sender == handle and p.round == k),
                  key=lambda p: p.id)


def _own_notes(state: EpisodeState, handle: str, k: int) -> list[NoteRow]:
    return sorted((n for n in state.notes if n.sender == handle and n.round == k), key=lambda n: n.id)


def record_lines(state: EpisodeState, slot: int, upto_round: int, *, debrief: bool) -> str:
    """The witness's own record for rounds 1..upto_round (research/41 section 2.1, A7 pins).

    Shift form: `Round k: {reviews}. {post}{ Sent to ops: "subject: message" / ...}`.
    Debrief form: the same without the Sent clause, then one `Round k: note sent to ops. Subject: s Message: m`
    line per accepted note of that round."""
    if upto_round < 1:
        return NOTHING_YET
    handle = state.spec.handles[slot - 1]
    out: list[str] = []
    for k in range(1, upto_round + 1):
        posts = _own_posts(state, handle, k)
        notes = _own_notes(state, handle, k)
        post = "Posted: " + " / ".join(f'"{p.text}"' for p in posts) if posts else NO_POST
        line = f"Round {k}: {_reviews_clause(state, handle, k)}. {post}"
        if not debrief and notes:
            line += " Sent to ops: " + " / ".join(f'"{n.subject}: {one_line(n.message)}"' for n in notes)
        out.append(line)
        if debrief:
            for n in notes:
                out.append(f"Round {k}: note sent to ops. Subject: {n.subject} Message: {one_line(n.message)}")
    return "\n".join(out)


def _record_note_ids(state: EpisodeState, handle: str, upto_round: int) -> tuple[int, ...]:
    return tuple(sorted(n.id for n in state.notes if n.sender == handle and 1 <= n.round <= upto_round))


def _owner_handle(state: EpisodeState) -> str:
    owner = state.spec.cell.owner or ""
    if owner == "slot1":
        return state.spec.handles[0]
    prefix = state.spec.handles[0].split("-", 1)[0]
    return f"{prefix}-{owner.split('-', 1)[1]}" if "-" in owner else owner


def _rendered(text: str, posts: Sequence[PostRow], notes: tuple[int, ...], log: Sequence[LogRow]) -> RenderedPrompt:
    return RenderedPrompt(text=text, sha256=sha256_text(text), visible_post_ids=tuple(sorted(p.id for p in posts)),
                          visible_note_ids=notes, visible_log_ids=tuple(sorted(r.id for r in log)))


# ---- round prompt ---------------------------------------------------------------------------------------

def round_prompt(state: EpisodeState, texts: "ScenarioTexts", round: int, slot: int) -> RenderedPrompt:
    """The user turn of `round` for `slot` (SB-2 filled in a single pass).

    visible_note_ids = the witness's own notes from earlier rounds (their text is quoted in the record); the
    notes behind log lines are not listed (the log shows subjects; ids of those rows are in visible_log_ids)."""
    if round not in range(1, stimuli.ROUNDS + 1):
        raise ValueError("round must be 1..5")
    if slot not in stimuli.SLOTS:
        raise ValueError("slot must be 1, 2 or 3")
    spec = state.spec
    cell = spec.cell
    handle = spec.handles[slot - 1]
    template = texts.round_template
    values: dict[str, str] = {}

    if cell.header_owner:
        anchor = "You are {handle}.\n"
        if template.count(anchor) != 1:
            raise ValueError("round template lacks the handle line")
        template = template.replace(anchor, anchor + "{owner_line}\n", 1)
        values["owner_line"] = fill(texts.owner_header_template, {"owner_handle": _owner_handle(state)})

    visible_posts = [p for p in state.posts if p.visible_from_round <= round]
    visible_log: list[LogRow] = []
    clock = stimuli.digest_clock(round)
    if cell.world is World.R1:
        visible_log = [r for r in state.log if r.visible_from_round <= round]
        values["log_block"] = log_block(clock, number_log(visible_log))
    else:
        if template.count(_LOG_BLOCK_GAP) != 1:
            raise ValueError("round template lacks the log block")
        template = template.replace(_LOG_BLOCK_GAP, "", 1)

    t1, t2, t3 = stimuli.tickets_for(spec, texts, round, slot)
    values.update({
        "round": str(round), "clock": clock, "handle": handle,
        "ticket_line_1": t1, "ticket_line_2": t2, "ticket_line_3": t3,
        "board_lines": board_lines(visible_posts),
        "record_lines": record_lines(state, slot, round - 1, debrief=False),
    })
    text = fill(template, values)
    return _rendered(text, visible_posts, _record_note_ids(state, handle, round - 1), visible_log)


# ---- debrief --------------------------------------------------------------------------------------------

def debrief_prompt(state: EpisodeState, texts: "ScenarioTexts", slot: int, which: Literal["D1", "D2"],
                   d1_answer: str | None) -> RenderedPrompt:
    """D1 message = record + blank line + D1; D2 message = record + blank line + `Earlier question: <D1>` +
    newline + `Your answer: <answer verbatim>` + blank line + D2."""
    if slot not in stimuli.SLOTS:
        raise ValueError("slot must be 1, 2 or 3")
    if which not in ("D1", "D2"):
        raise ValueError("which must be 'D1' or 'D2'")
    if which == "D2" and d1_answer is None:
        raise ValueError("D2 needs the stored D1 answer")
    spec = state.spec
    handle = spec.handles[slot - 1]
    template = texts.debrief_template
    values: dict[str, str] = {}
    posts = list(state.posts)
    log: list[LogRow] = []
    if spec.cell.world is World.R1:
        log = list(state.log)
        values["log_block_final"] = log_block(stimuli.DEBRIEF_CLOCK, number_log(log))
    else:
        if template.count(_LOG_BLOCK_FINAL_GAP) != 1:
            raise ValueError("debrief template lacks the final log block")
        template = template.replace(_LOG_BLOCK_FINAL_GAP, "", 1)
    values["board_lines_all"] = board_lines(posts)
    values["record_lines_all"] = record_lines(state, slot, stimuli.ROUNDS, debrief=True)
    record = fill(template, values)
    if which == "D1":
        text = record + "\n\n" + texts.d1
    else:
        text = (record + "\n\n" + "Earlier question: " + texts.d1 + "\n" + "Your answer: " + d1_answer
                + "\n\n" + texts.d2)
    return _rendered(text, posts, _record_note_ids(state, handle, stimuli.ROUNDS), log)
