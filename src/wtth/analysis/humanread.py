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
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from wtth.analysis.outcomes import WitnessOutcome

POSTHOC = "post hoc (D-021)"
STRATA = ("claim_never", "forged_untold", "noreceipt_never", "control")
STRATUM_TOKENS = {"claim_never": "ClaimNever", "forged_untold": "ForgedUntold", "noreceipt_never": "NoReceiptNever",
                  "control": "Control"}
SECOND_MODEL_NAME = "Claude Fable 5.1"      # D-025: the blind second reader (second_reader.csv carries no model column)
KAPPA_KEY = "author_read_kappa"
NOT_RUN, SELECTED, PARTIAL, COMPLETE = "not run", "selected", "partial", "complete"


@dataclass(frozen=True, slots=True)
class StratumCounts:
    n: int = 0                  # answered items (both questions yes / no)
    told_yes: int = 0
    told_no: int = 0
    mentioned_yes: int = 0
    mentioned_no: int = 0
    p1_yes: int = 0             # automated P1 of the same witnesses
    disagree_p1: int = 0        # answered items whose told_ops differs from the automated P1 of the same witness


@dataclass(frozen=True, slots=True)
class SecondReaderResults:
    """D-025: the blind second reader against the author's original answers and against P1."""
    model: str
    n: int                              # items read by both
    agree_author_told: int
    agree_author_mentioned: int
    n_p1: int                           # items matched to a witness
    agree_p1: int
    kappa_author_told: float
    kappa_ci: tuple[float, float]
    kappa_B: int
    kappa_seed: int
    kappa_undefined_draws: int
    p1_differences: tuple[str, ...] = ()    # item ids where the second reader and P1 differ


@dataclass(frozen=True, slots=True)
class AdjudicationResults:
    """D-025: the author's decisions after seeing only the disagreements."""
    n: int
    shown: int
    changed: int
    kept: int
    n_p1: int
    agree_p1: int
    agree_second: int
    strata: Mapping[str, StratumCounts] = field(default_factory=dict)   # told counts after adjudication


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
    second: SecondReaderResults | None = None
    adjudication: AdjudicationResults | None = None

    @property
    def author_agree_p1(self) -> int:
        return self.n_matched - self.disagree_p1

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


def read_human_read(directory: Path, witnesses: Sequence[WitnessOutcome], *, bootstrap_B: int = 10000) -> HumanReadResults:
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
    counts: dict[str, dict[str, int]] = {s: dict(n=0, told_yes=0, told_no=0, mentioned_yes=0, mentioned_no=0, p1_yes=0, disagree_p1=0)
                                         for s in STRATA}
    answered = matched = 0
    dy = dn = ay = an = 0
    seen: set[str] = set()
    item_row: dict[str, dict[str, str]] = {}
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
        item_row[iid] = row
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
        s["disagree_p1"] += int(told != x.p1)
        any_route = x.p1 or bool(x.esc_post)
        dy += int(told and not x.p1)
        dn += int(not told and x.p1)
        ay += int(told and not any_route)
        an += int(not told and any_route)
    n_sel = len(selection) if selection else len(answers)
    want = set(sel_by) if sel_by else {r.get("item_id", "") for r in answers}
    status = COMPLETE if want and want <= seen else PARTIAL
    strata = {k: StratumCounts(**v) for k, v in counts.items()}
    second = adjudication = None
    if status == COMPLETE:
        p1_of = {i: (_witness(by_ep, r.get("episode_id", ""), r.get("witness", "")) or None) for i, r in item_row.items()}
        told_of = {i: _yn(r.get("told_ops")) for i, r in item_row.items()}
        second = _second_reader(directory / "second_reader.csv", item_row, told_of, p1_of, notes, bootstrap_B)
        adjudication = _adjudication(directory / "answers_adjudicated.csv", item_row, second_told=second_told(
            directory / "second_reader.csv"), p1_of=p1_of, notes=notes)
    return HumanReadResults(status, n_sel, answered, strata, matched, dy, dn, ay, an, tuple(notes), second, adjudication)


