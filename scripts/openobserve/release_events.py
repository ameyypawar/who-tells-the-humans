"""Records of the D-027 receipts monitor, built from data/release only (stdlib only).

Four record sets, one per OpenObserve stream:

  wtth_posts   every board post, scripted and live, with its final labels and the monitor's claim fields
  wtth_notes   every accepted notify_ops record: the operator-side truth of who told ops, and when
  wtth_log     every #escalation-log line of the world-R1 shifts: the channel-written receipts
  wtth_shifts  one row per shift, from outcomes_per_shift.csv

Time. Every shift is placed on the same synthetic date, BASE_DATE (UTC). A record's `_timestamp` is BASE_DATE plus its
shift-clock stamp (HH:MM, as released) plus `seq` seconds. `seq` orders the actions of one witness turn: it is the
1-based position of the tool call in the turn (a turn has at most four). Scripted posts and C's scripted note have
seq 0, and no two of them share a minute with each other or with a witness turn. So within a shift the records are in
the order in which they happened, and a note and a post of the same turn are ordered as the agent made them.

Claims. `claim_present` and `claim_subject` are the final labels (labels_final.csv) of the judged live posts. Scripted
posts were never judged; for them the design fixes the value: C's round-2 claim line (line_id C2.P1, C2.P2 or C2.P3,
with or without +FORGED) is claim_present = 1, claim_subject = self, and every other scripted line is claim_present = 0
(label_source = design). Live posts of the follow-up (phase exp1) were not judged (D-023): they carry no claim label
(label_source = none) and the monitor cannot see a claim in them.

Claimed reporters (who the post says told ops), for posts with claim_present = 1:
  claim_subject = self   the post's author (C, rd-11, for C's own claim line);
  claim_subject = other  every handle rd-NN in the text except the author's own and the violator's (rd-09);
                         if the text names none, the claim is unattributed ("ops has been told", "already escalated").
"""
from __future__ import annotations

import csv
import gzip
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RELEASE = REPO / "data" / "release"

BASE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)   # synthetic; carries no meaning beyond ordering
BASE_US = int(BASE_DATE.timestamp()) * 1_000_000

CLAIMANT, VIOLATOR, BACKGROUND = "rd-11", "rd-09", "rd-04"
ROLES = {CLAIMANT: "C", VIOLATOR: "V", BACKGROUND: "B"}
HANDLE_RE = re.compile(r"\brd-\d{2}\b")
C_CLAIM_LINE_RE = re.compile(r"^C2\.P[123](\+FORGED)?$")

# Design truth of C's round-2 claim (PREREG section on cells; DEVIATIONS D-022 for the X-FORGED cells).
FALSE_CLAIM_CELLS = ("CLAIM", "POLICY", "NO-RECEIPT", "FORGED", "X-FORGED-POLICY", "X-FORGED-PROV")
TRUE_CLAIM_CELLS = ("RECEIPT", "POS")

# Shift clock (prereg/prompts/scripted_lines.md section 1): round r's digest is rendered at 21:00 + 30 (r - 1) minutes
# and the witness in slot s acts at that time + 4 s minutes; the last witness action of a shift is round 5, slot 3.
SHIFT_START_CLOCK = "20:30"
SHIFT_END_CLOCK = "23:12"
ROUNDS = 5

CELL_ORDER = ("NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS",
              "B-NONE", "X-FORGED-POLICY", "X-FORGED-PROV")
PHASE_ORDER = ("core", "sonnet", "exp1")

POST_LABELS = ("claim_present", "addressed_to_humans", "escalation_to_humans", "concern", "about_violation",
               "uncertain")
NOTE_LABELS = ("kw_match", "refers_to_violation", "names_rd09", "mentions_claim_or_log", "alleges_misconduct")
SHIFT_INT_COLS = ("episode_id", "attempt", "seed", "n_witnesses", "y", "p2", "p2_pending", "p2prime", "p2prime_lo",
                  "p2prime_hi", "n_posts_unresolved", "alert_load", "any_note", "first_round", "first_round_any",
                  "truth_any", "truth_viol", "concern_episode", "c_note")


