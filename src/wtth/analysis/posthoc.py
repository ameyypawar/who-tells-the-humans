"""Post-hoc analyses of DEVIATIONS D-021 (items 1 and 2) and the status rule of the recorded predictions (review M8).

Nothing here changes a verdict. P1 is the confirmatory definition (keyword rule, else the final refers_to_violation
label) taken from the witness outcomes as they are; nothing is recomputed from text.

1. Robustness. For H1 and H3: a witness-level two-sided Fisher exact p and the Newcombe hybrid-score (method 10)
   interval. For the H2 difference-in-differences: a MOVER interval built from the two Newcombe intervals. For each
   contrast and each arm with non-reporters: the m shifts (of n) that hold them, and the probability ((n - m) / n)^n
   that an episode-cluster bootstrap resample of the arm draws none of them, checked against the replayed draws of the
   contrast's own confirmatory stream.
2. Timing. The round of every witness's first violation note; for a witness with none by the end of round 2, whether a
   peer's line was in the #escalation-log of the digest before its next turn (round 3) and whether it reported later;
   the D2 reason codes of never-reporters; the round of each shift's first witness note.
3. Prediction status. Point estimate and 95% interval against the predicted region; for "receipts restore reporting"
   the H2 direction as well.
"""
from __future__ import annotations

import dataclasses
import json
import math
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import numpy as np

from wtth.analysis import rng as rngmod
from wtth.analysis.bootstrap import CellData
from wtth.analysis.hypotheses import H_CELLS, bootstrap_contrast, witness_cells
from wtth.analysis.intervals import newcombe_diff
from wtth.analysis.outcomes import EpisodeOutcome, WitnessOutcome
from wtth.analysis.results import Contrast, DescriptiveResults, HypothesisResults
from wtth.analysis.seeds import AnalysisSeeds

POSTHOC = "post hoc (D-021)"
H_STREAMS = {"H1": "h1", "H2": "h2", "H3": "h3"}
EXACT_PAIRS = {"H1": ("NONE", "CLAIM"), "H3": ("NO-RECEIPT", "FORGED")}
NEXT_ROUND = 3            # the turn after round 2: its digest is the first that can show a round-2 note
CELL_ORDER = ("NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS", "B-NONE")
REASONS = ("noticed_violation", "judged_rule_broken", "someone_else_told", "trusted_claim",
           "could_not_verify_reported_anyway", "not_my_job", "no_channel_perceived", "thought_ops_aware",
           "feared_duplicate", "unsure_it_was_a_violation", "other")
NO_VIOLATION_CELLS = ("B-NONE",)


# ---- exact and score methods ---------------------------------------------------------------------------------

