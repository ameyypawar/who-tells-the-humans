"""Pure tool handlers over Db + Identity, and the fault sink (research/41 section 2.3.3). Owner: WP2.

Every call that reaches the server writes one tool_calls row in the same BEGIN IMMEDIATE transaction as its
content rows. The reply is always the toolset's fixed string; it never depends on args, accept decision,
cell, round or DB success. Identity comes only from `Identity` (env), never from args.
"""
from __future__ import annotations

import functools
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wtth.textnorm import canon_json, kw_hits, normalize_note, normalize_post
from wtth.types import Caps, Identity, Toolset, World

if TYPE_CHECKING:
    from wtth.db import Db

DECISIONS = frozenset({"approve", "decline", "hold"})
_DEFAULT_CAPS = Caps()


# ---- fault sink ---------------------------------------------------------------------------------------

class FaultSink:
    """Appends one JSON line per fault to {fault_dir}/{turn_id}.jsonl (0600, fsync). Never raises."""

    def __init__(self, fault_dir: Path, turn_id: str | None) -> None:
        self.fault_dir = Path(fault_dir)
        self.turn_id = turn_id
        self.path = self.fault_dir / f"{turn_id if turn_id else 'no-turn'}.jsonl"
        self._tripped = False

    def record(self, tool: str, args: dict, error: str, ident: Identity | None) -> None:
        self._tripped = True
        line: dict[str, Any] = {"ts": time.time(), "pid": os.getpid(), "tool": tool, "error": error,
                                "turn_id": self.turn_id, "args": args}
        if ident is not None:
            line.update({"run_id": ident.run_id, "episode_id": ident.episode_id, "attempt": ident.attempt,
                         "round": ident.round, "slot": ident.slot, "sender": ident.sender})
        try:
            try:
                text = canon_json(line)
            except (TypeError, ValueError):
                text = json.dumps(line, ensure_ascii=True, sort_keys=True, default=repr)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (text + "\n").encode("utf-8", "backslashreplace"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:  # noqa: BLE001 - the fault sink must never raise into the tool path
            import sys
            try:
                sys.stderr.write(f"wtth.handlers: could not write fault file {self.path}\n")
            except Exception:  # noqa: BLE001
                pass

    @property
    def tripped(self) -> bool:
        return self._tripped


# ---- helpers ------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _settings():
    from wtth.config import load_settings
    return load_settings()


def _keywords(ident: Identity) -> tuple[str, ...]:
    """P1 keyword list for the episode's scenario, derived from the toolset (backup -> backup, else main)."""
    scenario = "backup" if ident.toolset == "backup" else "main"
    return _settings().keywords(scenario)


def _args_json(args: dict) -> str:
    try:
        return canon_json(args)
    except (TypeError, ValueError):
        return json.dumps(args, ensure_ascii=True, sort_keys=True, default=repr)


def _insert_tool_call(conn: sqlite3.Connection, ident: Identity, tool: str, args: dict, accepted: bool,
                      reject_reason: str | None, flags: dict) -> int:
    cur = conn.execute(
        "INSERT INTO tool_calls(turn_id, episode_id, attempt, round, slot, sender, tool, args_json, accepted, "
        "reject_reason, flags_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ident.turn_id, ident.episode_id, ident.attempt, ident.round, ident.slot, ident.sender, tool,
         _args_json(args), 1 if accepted else 0, reject_reason, canon_json(flags) if flags else None, time.time()))
    return cur.lastrowid


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


# ---- handlers -----------------------------------------------------------------------------------------

def submit_reviews(db: "Db", ident: Identity, toolset: Toolset, args: dict) -> None:
    flags: dict[str, Any] = {}
    raw = args.get("reviews")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            raw = parsed
            flags["pre_parsed"] = True

    id_field = toolset.id_field
    entries: list[list[str]] = []
    reject: str | None = None
    if not isinstance(raw, list):
        reject = "not_a_list"
    else:
        skipped = bad_fields = 0
        for item in raw:
            if not isinstance(item, dict):
                skipped += 1
                continue
            row = []
            for key in (id_field, "decision", "comment"):
                v = item.get(key)
                if not isinstance(v, str):
                    if key in item:
                        bad_fields += 1
                    v = ""
                row.append(v)
            entries.append(row)
        if skipped:
            flags["non_dict_entries"] = skipped
        if bad_fields:
            flags["non_str_fields"] = bad_fields
        known = set(ident.ticket_ids)
        ids = [e[0].strip() for e in entries]
        unknown = sorted({i for i in ids if i not in known})
        missing = [t for t in ident.ticket_ids if t not in set(ids)]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        bad_dec = sorted({e[1] for e in entries if e[1].strip().lower() not in DECISIONS})
        no_comment = sum(1 for e in entries if not e[2].strip())
        if unknown:
            flags["unknown_ids"] = unknown
        if missing:
            flags["missing_ids"] = missing
        if dupes:
            flags["duplicate_ids"] = dupes
        if bad_dec:
            flags["bad_decisions"] = bad_dec
        if no_comment:
            flags["missing_comment"] = no_comment

    def fn(conn: sqlite3.Connection) -> None:
        f = dict(flags)
        if reject is not None:
            _insert_tool_call(conn, ident, "submit_reviews", args, False, reject, f)
            return
        earlier = conn.execute(
            "SELECT 1 FROM reviews WHERE episode_id=? AND attempt=? AND round=? AND sender=? AND used=1 LIMIT 1",
            (ident.episode_id, ident.attempt, ident.round, ident.sender)).fetchone()
        used = 0 if earlier else 1
        if not used:
            f["not_first"] = True
        tc = _insert_tool_call(conn, ident, "submit_reviews", args, True, None, f)
        conn.execute(
            "INSERT INTO reviews(tool_call_id, episode_id, attempt, round, slot, sender, entries_json, n_entries, "
            "used, flags_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tc, ident.episode_id, ident.attempt, ident.round, ident.slot, ident.sender, canon_json(entries),
             len(entries), used, canon_json(f) if f else None, time.time()))

    db.write(fn)


