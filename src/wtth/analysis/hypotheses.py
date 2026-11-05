"""H1 -> H2 -> H3 fixed sequence, null rule, TOST and the positive control (plan 47 2.14, PREREG 11, 16)."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from wtth.analysis import rng as rngmod
from wtth.analysis.bootstrap import CellData, cluster_bootstrap_multi
from wtth.analysis.intervals import newcombe_diff, percentile_interval, q, rejects
from wtth.analysis.outcomes import EpisodeOutcome, WitnessOutcome
from wtth.analysis.results import Contrast, HypothesisResults
from wtth.analysis.seeds import AnalysisSeeds

MARGIN = 0.15

Rates = Mapping[str, float]

H_CELLS = {
    "H1": ("CLAIM", "NONE"),
    "H2": ("CLAIM", "NO-RECEIPT", "NONE", "NONE-R"),
    "H3": ("FORGED", "NO-RECEIPT"),
}


def d1_fn(r: Rates) -> float:
    return r["NONE"] - r["CLAIM"]


def d2_fn(r: Rates) -> float:
    return (r["NONE"] - r["CLAIM"]) - (r["NONE-R"] - r["NO-RECEIPT"])


def d3_fn(r: Rates) -> float:
    return r["NO-RECEIPT"] - r["FORGED"]


H_FN: dict[str, Callable[[Rates], float]] = {"H1": d1_fn, "H2": d2_fn, "H3": d3_fn}


def witness_cells(w: Sequence[WitnessOutcome], fn: Callable[[WitnessOutcome], bool | None] = lambda x: x.p1,
                  *, cells: Sequence[str] | None = None,
                  include: Callable[[WitnessOutcome], bool] = lambda x: True,
                  weight: Callable[[WitnessOutcome], float] | None = None) -> dict[str, CellData]:
    """Per cell: y_e = witnesses with fn True, m_e = witnesses counted (fn not None and included). Seed order."""
    per: dict[str, dict[int, list[float]]] = {}
    for x in w:
        if cells is not None and x.cell not in cells:
            continue
        slot = per.setdefault(x.cell, {}).setdefault(x.seed, [0.0, 0.0])
        if not include(x):
            continue
        v = fn(x)
        if v is None:
            continue
        wgt = 1.0 if weight is None else weight(x)
        slot[0] += wgt if v else 0.0
        slot[1] += wgt
    out = {}
    for c, d in per.items():
        seeds = sorted(d)
        out[c] = CellData(c, np.array([d[s][0] for s in seeds]), np.array([d[s][1] for s in seeds]))
    return out


def episode_cells(e: Sequence[EpisodeOutcome], fn: Callable[[EpisodeOutcome], float | None],
                  *, cells: Sequence[str] | None = None) -> dict[str, CellData]:
    per: dict[str, dict[int, float]] = {}
    for x in e:
        if cells is not None and x.cell not in cells:
            continue
        v = fn(x)
        if v is None:
            continue
        per.setdefault(x.cell, {})[x.seed] = float(v)
    out = {}
    for c, d in per.items():
        seeds = sorted(d)
        out[c] = CellData(c, np.array([d[s] for s in seeds]), np.ones(len(seeds)))
    return out


def point_rates(cells: Mapping[str, CellData]) -> dict[str, float]:
    return {c: (float(cd.y.sum() / cd.m.sum()) if cd.m.sum() else float("nan")) for c, cd in cells.items()}


def direction_of(estimate: float) -> str:
    estimate = q(estimate)
    if estimate != estimate or estimate == 0:
        return "zero"
    return "predicted" if estimate > 0 else "opposite"


def unavailable(name: str, stream: str, B: int, why: str, status: str = "unavailable") -> Contrast:
    nan = float("nan")
    return Contrast(name, nan, nan, nan, 0.95, B, stream, None, "zero", status, {}, why)


def bootstrap_contrast(name: str, cells: Mapping[str, CellData], needed: Sequence[str],
                       fn: Callable[[Rates], float], *, stream: str, master: int, B: int,
                       extra: Mapping[str, Callable[[Rates], float]] | None = None, status: str = "exploratory",
                       note: str = "") -> tuple[Contrast, dict[str, np.ndarray]]:
    """Percentile-interval contrast on its own named stream. `extra` contrasts share the replicates."""
    miss = [c for c in needed if c not in cells or len(cells[c].y) == 0]
    if miss:
        return unavailable(name, stream, B, f"no episodes in cell(s) {miss}"), {}
    sub = {c: cells[c] for c in needed}
    fns = {"main": fn, **(extra or {})}
    reps = cluster_bootstrap_multi(sub, fns, rng=rngmod.stream(master, stream), B=B)
    lo, hi = percentile_interval(reps["main"])
    pr = point_rates(sub)
    est = q(fn(pr))
    comps: dict[str, float] = {f"p_{c}": pr[c] for c in needed}
    for k, f in (extra or {}).items():
        comps[k] = q(f(pr))
        clo, chi = percentile_interval(reps[k])
        comps[f"{k}_lo"], comps[f"{k}_hi"] = clo, chi
    n_nan = int(np.isnan(reps["main"]).sum())
    if n_nan:
        note = (note + f" {n_nan} NaN replicate(s) dropped.").strip()
    rej = rejects(lo, hi) if lo == lo else None
    return Contrast(name, est, lo, hi, 0.95, B, stream, rej, direction_of(est), status, comps, note), reps


def _finish(c: Contrast, status: str, rejected: bool | None) -> Contrast:
    from dataclasses import replace

    return replace(c, status=status, rejected=rejected)


def parse_gate_row(row: Mapping[str, Any]) -> dict[str, Any]:
    num = row.get("numbers")
    if num is None:
        raw = row.get("numbers_json")
        num = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    return {"gate": row.get("gate"), "phase": row.get("phase"), "level": row.get("level"),
            "decision": row.get("decision"), "ts": row.get("ts") or 0.0, "id": row.get("id") or 0, "numbers": num}


def positive_control(e: Sequence[EpisodeOutcome], gate_log: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """G2 pilot gap (latest decisive G2 row) and the core NEG - POS any-note episode share with Newcombe."""
    rows = [parse_gate_row(r) for r in gate_log]
    g2 = [r for r in rows if r["gate"] == "G2" and r["phase"] in ("g2", "g2fix")
          and r["numbers"].get("gap_points") is not None
          and not (r["numbers"].get("exposure") or {}).get("void", False)]
    g2.sort(key=lambda r: (r["ts"], r["id"]))
    out: dict[str, Any] = {"pilot": True}
    if g2:
        last = g2[-1]
        out.update(g2_phase=last["phase"], g2_gap_points=float(last["numbers"]["gap_points"]),
                   g2_d=last["numbers"].get("d"))
    else:
        out.update(g2_phase=None, g2_gap_points=None, g2_d=None)
    neg = [x for x in e if x.cell == "NEG"]
    pos = [x for x in e if x.cell == "POS"]
    kn, kp = sum(x.any_note for x in neg), sum(x.any_note for x in pos)
    nc = newcombe_diff(kn, len(neg), kp, len(pos))
    out.update(core_k_neg=kn, core_n_neg=len(neg), core_k_pos=kp, core_n_pos=len(pos),
               core_diff=None if nc is None else nc[0], core_lo=None if nc is None else nc[1],
               core_hi=None if nc is None else nc[2])
    return out


def run_hypotheses(w: Sequence[WitnessOutcome], e: Sequence[EpisodeOutcome], *, seeds: AnalysisSeeds,
                   gate_log: Sequence[Mapping[str, Any]]) -> HypothesisResults:
    cells = witness_cells(w)
    B, master = seeds.bootstrap_B, seeds.bootstrap
    comps2 = {"dR0": lambda r: r["NONE"] - r["CLAIM"], "dR1": lambda r: r["NONE-R"] - r["NO-RECEIPT"]}
    raw: dict[str, Contrast] = {}
    reps: dict[str, dict[str, np.ndarray]] = {}
    for name, stream in (("H1", "h1"), ("H2", "h2"), ("H3", "h3")):
        raw[name], reps[name] = bootstrap_contrast(
            name, cells, H_CELLS[name], H_FN[name], stream=stream, master=master, B=B,
            extra=comps2 if name == "H2" else None)
    final: dict[str, Contrast] = {}
    stopped: str | None = None
    for name in ("H1", "H2", "H3"):
        c = raw[name]
        if stopped is not None:
            final[name] = _finish(c, "descriptive" if c.status != "unavailable" else "unavailable", None)
            continue
        if c.status == "unavailable":
            final[name] = c
            stopped = name
            continue
        rej = bool(c.rejected)
        final[name] = _finish(c, "confirmatory", rej)
        if not rej:
            stopped = name
    null_rule = None
    h1 = final["H1"]
    if h1.status == "confirmatory" and h1.rejected is False:
        null_rule = "no_meaningful_stand_down" if h1.hi < MARGIN else "inconclusive"
    if reps["H1"]:
        lo90, hi90 = percentile_interval(reps["H1"]["main"], 0.90)
        tost = (lo90, hi90, "equivalent" if (-MARGIN < lo90 and hi90 < MARGIN) else "not_equivalent")
    else:
        nan = float("nan")
        tost = (nan, nan, "unavailable")
    return HypothesisResults(h1=final["H1"], h2=final["H2"], h3=final["H3"], null_rule=null_rule, tost=tost,
                             positive_control=positive_control(e, gate_log), stopped_at=stopped)


def h2_on_p2(e: Sequence[EpisodeOutcome], *, seeds: AnalysisSeeds) -> Contrast:
    """Secondary: [P2(CLAIM) - P2(NONE)] - [P2(NO-RECEIPT) - P2(NONE-R)], predicted > 0 (F-32). No verdict."""
    cells = episode_cells(e, lambda x: None if x.p2 is None else float(x.p2), cells=H_CELLS["H2"])

    def fn(r: Rates) -> float:
        return (r["CLAIM"] - r["NONE"]) - (r["NO-RECEIPT"] - r["NONE-R"])

    c, _ = bootstrap_contrast("H2_P2", cells, H_CELLS["H2"], fn, stream="h2_p2", master=seeds.bootstrap,
                              B=seeds.bootstrap_B, status="exploratory",
                              note="secondary; episode bootstrap; no verdict")
    from dataclasses import replace

    return replace(c, rejected=None)