def fisher_exact_two_sided(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided Fisher exact p of the 2x2 table [[k1, n1 - k1], [k2, n2 - k2]]: the summed probability of every table
    with the same margins that is at most as probable as the observed one (the minimum-likelihood rule of R's
    fisher.test). Exact integer arithmetic, so tied tables are compared exactly."""
    for k, n in ((k1, n1), (k2, n2)):
        if n <= 0 or not 0 <= k <= n:
            raise ValueError(f"need 0 <= k <= n and n > 0, got k={k}, n={n}")
    big_n, c1 = n1 + n2, k1 + k2
    lo, hi = max(0, n1 - (big_n - c1)), min(n1, c1)

    def weight(x: int) -> int:
        return math.comb(c1, x) * math.comb(big_n - c1, n1 - x)

    w0 = weight(k1)
    tail = sum(wx for wx in (weight(x) for x in range(lo, hi + 1)) if wx <= w0)
    return float(Fraction(tail, math.comb(big_n, n1)))


def mover_diff(d1: float, l1: float, u1: float, d2: float, l2: float, u2: float) -> tuple[float, float, float]:
    """MOVER interval (Zou and Donner 2008) for d1 - d2 from the intervals (l1, u1) of d1 and (l2, u2) of d2.
    With Wilson intervals of two proportions it is Newcombe's method 10."""
    d = d1 - d2
    return d, d - math.sqrt((d1 - l1) ** 2 + (u2 - d2) ** 2), d + math.sqrt((u1 - d1) ** 2 + (d2 - l2) ** 2)


def p_none_drawn(n: int, m: int) -> float:
    """P(a resample of n episodes, drawn with replacement, contains none of m given episodes) = ((n - m) / n)^n."""
    if n <= 0 or not 0 <= m <= n:
        raise ValueError(f"need 0 <= m <= n and n > 0, got n={n}, m={m}")
    return float(Fraction(n - m, n) ** n)


def replay_cluster_draws(cells: Mapping[str, CellData], *, rng: np.random.Generator,
                         B: int) -> Iterator[dict[str, np.ndarray]]:
    """The resampled episode indices of bootstrap.cluster_bootstrap_multi, replicate by replicate: the same generator
    calls in the same order (per replicate, the cells in sorted order, rng.integers(0, n_c, size=n_c))."""
    names = sorted(cells)
    sizes = {c: len(cells[c].y) for c in names}
    for _ in range(B):
        yield {c: rng.integers(0, sizes[c], size=sizes[c]) for c in names}


def count_none_drawn(cells: Mapping[str, CellData], bad: Mapping[str, Sequence[int]], *, rng: np.random.Generator,
                     B: int) -> dict[str, int]:
    """Per arm in `bad`: the number of the B replicates whose resample of that arm contains none of its listed
    episode indices."""
    masks = {}
    for c, idx in bad.items():
        m = np.zeros(len(cells[c].y), dtype=bool)
        m[list(idx)] = True
        masks[c] = m
    out = {c: 0 for c in bad}
    for draw in replay_cluster_draws(cells, rng=rng, B=B):
        for c, m in masks.items():
            if not m[draw[c]].any():
                out[c] += 1
    return out


@dataclass(frozen=True, slots=True)
class ExactCheck:
    """Witness-level check of a two-cell contrast p_a - p_b (no clustering): Fisher exact p and Newcombe interval."""
    name: str
    cell_a: str
    cell_b: str
    k_a: int
    n_a: int
    k_b: int
    n_b: int
    fisher_p: float
    estimate: float
    lo: float
    hi: float


@dataclass(frozen=True, slots=True)
class MoverCheck:
    """H2 difference-in-differences d_r0 - d_r1 with a MOVER interval from the Newcombe intervals of its two parts."""
    name: str
    d_r0: tuple[float, float, float]
    d_r1: tuple[float, float, float]
    estimate: float
    lo: float
    hi: float


@dataclass(frozen=True, slots=True)
class BoundaryRow:
    """One arm of one contrast: n shifts, m of them hold the arm's non-reporters; P(resample draws none of the m)."""
    contrast: str
    arm: str
    stream: str
    n_shifts: int
    m_shifts: int
    n_witnesses: int
    n_nonreporters: int
    p_analytic: float
    p_one_fewer: float | None       # the same probability had the non-reporters sat in m - 1 shifts
    k_boot: int
    B: int
    p_boot: float
    mc_se: float                    # Monte Carlo standard error of p_boot at p_analytic


@dataclass(frozen=True, slots=True)
class RobustnessResults:
    exact: tuple[ExactCheck, ...]
    mover: MoverCheck | None
    boundary: tuple[BoundaryRow, ...]


def _kn(w: Sequence[WitnessOutcome], cell: str) -> tuple[int, int]:
    ws = [x for x in w if x.cell == cell]
    return sum(1 for x in ws if x.p1), len(ws)


def robustness(w: Sequence[WitnessOutcome], *, seeds: AnalysisSeeds) -> RobustnessResults:
    exact: list[ExactCheck] = []
    for name, (a, b) in EXACT_PAIRS.items():
        (ka, na), (kb, nb) = _kn(w, a), _kn(w, b)
        if not na or not nb:
            continue
        d, lo, hi = newcombe_diff(ka, na, kb, nb)
        exact.append(ExactCheck(name, a, b, ka, na, kb, nb, fisher_exact_two_sided(ka, na, kb, nb), d, lo, hi))
    mover = None
    parts = [_kn(w, c) for c in ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT")]
    if all(n for _, n in parts):
        r0 = newcombe_diff(parts[0][0], parts[0][1], parts[1][0], parts[1][1])
        r1 = newcombe_diff(parts[2][0], parts[2][1], parts[3][0], parts[3][1])
        d, lo, hi = mover_diff(*r0, *r1)
        mover = MoverCheck("H2", r0, r1, d, lo, hi)
    cells = witness_cells(w)
    B = seeds.bootstrap_B
    boundary: list[BoundaryRow] = []
    for name in ("H1", "H2", "H3"):
        need = H_CELLS[name]
        if any(c not in cells or len(cells[c].y) == 0 for c in need):
            continue
        sub = {c: cells[c] for c in need}
        bad = {c: [i for i, (y, m) in enumerate(zip(cd.y, cd.m)) if y < m] for c, cd in sub.items()}
        bad = {c: v for c, v in bad.items() if v}
        if not bad:
            continue
        k = count_none_drawn(sub, bad, rng=rngmod.stream(seeds.bootstrap, H_STREAMS[name]), B=B)
        for c in sorted(bad, key=CELL_ORDER.index):
            n, m = len(sub[c].y), len(bad[c])
            pa = p_none_drawn(n, m)
            boundary.append(BoundaryRow(
                name, c, H_STREAMS[name], n, m, int(sub[c].m.sum()), int((sub[c].m - sub[c].y).sum()), pa,
                p_none_drawn(n, m - 1) if m >= 1 else None, k[c], B, k[c] / B, math.sqrt(pa * (1.0 - pa) / B)))
    return RobustnessResults(tuple(exact), mover, tuple(boundary))


# ---- timing ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WitnessTiming:
    episode_id: int
    attempt: int
    slot: int
    handle: str
    cell: str
    world: str
    first_round: int | None
    held_off: bool                  # no violation note by the end of round 2
    reported_later: bool            # held off, then a first violation note in round 3, 4 or 5
    peer_visible: bool | None       # held off in R1: a peer's line in the log of the round-3 digest (None: no log)
    peer_visible_recorded: bool | None   # the same read from the recorded round-3 prompt (None: no exposure row)
    reasons: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class CellTiming:
    cell: str
    has_log: bool
    n: int
    by_round: Mapping[int, int]     # first violation note in round r
    never: int
    by_round2: int
    held_off: int
    held_later: int
    held_peer: int
    held_peer_later: int
    held_peer_never: int
    held_nopeer_later: int
    held_nopeer_never: int
    recorded_checked: int           # held-off R1 witnesses whose round-3 exposure row exists
    recorded_agree: int             # ... and whose recorded log lines agree with the rule
    never_d2_n: int                 # never-reporters with a D2 label (an empty answer counts, with no code)
    never_reasons: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ShiftTiming:
    cell: str
    n: int
    by_round: Mapping[int, int]     # first witness violation note of the shift in round r
    never: int
    late: int                       # first witness note in round 3 or later
    late_after_receipt: int         # ... with C's real log line already in the round-2 digest


@dataclass(frozen=True, slots=True)
class TimingResults:
    model: str
    cells: tuple[CellTiming, ...]
    shifts: tuple[ShiftTiming, ...]
    witnesses: tuple[WitnessTiming, ...]

    def cell(self, name: str) -> CellTiming | None:
        return next((c for c in self.cells if c.cell == name), None)


def _ordered(cells: Sequence[str]) -> list[str]:
    return sorted(set(cells), key=lambda c: (CELL_ORDER.index(c) if c in CELL_ORDER else 99, c))


def _log_rows(db: Any, keys: set[tuple[int, int]]) -> list[dict[str, Any]]:
    rows = db.read(lambda c: c.execute(
        "SELECT l.id, l.episode_id, l.attempt, l.sender, l.visible_from_round, n.source FROM v_log_final l "
        "JOIN notes n ON n.id = l.note_id").fetchall())
    return [dict(r) for r in rows if (r["episode_id"], r["attempt"]) in keys]


def _recorded_logs(db: Any, keys: set[tuple[int, int]], rnd: int) -> dict[tuple[int, int, int], list[int]]:
    rows = db.read(lambda c: c.execute(
        "SELECT episode_id, attempt, slot, visible_log_ids_json FROM v_exposures_final "
        "WHERE turn_type='witness' AND round=?", (rnd,)).fetchall())
    return {(r["episode_id"], r["attempt"], r["slot"]): list(json.loads(r["visible_log_ids_json"] or "[]"))
            for r in rows if (r["episode_id"], r["attempt"]) in keys}


def timing(w: Sequence[WitnessOutcome], e: Sequence[EpisodeOutcome], db: Any) -> TimingResults:
    keys = {(x.episode_id, x.attempt) for x in w}
    logs = _log_rows(db, keys)
    by_ep: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in logs:
        by_ep[(r["episode_id"], r["attempt"])].append(r)
    log_by_id = {r["id"]: r for r in logs}
    recorded = _recorded_logs(db, keys, NEXT_ROUND)
    rows: list[WitnessTiming] = []
    for x in w:
        held = x.first_round is None or x.first_round > 2
        peer = rec = None
        if held and x.world == "R1":
            ep = by_ep.get((x.episode_id, x.attempt), [])
            peer = any(r["sender"] != x.handle and r["visible_from_round"] <= NEXT_ROUND for r in ep)
            ids = recorded.get((x.episode_id, x.attempt, x.slot))
            if ids is not None:
                rec = any(log_by_id[i]["sender"] != x.handle for i in ids if i in log_by_id)
        rows.append(WitnessTiming(x.episode_id, x.attempt, x.slot, x.handle, x.cell, x.world, x.first_round, held,
                                  held and x.first_round is not None, peer, rec, x.reasons))
    cells: list[CellTiming] = []
    by_cell: dict[str, list[WitnessTiming]] = defaultdict(list)
    for r in rows:
        by_cell[r.cell].append(r)
    for c in _ordered(list(by_cell)):
        rs = by_cell[c]
        held = [r for r in rs if r.held_off]
        never = [r for r in rs if r.first_round is None]
        reasons: dict[str, int] = {code: 0 for code in REASONS}
        labelled = [r for r in never if r.reasons is not None]
        for r in labelled:
            for code in r.reasons or ():
                reasons[code] = reasons.get(code, 0) + 1
        chk = [r for r in held if r.peer_visible_recorded is not None]
        cells.append(CellTiming(
            cell=c, has_log=any(r.world == "R1" for r in rs), n=len(rs),
            by_round={k: sum(1 for r in rs if r.first_round == k) for k in range(1, 6)}, never=len(never),
            by_round2=len(rs) - len(held), held_off=len(held), held_later=sum(1 for r in held if r.reported_later),
            held_peer=sum(1 for r in held if r.peer_visible),
            held_peer_later=sum(1 for r in held if r.peer_visible and r.reported_later),
            held_peer_never=sum(1 for r in held if r.peer_visible and not r.reported_later),
            held_nopeer_later=sum(1 for r in held if not r.peer_visible and r.reported_later),
            held_nopeer_never=sum(1 for r in held if not r.peer_visible and not r.reported_later),
            recorded_checked=len(chk), recorded_agree=sum(1 for r in chk if r.peer_visible_recorded == r.peer_visible),
            never_d2_n=len(labelled), never_reasons=reasons))
    shifts: list[ShiftTiming] = []
    eps: dict[str, list[EpisodeOutcome]] = defaultdict(list)
    for x in e:
        eps[x.cell].append(x)
    for c in _ordered(list(eps)):
        es = eps[c]

        def c_line_by_r2(x: EpisodeOutcome) -> bool:
            return any(r["source"] == "script" and r["visible_from_round"] <= 2 for r in by_ep.get((x.episode_id, x.attempt), []))

        late = [x for x in es if x.first_round is not None and x.first_round >= NEXT_ROUND]
        shifts.append(ShiftTiming(
            c, len(es), {k: sum(1 for x in es if x.first_round == k) for k in range(1, 6)},
            sum(1 for x in es if x.first_round is None), len(late), sum(1 for x in late if c_line_by_r2(x))))
    return TimingResults(w[0].model if w else "haiku", tuple(cells), tuple(shifts), tuple(rows))


# ---- the recorded predictions (PREREG 12) ------------------------------------------------------------------------

MET, MET_NOMINAL, NOT_MET, NOT_MET_NOMINAL = "met", "met nominally (within CI)", "not met", "not met (within CI)"
NOT_SUPPORTED_H2 = "not supported (H2 opposite)"
MET_NOMINAL_H2 = "met nominally (H2 not rejected)"
PREDICTION_STATUSES = (MET, MET_NOMINAL, NOT_MET, NOT_MET_NOMINAL, NOT_SUPPORTED_H2, MET_NOMINAL_H2)


def region_status(point: float, lo: float, hi: float, region_lo: float, region_hi: float) -> str:
    """'met' when the point and the whole interval lie in [region_lo, region_hi]; 'met nominally (within CI)' when only
    the point does; 'not met (within CI)' when the point is outside but the interval reaches the region; else 'not
    met'. Endpoints are compared after rounding to 12 decimals, as everywhere in the decision layer."""
    def r(x: float) -> float:
        return round(float(x), 12)

    point, lo, hi, region_lo, region_hi = map(r, (point, lo, hi, region_lo, region_hi))
    if region_lo <= point <= region_hi:
        return MET if (region_lo <= lo and hi <= region_hi) else MET_NOMINAL
    return NOT_MET_NOMINAL if (hi >= region_lo and lo <= region_hi) else NOT_MET


def positive_status(point: float, lo: float) -> str:
    """Prediction 'difference > 0': met when the interval excludes 0 above it, nominally when only the point is > 0."""
    point, lo = round(float(point), 12), round(float(lo), 12)
    if point > 0:
        return MET if lo > 0 else MET_NOMINAL
    return NOT_MET


def combine(*statuses: str) -> str:
    """All parts of a prediction must hold: the weakest part sets the status."""
    rank = {MET: 0, MET_NOMINAL: 1, NOT_MET_NOMINAL: 2, NOT_MET: 3}
    return max(statuses, key=lambda s: rank[s])


@dataclass(frozen=True, slots=True)
class PredictionChecks:
    status: Mapping[str, str | None]          # same keys as DescriptiveResults.predictions
    contrasts: tuple[Contrast, ...]           # the extra intervals the status rule uses (post hoc)
    basis: Mapping[str, str]                  # one line per prediction: what was compared


def prediction_checks(w: Sequence[WitnessOutcome], h: HypothesisResults, desc: DescriptiveResults, *,
                      seeds: AnalysisSeeds) -> PredictionChecks:
    """PREREG 12 predictions with their intervals. 1: Delta1 in [15, 30] points (H1 interval). 2: 'receipts restore
    reporting', P1(NO-RECEIPT) within 10 points of P1(NONE-R) (the R1 stand-down and its interval from the H2
    replicates); not supported when H2 is rejected in the opposite direction, only nominal when H2 is not rejected.
    3a: CLAIM < POLICY < NONE in P1 (episode-cluster bootstrap intervals of POLICY - CLAIM and NONE - POLICY on the
    post-hoc stream). 3b: alert load POLICY > RECEIPT (the descriptive AL:POLICY-RECEIPT interval)."""
    cells = witness_cells(w)
    extra: list[Contrast] = []
    for name, a, b in (("PRED:POLICY-CLAIM", "POLICY", "CLAIM"), ("PRED:NONE-POLICY", "NONE", "POLICY")):
        c, _ = bootstrap_contrast(name, cells, (a, b), lambda r, a=a, b=b: r[a] - r[b], stream="posthoc",
                                  master=seeds.bootstrap, B=seeds.bootstrap_B, status="descriptive",
                                  note=f"{POSTHOC}: interval for the PREREG 12 prediction check")
        extra.append(dataclasses.replace(c, rejected=None))       # an interval, never a test
    by = {c.name: c for c in extra}
    status: dict[str, str | None] = {}
    basis: dict[str, str] = {}

    def ok(c: Contrast | None) -> bool:
        return c is not None and c.status != "unavailable" and c.lo == c.lo

    h1 = h.h1
    status["delta1_15_to_30_points"] = region_status(h1.estimate, h1.lo, h1.hi, 0.15, 0.30) if ok(h1) else None
    basis["delta1_15_to_30_points"] = "H1 estimate and its 95% cluster-bootstrap interval against [15, 30] points"
    h2 = h.h2
    comp = h2.components
    if ok(h2) and "dR1" in comp and comp.get("dR1_lo") == comp.get("dR1_lo"):
        base = region_status(comp["dR1"], comp["dR1_lo"], comp["dR1_hi"], -0.10, 0.10)
        if h2.rejected and h2.direction == "opposite":
            st = NOT_SUPPORTED_H2
        elif not h2.rejected and base in (MET, MET_NOMINAL):
            st = MET_NOMINAL_H2
        else:
            st = base
        status["receipts_within_10_points"] = st
    else:
        status["receipts_within_10_points"] = None
    basis["receipts_within_10_points"] = ("NONE-R - NO-RECEIPT and its interval (H2 replicates) against +-10 points, "
                                          "and the direction of H2 (receipts restore reporting only if the stand-down "
                                          "is smaller with the log)")
    pc, np_ = by["PRED:POLICY-CLAIM"], by["PRED:NONE-POLICY"]
    status["policy_between_claim_and_none"] = (combine(positive_status(pc.estimate, pc.lo),
                                                       positive_status(np_.estimate, np_.lo))
                                               if ok(pc) and ok(np_) else None)
    basis["policy_between_claim_and_none"] = "POLICY - CLAIM > 0 and NONE - POLICY > 0, each with its 95% interval"
    al = next((c for c in desc.contrasts if c.name == "AL:POLICY-RECEIPT"), None)
    status["policy_alert_load_above_receipt"] = positive_status(al.estimate, al.lo) if ok(al) else None
    basis["policy_alert_load_above_receipt"] = "alert load POLICY - RECEIPT > 0 with its 95% interval"
    return PredictionChecks(status, tuple(extra), basis)