def post_to_board(db: "Db", ident: Identity, args: dict, caps: Caps) -> None:
    raw = args.get("message")
    probe = "" if raw is None else raw
    text, flags = normalize_post(probe, caps.post_chars)
    reject = None if text is not None else ("bad_type" if flags.get("bad_type") else "empty")

    def fn(conn: sqlite3.Connection) -> None:
        tc = _insert_tool_call(conn, ident, "post_to_board", args, text is not None, reject, flags)
        if text is None:
            return
        conn.execute(
            "INSERT INTO posts(episode_id, attempt, round, sender, slot, clock, clock_min, kind, line_id, "
            "is_private, text, text_raw, was_cut, was_collapsed, visible_from_round, turn_id, tool_call_id, "
            "created_at) VALUES (?,?,?,?,?,?,?,'live',NULL,0,?,?,?,?,?,?,?,?)",
            (ident.episode_id, ident.attempt, ident.round, ident.sender, ident.slot, ident.clock, ident.clock_min,
             text, probe, 1 if flags.get("cut") else 0, 1 if flags.get("collapsed") else 0,
             ident.visible_from_round, ident.turn_id, tc, time.time()))

    db.write(fn)


def notify_ops(db: "Db", ident: Identity, args: dict, caps: Caps) -> None:
    """Raises WriteFailed. The orchestrator also calls it directly for the scripted note (source='script')."""
    raw_subject, raw_message = args.get("subject"), args.get("message")
    accepted, subject, message, flags = normalize_note(raw_subject, raw_message, caps.subject_chars,
                                                       caps.message_chars)
    reject = None if accepted else ("bad_type" if flags.get("bad_type") else "empty")
    hits = canon_json(kw_hits(subject, message, _keywords(ident))) if accepted else None
    script = ident.source == "script"

    def fn(conn: sqlite3.Connection) -> None:
        if script:
            if not accepted:
                return
            dup = conn.execute("SELECT 1 FROM notes WHERE episode_id=? AND attempt=? AND sender=? "
                               "AND source='script' LIMIT 1", (ident.episode_id, ident.attempt, ident.sender)).fetchone()
            if dup:
                return
            tc = None
        else:
            tc = _insert_tool_call(conn, ident, "notify_ops", args, accepted, reject, flags)
            if not accepted:
                return
        now = time.time()
        cur = conn.execute(
            "INSERT INTO notes(episode_id, attempt, round, sender, slot, clock, clock_min, subject, message, "
            "subject_raw, message_raw, source, turn_id, tool_call_id, visible_from_round, created_at, kw_hits_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident.episode_id, ident.attempt, ident.round, ident.sender, ident.slot, ident.clock, ident.clock_min,
             subject, message, raw_subject, raw_message, "script" if script else "tool", ident.turn_id, tc,
             ident.visible_from_round, now, hits))
        if ident.world == World.R1:
            conn.execute(
                "INSERT INTO escalation_log(note_id, episode_id, attempt, sender, clock, clock_min, subject_display, "
                "visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (cur.lastrowid, ident.episode_id, ident.attempt, ident.sender, ident.clock, ident.clock_min,
                 subject, ident.visible_from_round, now))

    db.write(fn)


def _unknown_tool(db: "Db", ident: Identity, name: str, args: dict) -> None:
    def fn(conn: sqlite3.Connection) -> None:
        _insert_tool_call(conn, ident, name, args, False, "unknown_tool", {})
    db.write(fn)


def dispatch(db: "Db", ident: Identity, toolset: Toolset, name: str, args: dict, fault: FaultSink,
             caps: Caps | None = None) -> str:
    """Never raises; always returns the toolset's fixed reply. Owner: WP2."""
    # LEAD-REVIEW: an unknown tool name has no reply of its own; the first tool's reply is returned.
    reply = toolset.tools[0].reply if toolset.tools else ""
    try:
        if not isinstance(args, dict):
            args = {}
        caps = caps or _DEFAULT_CAPS
        spec = next((t for t in toolset.tools if t.name == name), None)
        if spec is None:
            fault.record(str(name), args, "unknown_tool", ident)
            _unknown_tool(db, ident, str(name), args)
            return reply
        reply = spec.reply
        if name == "submit_reviews":
            submit_reviews(db, ident, toolset, args)
        elif name == "post_to_board":
            post_to_board(db, ident, args, caps)
        elif name == "notify_ops":
            notify_ops(db, ident, args, caps)
        else:
            fault.record(name, args, "no_handler", ident)
    except Exception as exc:  # noqa: BLE001 - nothing may reach the model
        err = getattr(exc, "error_class", None) or type(exc).__name__
        fault.record(str(name), args if isinstance(args, dict) else {}, str(err), ident)
    return reply
