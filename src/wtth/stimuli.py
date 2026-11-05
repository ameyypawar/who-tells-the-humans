"""Seed -> episode spec, clocks, scripted posts and notes, round injection, state loading. Owner: WP1.

This module is the only place the shift clock, the stamps and the visibility rules are computed (research/41
section 2.1). Handle and ticket-set orders are pinned here and unit-tested against scripted_lines.md section 1.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import TYPE_CHECKING

from wtth.types import (Caps, CellSpec, EpisodeSpec, EpisodeState, Identity, LogRow, NoteRow, PostRow, ReviewRow,
                        ScriptedNote, ScriptedPost, World)

if TYPE_CHECKING:
    from wtth.db import Db
    from wtth.prereg import ScenarioTexts

# scripted_lines.md section 1. Index = (seed - 1) mod 6 -> handles of slots 1, 2, 3 (main-scenario handles;
# `episode_spec` swaps the prefix for the backup scenario).
HANDLE_ORDER: dict[int, tuple[str, str, str]] = {
    0: ("rd-14", "rd-21", "rd-26"),
    1: ("rd-14", "rd-26", "rd-21"),
    2: ("rd-21", "rd-14", "rd-26"),
    3: ("rd-21", "rd-26", "rd-14"),
    4: ("rd-26", "rd-14", "rd-21"),
    5: ("rd-26", "rd-21", "rd-14"),
}
# Index = ((seed - 1) // 6) mod 6 -> ticket set letters of slots 1, 2, 3.
TICKET_ORDER: dict[int, tuple[str, str, str]] = {
    0: ("A", "B", "C"),
    1: ("A", "C", "B"),
    2: ("B", "A", "C"),
    3: ("B", "C", "A"),
    4: ("C", "A", "B"),
    5: ("C", "B", "A"),
}

# Scripted roster (numbers; the prefix comes from the scenario).
V_NUM, C_NUM, B_NUM = "09", "11", "04"
# Stamps of scripted posts for round 1; each later round adds 30 minutes (scripted_lines.md section 1).
_B_STAMP, _V_STAMP, _C_STAMP = "20:35", "20:48", "20:56"
C_NOTE_STAMP = "21:24"
C_NOTE_ROUND = 2
DEBRIEF_CLOCK = "23:30"
ROUNDS = 5
SLOTS = (1, 2, 3)

_CLAIM_C2 = ("CLAIM", "CLAIM+FORGED")


# ---- clocks and visibility ------------------------------------------------------------------------------

def clock_min(hhmm: str) -> int:
    """'21:24' -> 1284 (minutes since 00:00)."""
    m = re.fullmatch(r"(\d{2}):(\d{2})", hhmm)
    if not m:
        raise ValueError("clock must be HH:MM")
    return int(m.group(1)) * 60 + int(m.group(2))


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _check_round(r: int) -> None:
    if not isinstance(r, int) or not 1 <= r <= ROUNDS:
        raise ValueError(f"round must be 1..{ROUNDS}")


def digest_clock(r: int) -> str:
    """T_r = 21:00 + 30 (r-1) minutes."""
    _check_round(r)
    return _hhmm(clock_min("21:00") + 30 * (r - 1))


def witness_stamp(r: int, slot: int) -> str:
    """Stamp of every post and note a witness makes in round r: T_r + 4 * slot minutes."""
    _check_round(r)
    if slot not in SLOTS:
        raise ValueError("slot must be 1, 2 or 3")
    return _hhmm(clock_min(digest_clock(r)) + 4 * slot)


def witness_visible_from(r: int) -> int:
    """A witness action in round r is first visible in the digest of round r + 1."""
    _check_round(r)
    return r + 1


def scripted_visible_from(r: int) -> int:
    """A scripted post labelled round r is visible in the digest of round r."""
    _check_round(r)
    return r


# ---- episode spec ---------------------------------------------------------------------------------------

def _swap_prefix(handle: str, prefix: str) -> str:
    return f"{prefix}-{handle.split('-', 1)[1]}"


def episode_spec(phase: str, scenario: str, model: str, cell: CellSpec, level: str, seed: int,
                 queue_pos: int, prefix: str) -> EpisodeSpec:
    """Seed formulas: handles by (seed-1) mod 6, ticket sets by ((seed-1)//6) mod 6, claim paraphrase by (seed-1) mod 3."""
    if not isinstance(seed, int) or seed < 1:
        raise ValueError("seed must be a positive integer")
    handle_idx = (seed - 1) % 6
    ticket_idx = ((seed - 1) // 6) % 6
    paraphrase_idx = (seed - 1) % 3
    h = HANDLE_ORDER[handle_idx]
    handles = (_swap_prefix(h[0], prefix), _swap_prefix(h[1], prefix), _swap_prefix(h[2], prefix))
    return EpisodeSpec(
        episode_key=f"{phase}/{scenario}/{model}/{cell.name}/{level}/s{seed}", phase=phase, scenario=scenario,
        model=model, cell=cell, level=level, seed=seed, handle_idx=handle_idx, ticket_idx=ticket_idx,
        paraphrase_idx=paraphrase_idx, handles=handles, ticket_sets=TICKET_ORDER[ticket_idx], queue_pos=queue_pos)


def _prefix(spec: EpisodeSpec) -> str:
    return spec.handles[0].split("-", 1)[0]


def roster(spec: EpisodeSpec) -> dict[str, str]:
    """Scripted handles: {'V': ..., 'C': ..., 'B': ...}."""
    p = _prefix(spec)
    return {"V": f"{p}-{V_NUM}", "C": f"{p}-{C_NUM}", "B": f"{p}-{B_NUM}"}


# ---- scripted lines -------------------------------------------------------------------------------------

def _v_line_id(spec: EpisodeSpec, r: int) -> str:
    if r == 1:
        return "V1"
    if r == 5:
        return "V5"
    if spec.cell.v_lines == "BN":
        return f"V{r}.BN"
    if spec.level not in ("L1", "L2", "L3"):
        raise ValueError(f"level must be L1, L2 or L3 (got {spec.level!r}); resolve 'F' before building specs")
    return f"V{r}.{spec.level}"


def _c_round2(spec: EpisodeSpec, texts: "ScenarioTexts") -> tuple[str, str, bool]:
    """(line_id, text, is_private) of C's round-2 post for the cell."""
    c2 = spec.cell.c2
    if c2 in ("C2.R", "C2.CN", "C2.OBJ"):
        return c2, texts.lines[c2], False
    if c2 in _CLAIM_C2:
        idx = spec.paraphrase_idx
        text = texts.claims[idx]
        line_id = f"C2.P{idx + 1}"
        private = idx != 0
        if c2 == "CLAIM+FORGED":
            return line_id + "+FORGED", text + "\n" + texts.forged_line, True
        return line_id, text, private
    raise ValueError(f"unknown c2 value {c2!r}")


def scripted_posts(spec: EpisodeSpec, texts: "ScenarioTexts", round: int) -> list[ScriptedPost]:
    """B, V and C posts of `round`, in stamp order."""
    _check_round(round)
    who = roster(spec)
    shift = 30 * (round - 1)
    vid = _v_line_id(spec, round)
    if round == 2:
        cid, ctext, cpriv = _c_round2(spec, texts)
    else:
        cid = f"C{round}"
        ctext, cpriv = texts.lines[cid], False
    return [
        ScriptedPost(round, who["B"], _hhmm(clock_min(_B_STAMP) + shift), f"B{round}", texts.lines[f"B{round}"], False),
        ScriptedPost(round, who["V"], _hhmm(clock_min(_V_STAMP) + shift), vid, texts.lines[vid], False),
        ScriptedPost(round, who["C"], _hhmm(clock_min(_C_STAMP) + shift), cid, ctext, cpriv),
    ]


def scripted_note(spec: EpisodeSpec, texts: "ScenarioTexts") -> ScriptedNote | None:
    """C's real note (cells with c_note: RECEIPT, POS), filed before the round-2 digest is rendered."""
    if not spec.cell.c_note:
        return None
    subject, message = texts.c_note
    return ScriptedNote(C_NOTE_ROUND, roster(spec)["C"], C_NOTE_STAMP, subject, message,
                        scripted_visible_from(C_NOTE_ROUND))


# ---- tickets --------------------------------------------------------------------------------------------

def tickets_for(spec: EpisodeSpec, texts: "ScenarioTexts", round: int, slot: int) -> tuple[str, str, str]:
    _check_round(round)
    if slot not in SLOTS:
        raise ValueError("slot must be 1, 2 or 3")
    letter = spec.ticket_sets[slot - 1]
    return tuple(texts.tickets[f"R{round}{letter}{n}"] for n in (1, 2, 3))  # type: ignore[return-value]


def ticket_ids(lines: tuple[str, ...]) -> tuple[str, ...]:
    """The id at the start of each ticket line (text before the first ' · ')."""
    out = []
    for line in lines:
        head, sep, _ = line.partition(" · ")
        if not sep or not head or " " in head:
            raise ValueError("ticket line does not start with '<id> · '")
        out.append(head)
    return tuple(out)


# ---- database side --------------------------------------------------------------------------------------

def inject_round(db: "Db", spec: EpisodeSpec, episode_id: int, attempt: int, round: int,
                 texts: "ScenarioTexts", run_id: str) -> None:
    """Idempotent: INSERT OR IGNORE the round's scripted posts; in round 2 of a c_note cell, file C's note through
    handlers.notify_ops (source='script'; the handler skips it if it already exists)."""
    posts = scripted_posts(spec, texts, round)
    now = time.time()
    vis = scripted_visible_from(round)

    def fn(conn: sqlite3.Connection) -> None:
        for p in posts:
            conn.execute(
                "INSERT OR IGNORE INTO posts(episode_id, attempt, round, sender, slot, clock, clock_min, kind, "
                "line_id, is_private, text, text_raw, was_cut, was_collapsed, visible_from_round, turn_id, "
                "tool_call_id, created_at) VALUES (?,?,?,?,NULL,?,?,'scripted',?,?,?,NULL,0,0,?,NULL,NULL,?)",
                (episode_id, attempt, p.round, p.sender, p.clock, clock_min(p.clock), p.line_id,
                 1 if p.is_private else 0, p.text, vis, now))

    db.write(fn)
    note = scripted_note(spec, texts) if round == C_NOTE_ROUND else None
    if note is not None:
        from wtth import handlers  # late import: handlers is owned by WP2 and imports types only

        toolset = "backup" if spec.scenario == "backup" and spec.cell.toolset == "main" else spec.cell.toolset
        ident = Identity(
            run_id=run_id, episode_id=episode_id, attempt=attempt, round=note.round, slot=0, sender=note.sender,
            clock=note.clock, clock_min=clock_min(note.clock), visible_from_round=note.visible_from_round,
            world=spec.cell.world, source="script", turn_id=None, toolset=toolset, ticket_ids=())
        handlers.notify_ops(db, ident, {"subject": note.subject, "message": note.message}, Caps())


def _review_entries(raw: str) -> tuple[tuple[str, str, str], ...]:
    """entries_json is a list of [id, decision, comment]; tolerate dict items and short lists."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    out: list[tuple[str, str, str]] = []
    if not isinstance(data, list):
        return ()
    for e in data:
        if isinstance(e, dict):
            vals = [e.get("id", e.get("entry_id", e.get("ticket_id", ""))), e.get("decision", ""), e.get("comment", "")]
        elif isinstance(e, (list, tuple)):
            vals = list(e) + [""] * (3 - len(e))
        else:
            continue
        out.append(tuple(v if isinstance(v, str) else "" for v in vals[:3]))  # type: ignore[arg-type]
    return tuple(out)


def load_state(conn: sqlite3.Connection, spec: EpisodeSpec, episode_id: int, attempt: int) -> EpisodeState:
    """Every row of (episode, attempt); no other attempt is ever read."""
    key = (episode_id, attempt)
    posts = tuple(PostRow(*r) for r in conn.execute(
        "SELECT id, round, sender, slot, clock, clock_min, kind, line_id, text, visible_from_round "
        "FROM posts WHERE episode_id=? AND attempt=? ORDER BY id", key).fetchall())
    notes = tuple(NoteRow(*r) for r in conn.execute(
        "SELECT id, round, sender, slot, clock, clock_min, subject, message, source, visible_from_round "
        "FROM notes WHERE episode_id=? AND attempt=? ORDER BY id", key).fetchall())
    log = tuple(LogRow(*r) for r in conn.execute(
        "SELECT id, note_id, sender, clock, clock_min, subject_display, visible_from_round "
        "FROM escalation_log WHERE episode_id=? AND attempt=? ORDER BY id", key).fetchall())
    reviews = tuple(ReviewRow(r[0], r[1], r[2], _review_entries(r[3]), bool(r[4])) for r in conn.execute(
        "SELECT id, round, sender, entries_json, used FROM reviews WHERE episode_id=? AND attempt=? ORDER BY id",
        key).fetchall())
    d1 = {int(r[0]): r[1] for r in conn.execute(
        "SELECT slot, answer_text FROM debrief_answers WHERE episode_id=? AND attempt=? AND question='D1'",
        key).fetchall()}
    return EpisodeState(spec, episode_id, attempt, posts, notes, log, reviews, d1)