def model_short(model: str) -> str:
    """'claude-haiku-5-5' or 'haiku' -> 'haiku'."""
    for m in ("haiku", "sonnet"):
        if m in model:
            return m
    raise ValueError(f"unknown model {model!r}")


def arm_of(phase: str, model: str, cell: str) -> str:
    """Display label of a cell in one arm, e.g. 'CLAIM (Haiku)', 'RECEIPT (Sonnet, exp1)'."""
    m = model_short(model).capitalize()
    return f"{cell} ({m}, exp1)" if phase == "exp1" and cell == "RECEIPT" else f"{cell} ({m})"


def arm_order(phase: str, cell: str) -> int:
    """Sort key: phase first (core, sonnet, exp1), then the design order of the cells."""
    return PHASE_ORDER.index(phase) * 100 + CELL_ORDER.index(cell)


def c_claim_truth(cell: str) -> str:
    return "false" if cell in FALSE_CLAIM_CELLS else "true" if cell in TRUE_CLAIM_CELLS else "none"


def clock_minutes(clock: str) -> int:
    h, m = clock.split(":")
    return int(h) * 60 + int(m)


def ts_us(clock: str, seq: int = 0) -> int:
    return BASE_US + (clock_minutes(clock) * 60 + seq) * 1_000_000


def _int_or_none(v: str | None) -> int | None:
    return None if v in (None, "") else int(v)


def claimed_reporters(author: str, claim_subject: str, text: str) -> list[str]:
    """Agents the post says told ops (see the module docstring); [] for an unattributed claim."""
    if claim_subject == "self":
        return [author]
    return [h for h in dict.fromkeys(HANDLE_RE.findall(text)) if h not in (author, VIOLATOR)]


@dataclass
class Release:
    posts: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    shifts: list[dict] = field(default_factory=list)
    witnesses: list[dict] = field(default_factory=list)

    def streams(self) -> dict[str, list[dict]]:
        return {"wtth_posts": self.posts, "wtth_notes": self.notes, "wtth_log": self.log, "wtth_shifts": self.shifts}


def _turn_seq(episode: dict) -> dict[tuple[str, str, int], int]:
    """(turn_id, tool, k) -> 1-based position in the turn of the k-th accepted call of that tool."""
    seq: dict[tuple[str, str, int], int] = {}
    for t in episode["turns"]:
        seen: Counter[str] = Counter()
        for pos, call in enumerate(t["tool_calls"], start=1):
            if call["accepted"]:
                seq[(t["turn_id"], call["tool"], seen[call["tool"]])] = pos
                seen[call["tool"]] += 1
    return seq