def kappa_binary(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Cohen's kappa of two binary raters; nan when chance agreement is 1 (undefined)."""
    x, y = np.asarray(a, dtype=bool), np.asarray(b, dtype=bool)
    po = float(np.mean(x == y))
    pe = float(np.mean(x) * np.mean(y) + (1 - np.mean(x)) * (1 - np.mean(y)))
    return float("nan") if pe >= 1.0 else (po - pe) / (1.0 - pe)


def author_read_seed() -> int:
    """Derived like the seeds of configs/analysis.yaml: int(sha256(f"wtth-{key}-2026-11-06")[:8], 16)."""
    return int(hashlib.sha256(f"wtth-{KAPPA_KEY}-2026-11-06".encode()).hexdigest()[:8], 16)


def bootstrap_kappa(a: Sequence[bool], b: Sequence[bool], *, B: int, seed: int) -> tuple[tuple[float, float], int]:
    """Percentile 95% interval of kappa over items (resample items with replacement); draws with undefined kappa
    (chance agreement 1) are dropped and counted."""
    from wtth.analysis.rng import stream

    x, y = np.asarray(a, dtype=bool), np.asarray(b, dtype=bool)
    n = len(x)
    idx = stream(seed, "author_read").integers(0, n, size=(B, n))
    xs, ys = x[idx].astype(float), y[idx].astype(float)
    po = (xs == ys).mean(axis=1)
    px, py = xs.mean(axis=1), ys.mean(axis=1)
    pe = px * py + (1 - px) * (1 - py)
    ok = pe < 1.0
    k = (po[ok] - pe[ok]) / (1.0 - pe[ok])
    if not len(k):
        return (float("nan"), float("nan")), int(B)
    lo, hi = np.percentile(k, [2.5, 97.5])
    return (float(lo), float(hi)), int(B - ok.sum())


def second_told(path: Path) -> dict[str, bool | None]:
    return {r.get("item_id", ""): _yn(r.get("told_ops")) for r in _read(path)} if path.is_file() else {}


def _second_reader(path: Path, item_row: Mapping[str, Mapping[str, str]], told_of: Mapping[str, bool | None],
                   p1_of: Mapping[str, WitnessOutcome | None], notes: list[str], B: int) -> SecondReaderResults | None:
    if not path.is_file():
        return None
    rows = {r.get("item_id", ""): r for r in _read(path)}
    ids = [i for i in item_row if _yn(rows.get(i, {}).get("told_ops")) is not None
           and _yn(rows.get(i, {}).get("mentioned_split")) is not None]
    if len(ids) != len(item_row):
        notes.append("human read: second_reader.csv does not cover every answered item; second reader not reported")
        return None
    a = [bool(told_of[i]) for i in ids]
    b = [bool(_yn(rows[i]["told_ops"])) for i in ids]
    am = [bool(_yn(item_row[i].get("mentioned_split"))) for i in ids]
    bm = [bool(_yn(rows[i]["mentioned_split"])) for i in ids]
    matched = [i for i in ids if p1_of.get(i) is not None]
    diff = tuple(i for i in matched if bool(_yn(rows[i]["told_ops"])) != p1_of[i].p1)
    seed = author_read_seed()
    ci, undef = bootstrap_kappa(a, b, B=B, seed=seed)
    return SecondReaderResults(SECOND_MODEL_NAME, len(ids), sum(x == y for x, y in zip(a, b)),
                               sum(x == y for x, y in zip(am, bm)), len(matched), len(matched) - len(diff),
                               kappa_binary(a, b), ci, B, seed, undef, diff)


def _adjudication(path: Path, item_row: Mapping[str, Mapping[str, str]], *, second_told: Mapping[str, bool | None],
                  p1_of: Mapping[str, WitnessOutcome | None], notes: list[str]) -> AdjudicationResults | None:
    if not path.is_file():
        return None
    rows = {r.get("item_id", ""): r for r in _read(path)}
    if any(_yn(rows.get(i, {}).get("told_ops")) is None for i in item_row):
        notes.append("human read: answers_adjudicated.csv does not cover every answered item; adjudication not reported")
        return None
    counts = {s: dict(n=0, told_yes=0, told_no=0, mentioned_yes=0, mentioned_no=0, p1_yes=0, disagree_p1=0)
              for s in STRATA}
    shown = changed = kept = n_p1 = agree_p1 = agree_second = 0
    for i, base in item_row.items():
        r = rows[i]
        told = bool(_yn(r["told_ops"]))
        c = counts[base["stratum"]]
        c["n"] += 1
        c["told_yes" if told else "told_no"] += 1
        sh = _yn(r.get("shown_to_author"))
        shown += int(bool(sh))
        dec = (r.get("decision") or "").casefold()
        changed += int(dec == "changed")
        kept += int(dec == "kept")
        x = p1_of.get(i)
        if x is not None:
            n_p1 += 1
            agree_p1 += int(told == x.p1)
            c["p1_yes"] += int(x.p1)
            c["disagree_p1"] += int(told != x.p1)
        agree_second += int(second_told.get(i) is not None and told == second_told[i])
    return AdjudicationResults(len(item_row), shown, changed, kept, n_p1, agree_p1, agree_second,
                               {k: StratumCounts(**v) for k, v in counts.items()})


def summary_note(r: HumanReadResults) -> dict[str, Any]:
    return {"status": r.status, "selected": r.n_selected, "answered": r.n_answered, "matched": r.n_matched,
            "notes": list(r.notes)}
