"""Exploratory follow-up cells of DEVIATIONS D-022 (phase exp1). Nothing here is confirmatory.

exp1 never enters a confirmatory set: run._phases_present and sets.analysis_set select the phases core, mvp and
sonnet only, and this module builds its own episode list from phase 'exp1' alone. It uses the same outcome code (P1:
keyword rule, else the final refers_to_violation label; P2: no witness with P1). Its labels come from the exp1 judge
database (runs/judge_exp1/judge.sqlite, configs/exp1.yaml paths.judge_subdir), never from the confirmatory one.

Status (macro ExpOneStatus): 'not run' while no exp1 episode is done; 'partial' while an arm is short of its D-022 size,
an exp1 episode is still queued or running, or a P1 label is unresolved; 'complete' otherwise. Numbers are emitted only
for a complete phase: Wilson intervals per arm, and episode-cluster bootstrap differences (stream exp1) against Haiku
FORGED and Haiku NO-RECEIPT (the two Haiku arms) and Sonnet FORGED (Sonnet RECEIPT).
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wtth.analysis.bootstrap import CellData
from wtth.analysis.hypotheses import bootstrap_contrast, episode_cells, witness_cells
from wtth.analysis.intervals import wilson
from wtth.analysis.outcomes import EpisodeOutcome, WitnessOutcome, episode_outcomes, witness_outcomes
from wtth.analysis.results import CellRate, Contrast, EpisodeRate
from wtth.analysis.seeds import AnalysisSeeds
from wtth.analysis.sets import AnalysisSet, EpisodeRef, fingerprint_of, model_short

PHASE = "exp1"
JUDGE_SUBDIR = "judge_exp1"
EXPLORATORY = "exploratory (D-022)"
NOT_RUN, PARTIAL, COMPLETE = "not run", "partial", "complete"


@dataclass(frozen=True, slots=True)
class Arm:
    cell: str
    model: str          # 'haiku' | 'sonnet'
    planned: int        # D-022 size in shifts
    token: str          # macro token
    label: str          # figure label
    has_p2: bool        # False where C really reported (P2 is not defined, PREREG 10)
    refs: tuple[str, ...]


ARMS = (
    Arm("X-FORGED-POLICY", "haiku", 50, "XForgedPolicy", "X-FORGED-POLICY", True, ("HaikuForged", "HaikuNoReceipt")),
    Arm("X-FORGED-PROV", "haiku", 50, "XForgedProv", "X-FORGED-PROV", True, ("HaikuForged", "HaikuNoReceipt")),
    Arm("RECEIPT", "sonnet", 30, "SonnetReceipt", "RECEIPT (Sonnet)", False, ("SonnetForged",)),
)
REFS: Mapping[str, tuple[str, str]] = {"HaikuForged": ("haiku", "FORGED"), "HaikuNoReceipt": ("haiku", "NO-RECEIPT"),
                                       "SonnetForged": ("sonnet", "FORGED")}


@dataclass(frozen=True, slots=True)
class ArmResult:
    arm: Arm
    n_episodes: int
    p1: CellRate
    p2: EpisodeRate | None


@dataclass(frozen=True, slots=True)
class Exp1Results:
    status: str
    n_done: Mapping[str, int]           # by arm token
    n_open: int                         # queued or running exp1 episodes
    n_pending: int                      # witnesses with an unresolved note
    arms: tuple[ArmResult, ...] = ()
    p1_diffs: tuple[tuple[str, str, Contrast], ...] = ()     # (arm token, ref token, contrast)
    p2_diffs: tuple[tuple[str, str, Contrast], ...] = ()
    notes: tuple[str, ...] = ()


def label_source(settings: Any) -> Any:
    """Final labels (reviewed where reviewed, else the judge's) from the exp1 judge database, read from an online
    snapshot of it (runs/analysis/snapshots/judge_exp1-<UTC>.sqlite) so the analysis never writes to, or waits on,
    the live file; an empty source when it does not exist yet, so judge-path notes stay unresolved and the status
    stays 'partial'."""
    import time
    from datetime import datetime, timezone
    from pathlib import Path

    from wtth import db as dbmod
    from wtth.judge.types import DictLabelSource

    path = Path(settings.paths.runs_dir) / JUDGE_SUBDIR / "judge.sqlite"
    if not path.is_file():
        return DictLabelSource({}, gates={"p1_final": False, "p2prime_final": False})
    from wtth.annotate.final import JudgeDbLabelSource

    stamp = datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y%m%dT%H%M%S")
    snap_dir = Path(settings.paths.runs_dir) / "analysis" / "snapshots"
    dest = snap_dir / f"{JUDGE_SUBDIR}-{stamp}.sqlite"
    n = 1
    while dest.exists():
        dest = snap_dir / f"{JUDGE_SUBDIR}-{stamp}-{n}.sqlite"
        n += 1
    dbmod.backup(path, dest)
    return JudgeDbLabelSource(dbmod.Db(dest), strict=False)


def _ref(r: Any) -> EpisodeRef:
    import json

    return EpisodeRef(
        episode_id=int(r["episode_id"]), attempt=int(r["final_attempt"]), phase=r["phase"], scenario=r["scenario"],
        model=r["model"], cell=r["cell"], level=r["level"], seed=int(r["seed"]), world=r["world"],
        handles=tuple(json.loads(r["handles_json"])), paraphrase_idx=int(r["paraphrase_idx"]),
        handle_idx=int(r["handle_idx"]), ticket_idx=int(r["ticket_idx"]))


def _arm_of(row: Any) -> Arm | None:
    short = model_short(row["model"])
    return next((a for a in ARMS if a.cell == row["cell"] and a.model == short), None)


def _set(model_id: str, refs: Sequence[EpisodeRef]) -> AnalysisSet:
    return AnalysisSet(phase=PHASE, model=model_id, s_c=None, cut={}, planned={}, episodes=tuple(refs), above=(),
                       failed_permanent=(), missing=(),
                       fingerprint=fingerprint_of([(x.episode_id, x.attempt) for x in refs]))


def _p2_rate(model: str, cell: str, es: Sequence[EpisodeOutcome]) -> EpisodeRate:
    n = len(es)
    k = sum(1 for x in es if x.y == 0)
    iv = wilson(k, n)
    return EpisodeRate(model, cell, "p2", k, n, k / n, iv[0], iv[1], k / n, k / n)


def _p1_rate(model: str, cell: str, ws: Sequence[WitnessOutcome]) -> CellRate:
    k, n = sum(1 for x in ws if x.p1), len(ws)
    iv = wilson(k, n)
    return CellRate(model, cell, "p1", k, n, len({x.episode_id for x in ws}), k / n, iv[0], iv[1], "wilson")


def analyse(db: Any, labels: Any, *, seeds: AnalysisSeeds,
            ref_witnesses: Mapping[str, Sequence[WitnessOutcome]],
            ref_episodes: Mapping[str, Sequence[EpisodeOutcome]]) -> Exp1Results:
    """ref_witnesses / ref_episodes: the confirmatory outcomes by model ('haiku' core, 'sonnet'), read-only."""
    rows = db.read(lambda c: c.execute(
        "SELECT * FROM episodes WHERE phase=? AND probe=0 ORDER BY episode_id", (PHASE,)).fetchall())
    done: dict[str, list[EpisodeRef]] = {a.token: [] for a in ARMS}
    n_open = 0
    notes: list[str] = []
    unknown = 0
    for r in rows:
        a = _arm_of(r)
        if a is None:
            unknown += 1
            continue
        if r["status"] in ("queued", "running"):
            n_open += 1
        elif r["status"] == "done":
            done[a.token].append(_ref(r))
    if unknown:
        notes.append(f"exp1: {unknown} episode(s) in a cell outside D-022 ignored")
    n_done = {k: len(v) for k, v in done.items()}
    if not any(n_done.values()):
        return Exp1Results(NOT_RUN, n_done, n_open, 0, notes=tuple(notes))
    w_by: dict[str, list[WitnessOutcome]] = {}
    e_by: dict[str, list[EpisodeOutcome]] = {}
    pending = 0
    for a in ARMS:
        refs = done[a.token]
        if not refs:
            w_by[a.token], e_by[a.token] = [], []
            continue
        aset = _set(refs[0].model, refs)
        w = witness_outcomes(db, aset, labels, strict=False)
        e = episode_outcomes(w, db, aset, labels, strict=False)
        pending += sum(1 for x in w if x.p1_pending)
        w_by[a.token], e_by[a.token] = w, e
    short = [a.token for a in ARMS if n_done[a.token] < a.planned]
    if short or n_open or pending:
        if short:
            notes.append("exp1 arms below their D-022 size: " + ", ".join(f"{t} {n_done[t]}" for t in short))
        if pending:
            notes.append(f"exp1: {pending} witness(es) with an unresolved note")
        return Exp1Results(PARTIAL, n_done, n_open, pending, notes=tuple(notes))
    arms: list[ArmResult] = []
    p1d: list[tuple[str, str, Contrast]] = []
    p2d: list[tuple[str, str, Contrast]] = []
    for a in ARMS:
        w, e = w_by[a.token], e_by[a.token]
        arms.append(ArmResult(a, len(e), _p1_rate(a.model, a.cell, w), _p2_rate(a.model, a.cell, e) if a.has_p2 else None))
        mine_w = witness_cells(w)[a.cell]
        mine_e = episode_cells(e, lambda x: float(x.y == 0))[a.cell] if a.has_p2 else None
        for ref in a.refs:
            model, cell = REFS[ref]
            rw = [x for x in ref_witnesses.get(model, ()) if x.cell == cell]
            if not rw:
                notes.append(f"exp1: reference {ref} has no witnesses; difference not computed")
                continue
            cells: dict[str, CellData] = {"A": mine_w, "B": witness_cells(rw)[cell]}
            c, _ = bootstrap_contrast(f"P1:{a.cell}-{ref}", cells, ("A", "B"), lambda r: r["A"] - r["B"], stream="exp1",
                                      master=seeds.bootstrap, B=seeds.bootstrap_B, status="exploratory",
                                      note=f"{EXPLORATORY}: episode-cluster bootstrap, {a.label} minus {ref}")
            p1d.append((a.token, ref, dataclasses.replace(c, rejected=None)))
            if mine_e is not None:
                re_ = [x for x in ref_episodes.get(model, ()) if x.cell == cell]
                cells2 = {"A": mine_e, "B": episode_cells(re_, lambda x: float(x.y == 0))[cell]}
                c2, _ = bootstrap_contrast(f"P2:{a.cell}-{ref}", cells2, ("A", "B"), lambda r: r["A"] - r["B"],
                                           stream="exp1", master=seeds.bootstrap, B=seeds.bootstrap_B,
                                           status="exploratory",
                                           note=f"{EXPLORATORY}: episode bootstrap, {a.label} minus {ref}")
                p2d.append((a.token, ref, dataclasses.replace(c2, rejected=None)))
    return Exp1Results(COMPLETE, n_done, 0, 0, tuple(arms), tuple(p1d), tuple(p2d), tuple(notes))