def load(release_dir: Path = RELEASE) -> Release:
    with (release_dir / "labels_final.csv").open(newline="", encoding="utf-8") as f:
        labels = {(r["source_table"], int(r["source_id"])): r for r in csv.DictReader(f)
                  if r["source_table"] in ("posts", "notes")}
    out = Release()
    with gzip.open(release_dir / "transcripts_masked.jsonl.gz", "rt", encoding="utf-8") as f:
        episodes = [json.loads(line) for line in f]
    for e in episodes:
        _episode(e, labels, out)
    with (release_dir / "outcomes_per_shift.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rec: dict = {k: (_int_or_none(v) if k in SHIFT_INT_COLS else v) for k, v in r.items()}
            rec.update(_timestamp=ts_us(SHIFT_START_CLOCK), model=model_short(r["model"]),
                       arm=arm_of(r["phase"], r["model"], r["cell"]), arm_order=arm_order(r["phase"], r["cell"]),
                       c_claim=c_claim_truth(r["cell"]), untold=int(int(r["y"]) == 0),
                       p1_rate=int(r["y"]) / int(r["n_witnesses"]))
            out.shifts.append(rec)
    with (release_dir / "outcomes_per_witness.csv").open(newline="", encoding="utf-8") as f:
        out.witnesses = list(csv.DictReader(f))
    return out


def _episode(e: dict, labels: dict, out: Release) -> None:
    phase, cell, model = e["phase"], e["cell"], model_short(e["model"])
    keys = {"episode_id": e["episode_id"], "attempt": e["attempt"], "phase": phase, "model": model, "cell": cell,
            "world": e["world"], "seed": e["seed"], "arm": arm_of(phase, model, cell),
            "arm_order": arm_order(phase, cell), "c_claim": c_claim_truth(cell)}
    seq = _turn_seq(e)
    k_post: Counter[str] = Counter()
    for p in sorted(e["board_posts"], key=lambda p: p["id"]):
        author = p["sender"]
        rec = dict(keys, post_id=p["id"], round=p["round"], clock=p["clock"], slot=p["slot"] or 0, kind=p["kind"],
                   line_id=p["line_id"] or "", author=author, author_role=ROLES.get(author, "witness"),
                   is_private=p["is_private"], was_cut=p["was_cut"], was_collapsed=p["was_collapsed"],
                   visible_from_round=p["visible_from_round"], turn_id=p["turn_id"] or "", text=p["text"])
        if p["kind"] == "scripted":
            s = 0
            is_claim = bool(C_CLAIM_LINE_RE.match(p["line_id"] or ""))
            rec.update(label_source="design", claim_present=int(is_claim), claim_subject="self" if is_claim else "")
        else:
            s = seq[(p["turn_id"], "post_to_board", k_post[p["turn_id"]])]
            k_post[p["turn_id"]] += 1
            lab = labels.get(("posts", p["id"]))
            if lab is None:
                rec.update(label_source="none", claim_subject="")
            else:
                rec.update(label_source=lab["label_source"], claim_subject=lab["claim_subject"] or "")
                rec.update({k: _int_or_none(lab[k]) for k in POST_LABELS})
        rec.update(seq=s, _timestamp=ts_us(p["clock"], s))
        if rec.get("claim_present") == 1:
            rep = claimed_reporters(author, rec["claim_subject"], p["text"])
            attribution = "author" if rec["claim_subject"] == "self" else ("named" if rep else "unattributed")
        else:
            rep, attribution = [], ""
        rec.update(claimed_reporters=",".join(rep), n_claimed_reporters=len(rep), claim_attribution=attribution)
        out.posts.append(rec)

    k_note: Counter[str] = Counter()
    note_ts: dict[tuple[str, str, str], list[dict]] = {}
    for n in sorted(e["notes"], key=lambda n: n["id"]):
        if n["source"] == "script":
            s = 0
        else:
            s = seq[(n["turn_id"], "notify_ops", k_note[n["turn_id"]])]
            k_note[n["turn_id"]] += 1
        rec = dict(keys, note_id=n["id"], round=n["round"], clock=n["clock"], slot=n["slot"] or 0,
                   sender=n["sender"], sender_role=ROLES.get(n["sender"], "witness"), source=n["source"],
                   visible_from_round=n["visible_from_round"], turn_id=n["turn_id"] or "", seq=s,
                   _timestamp=ts_us(n["clock"], s), subject=n["subject"], message=n["message"])
        lab = labels.get(("notes", n["id"]))
        rec.update({k: (_int_or_none(lab[k]) if lab else None) for k in NOTE_LABELS})
        out.notes.append(rec)
        note_ts.setdefault((n["sender"], n["clock"], n["subject"]), []).append(rec)

    for line in sorted(e["escalation_log"], key=lambda x: x["ref_no"]):
        # The log is rendered from the notes table (PREREG): each line is one note, matched on sender, stamp, subject.
        match = note_ts[(line["sender"], line["clock"], line["subject"])].pop(0)
        out.log.append(dict(keys, ref_no=line["ref_no"], entry=f"E-{line['ref_no']:04d}", clock=line["clock"],
                            sender=line["sender"], subject=line["subject"],
                            visible_from_round=line["visible_from_round"],
                            seen_by_witnesses=int(line["visible_from_round"] <= ROUNDS),
                            note_id=match["note_id"], _timestamp=match["_timestamp"]))
