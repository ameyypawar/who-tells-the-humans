"""Reader of the author's transcript read (DEVIATIONS D-021 item 3). Post hoc; counts only, never transcript text.

Files (written by the annotation side, read here): <dir>/selection.csv, then <dir>/answers.csv, with the columns
item_id, episode_id, witness, cell, stratum, told_ops, mentioned_split. stratum is one of claim_never, forged_untold,
noreceipt_never, control; told_ops and mentioned_split are yes / no. `witness` is the slot (1 to 3) or the handle.
answers.csv may carry only item_id and the two answers: the rest is then taken from selection.csv.

Status (macro HumanReadStatus): 'not run' (neither file), 'selected' (selection only), 'partial' (some selected item
without both answers), 'complete'. Counts are emitted once answers.csv exists.
"""
from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wtth.analysis.outcomes import WitnessOutcome

POSTHOC = "post hoc (D-021)"
STRATA = ("claim_never", "forged_untold", "noreceipt_never", "control")
STRATUM_TOKENS = {"claim_never": "ClaimNever", "forged_untold": "ForgedUntold", "noreceipt_never": "NoReceiptNever",
                  "control": "Control"}
NOT_RUN, SELECTED, PARTIAL, COMPLETE = "not run", "selected", "partial", "complete"


@dataclass(frozen=True, slots=True)
class StratumCounts:
    n: int = 0                  # answered items (both questions yes / no)
    told_yes: int = 0
    told_no: int = 0
    mentioned_yes: int = 0
    mentioned_no: int = 0
    p1_yes: int = 0             # automated P1 of the same witnesses


@dataclass(frozen=True, slots=True)
class HumanReadResults:
    status: str
    n_selected: int = 0
    n_answered: int = 0
    strata: Mapping[str, StratumCounts] = field(default_factory=dict)
    n_matched: int = 0                  # answered items matched to a witness of the analysis
    disagree_p1_human_yes: int = 0      # human: told ops; automated P1: no violation note
    disagree_p1_human_no: int = 0       # human: did not tell ops; automated P1: a violation note
    disagree_any_human_yes: int = 0     # the same against P1 or a post labelled an escalation to humans
    disagree_any_human_no: int = 0
    notes: tuple[str, ...] = ()

    @property
    def disagree_p1(self) -> int:
        return self.disagree_p1_human_yes + self.disagree_p1_human_no

    @property
    def disagree_any(self) -> int:
        return self.disagree_any_human_yes + self.disagree_any_human_no


def _read(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def _yn(v: str | None) -> bool | None:
    v = (v or "").strip().casefold()
    return True if v in ("yes", "y", "1", "true") else False if v in ("no", "n", "0", "false") else None


def _witness(by_ep: Mapping[int, Sequence[WitnessOutcome]], ep: str, wit: str) -> WitnessOutcome | None:
    try:
        ws = by_ep.get(int(ep), ())
    except ValueError:
        return None
    w = wit.strip()
    if w.isdigit():
        return next((x for x in ws if x.slot == int(w)), None)
    if w[:1] in "sS" and w[1:].isdigit():
        return next((x for x in ws if x.slot == int(w[1:])), None)
    return next((x for x in ws if x.handle == w), None)


def read_human_read(directory: Path, witnesses: Sequence[WitnessOutcome]) -> HumanReadResults:
    directory = Path(directory)
    sel_p, ans_p = directory / "selection.csv", directory / "answers.csv"
    if not sel_p.is_file() and not ans_p.is_file():
        return HumanReadResults(NOT_RUN)
    selection = _read(sel_p) if sel_p.is_file() else []
    if not ans_p.is_file():
        return HumanReadResults(SELECTED, n_selected=len(selection))
    answers = _read(ans_p)
    sel_by = {r.get("item_id", ""): r for r in selection}
    by_ep: dict[int, list[WitnessOutcome]] = {}
    for x in witnesses:
        by_ep.setdefault(x.episode_id, []).append(x)
    notes: list[str] = []
    counts: dict[str, dict[str, int]] = {s: dict(n=0, told_yes=0, told_no=0, mentioned_yes=0, mentioned_no=0, p1_yes=0)
                                         for s in STRATA}
    answered = matched = 0
    dy = dn = ay = an = 0
    seen: set[str] = set()
    for r in answers:
        iid = r.get("item_id", "")
        row = {**sel_by.get(iid, {}), **{k: v for k, v in r.items() if v}}
        told, ment = _yn(row.get("told_ops")), _yn(row.get("mentioned_split"))
        stratum = row.get("stratum", "")
        if told is None or ment is None:
            continue
        if stratum not in counts:
            notes.append(f"human read: item {iid} has an unknown stratum; not counted")
            continue
        if iid in seen:
            notes.append(f"human read: item {iid} answered twice; the first answer is used")
            continue
        seen.add(iid)
        answered += 1
        s = counts[stratum]
        s["n"] += 1
        s["told_yes" if told else "told_no"] += 1
        s["mentioned_yes" if ment else "mentioned_no"] += 1
        x = _witness(by_ep, row.get("episode_id", ""), row.get("witness", ""))
        if x is None:
            notes.append(f"human read: item {iid} matches no witness of the analysis")
            continue
        matched += 1
        s["p1_yes"] += int(x.p1)
        any_route = x.p1 or bool(x.esc_post)
        dy += int(told and not x.p1)
        dn += int(not told and x.p1)
        ay += int(told and not any_route)
        an += int(not told and any_route)
    n_sel = len(selection) if selection else len(answers)
    want = set(sel_by) if sel_by else {r.get("item_id", "") for r in answers}
    status = COMPLETE if want and want <= seen else PARTIAL
    return HumanReadResults(status, n_sel, answered, {k: StratumCounts(**v) for k, v in counts.items()}, matched,
                            dy, dn, ay, an, tuple(notes))


def summary_note(r: HumanReadResults) -> dict[str, Any]:
    return {"status": r.status, "selected": r.n_selected, "answered": r.n_answered, "matched": r.n_matched,
            "notes": list(r.notes)}
