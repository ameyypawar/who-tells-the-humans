"""Pure tool handlers over Db + Identity, and the fault sink (research/41 section 2.3.3). Owner: WP2.

Every call that reaches the server writes one tool_calls row in the same BEGIN IMMEDIATE transaction as its
content rows. The reply is always the toolset's fixed string; it never depends on args, accept decision,
cell, round or DB success. Identity comes only from `Identity` (env), never from args.

D-028 (exp2) adds the receipts store (wtth.receipts), passed in as `receipts` (a store bound to the shift):
- in a receipts cell, notify_ops also writes each accepted note to the store and returns its fixed reply only once the
  record is searchable;
- lookup_escalation (toolset main_check only) is the one tool whose reply depends on the call: the store's matching
  entries in the log's line format, or the no-match reply. The reply sent is recorded in the call's tool_calls row
  (flags_json {"lookup": {..., "reply": ...}}), and the runner checks the stream against that record.
A store failure raises ToolUnavailable: the server answers with an MCP error (never a success reply) and a fault line,
which makes the turn a technical failure. There is no fallback.
"""
from __future__ import annotations

import functools
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wtth import receipts as receipts_mod
from wtth.textnorm import canon_json, kw_hits, normalize_note, normalize_post
from wtth.types import Caps, Identity, Toolset, World

if TYPE_CHECKING:
    from wtth.db import Db

DECISIONS = frozenset({"approve", "decline", "hold"})
_DEFAULT_CAPS = Caps()
LOOKUP_MARGIN_S = 5.0   # D-028: a log row this much older than the store's confirm window must be in the store


class ToolUnavailable(Exception):
    """D-028: the receipts store failed (`kind` is a short label). dispatch records the fault and re-raises; the server
    then returns an MCP error result instead of a reply."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


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


def notify_ops(db: "Db", ident: Identity, args: dict, caps: Caps, receipts: Any = None) -> None:
    """Raises WriteFailed. The orchestrator also calls it directly for the scripted note (source='script').
    D-028: with `receipts`, an accepted tool note that wrote a log line is also written to the store, and this returns
    only once the record is searchable; a store failure raises ToolUnavailable (the note row is already committed, and
    the fault fails the attempt)."""
    raw_subject, raw_message = args.get("subject"), args.get("message")
    accepted, subject, message, flags = normalize_note(raw_subject, raw_message, caps.subject_chars,
                                                       caps.message_chars)
    reject = None if accepted else ("bad_type" if flags.get("bad_type") else "empty")
    hits = canon_json(kw_hits(subject, message, _keywords(ident))) if accepted else None
    script = ident.source == "script"
    if getattr(ident, "receipts", False) and receipts is None and not script:
        raise ToolUnavailable("no_store")

    def fn(conn: sqlite3.Connection) -> tuple[int, int | None, int] | None:
        if script:
            if not accepted:
                return None
            dup = conn.execute("SELECT 1 FROM notes WHERE episode_id=? AND attempt=? AND sender=? "
                               "AND source='script' LIMIT 1", (ident.episode_id, ident.attempt, ident.sender)).fetchone()
            if dup:
                return None
            tc = None
        else:
            tc = _insert_tool_call(conn, ident, "notify_ops", args, accepted, reject, flags)
            if not accepted:
                return None
        now = time.time()
        cur = conn.execute(
            "INSERT INTO notes(episode_id, attempt, round, sender, slot, clock, clock_min, subject, message, "
            "subject_raw, message_raw, source, turn_id, tool_call_id, visible_from_round, created_at, kw_hits_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident.episode_id, ident.attempt, ident.round, ident.sender, ident.slot, ident.clock, ident.clock_min,
             subject, message, raw_subject, raw_message, "script" if script else "tool", ident.turn_id, tc,
             ident.visible_from_round, now, hits))
        if ident.world == World.R1:
            note_id = cur.lastrowid
            conn.execute(
                "INSERT INTO escalation_log(note_id, episode_id, attempt, sender, clock, clock_min, subject_display, "
                "visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (note_id, ident.episode_id, ident.attempt, ident.sender, ident.clock, ident.clock_min,
                 subject, ident.visible_from_round, now))
            if receipts is not None and not script:   # the line's number now (render.number_log's rule)
                before = conn.execute(
                    "SELECT COUNT(*) FROM escalation_log WHERE episode_id=? AND attempt=? AND "
                    "(clock_min<? OR (clock_min=? AND note_id<?))",
                    (ident.episode_id, ident.attempt, ident.clock_min, ident.clock_min, note_id)).fetchone()[0]
                return note_id, tc, receipts_mod.FIRST_REF + int(before)
        return None

    out = db.write(fn)
    if receipts is None or out is None:
        return
    note_id, tc, ref = out
    rec = receipts_mod.make_record(ident, episode_key=receipts.episode_key, note_id=note_id, tool_call_id=tc,
                                   entry_ref=ref, subject=subject)
    try:
        receipts.write(rec)
        waited, reads = receipts.confirm(note_id)
    except receipts_mod.ReceiptStoreError as exc:
        raise ToolUnavailable(exc.kind) from None
    _log_event(db, ident, "receipt_confirmed", {"note_id": note_id, "entry_id": rec["entry_id"],
                                                "ms": round(waited * 1000.0, 1), "reads": reads})


def _log_event(db: "Db", ident: Identity, kind: str, detail: dict) -> None:
    """D-028: an events row (insert-only) for the store's timings. Never raises."""
    def fn(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO events(ts, run_id, level, kind, episode_id, attempt, turn_id, detail_json) "
                     "VALUES (?,?,?,?,?,?,?,?)", (time.time(), ident.run_id, "info", kind, ident.episode_id,
                                                  ident.attempt, ident.turn_id, canon_json(detail)))
    try:
        db.write(fn)
    except Exception:  # noqa: BLE001 - a timing row must never fail the call
        pass


def lookup_escalation(db: "Db", ident: Identity, args: dict, receipts: Any) -> str:
    """D-028: the shift's store records matching `args`, as log lines, or the no-match reply. The reply comes from the
    store alone; the main database is read only to check the store (wtth.receipts.cross_check). Writes one
    tool_calls row; raises ToolUnavailable on any store failure or check failure."""
    if receipts is None:
        raise ToolUnavailable("no_store")
    unknown = sorted(k for k in args if k not in ("entry_id", "sender"))
    t_query = time.time()
    try:
        records = receipts.fetch()
    except receipts_mod.ReceiptStoreError as exc:
        _lookup_row(db, ident, args, False, "store_error", {"error": exc.kind})
        raise ToolUnavailable(exc.kind) from None
    reply, info = receipts_mod.lookup_reply(records, args)
    rows = db.read(lambda c: c.execute(
        "SELECT note_id, sender, clock, clock_min, subject_display, created_at FROM escalation_log "
        "WHERE episode_id=? AND attempt=?", (ident.episode_id, ident.attempt)).fetchall())
    problems = receipts_mod.cross_check(records, rows, t_query,
                                        float(receipts.confirm_timeout_s) + LOOKUP_MARGIN_S)
    if problems:
        _lookup_row(db, ident, args, False, "receipt_check", {"problems": problems})
        raise ToolUnavailable("receipt_check")
    if unknown:
        info["unknown_args"] = unknown
    _lookup_row(db, ident, args, True, None, {"lookup": {**info, "reply": reply}})
    return reply


def _lookup_row(db: "Db", ident: Identity, args: dict, accepted: bool, reject: str | None, flags: dict) -> None:
    def fn(conn: sqlite3.Connection) -> None:
        _insert_tool_call(conn, ident, "lookup_escalation", args, accepted, reject, flags)
    db.write(fn)


def _unknown_tool(db: "Db", ident: Identity, name: str, args: dict) -> None:
    def fn(conn: sqlite3.Connection) -> None:
        _insert_tool_call(conn, ident, name, args, False, "unknown_tool", {})
    db.write(fn)


def dispatch(db: "Db", ident: Identity, toolset: Toolset, name: str, args: dict, fault: FaultSink,
             caps: Caps | None = None, receipts: Any = None) -> str:
    """Returns the toolset's fixed reply (lookup_escalation: its computed reply). Never raises, except
    ToolUnavailable (D-028: the receipts store failed), after its fault line is written. Owner: WP2."""
    # Note: an unknown tool name has no reply of its own; the first tool's reply is returned.
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
            notify_ops(db, ident, args, caps, receipts=receipts)
        elif name == "lookup_escalation":
            reply = lookup_escalation(db, ident, args, receipts)
        else:
            fault.record(name, args, "no_handler", ident)
    except ToolUnavailable as exc:
        fault.record(str(name), args if isinstance(args, dict) else {}, f"receipts:{exc.kind}", ident)
        raise
    except Exception as exc:  # noqa: BLE001 - nothing may reach the model
        err = getattr(exc, "error_class", None) or type(exc).__name__
        fault.record(str(name), args if isinstance(args, dict) else {}, str(err), ident)
        # D-028: a receipts call that failed for any reason never gets a success reply (no false "No matching entry.")
        if name == "lookup_escalation" or (name == "notify_ops" and getattr(ident, "receipts", False)):
            raise ToolUnavailable(str(err)) from None
    return reply
