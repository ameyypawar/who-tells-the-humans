"""Exploratory follow-up 2 of DEVIATIONS D-028 (phase exp2). Nothing here is confirmatory.

exp2 never enters a confirmatory set: run._phases_present and sets.analysis_set select the phases core, mvp and sonnet
only, and this module builds its own episode list from phase 'exp2' alone (probes, phase exp2probe, are never read).
It uses the outcome code of the core (P1: keyword rule, else the final refers_to_violation label; P2: no witness with
P1). Its labels come from the exp2 judge database (runs/judge_exp2/judge.sqlite, configs/exp2.yaml paths.judge_subdir)
through an online snapshot, as exp1 reads its own.

Two Haiku cells, seeds 181-230 in both, seed-paired and interleaved in one run, so concurrent:
  X-FORGED-CHECK  FORGED plus one witness tool, lookup_escalation (receipts in OpenObserve)
  X-FORGED-CONC   FORGED unchanged, the concurrent control

Status (macro ExpTwoStatus): 'not run', 'partial' or 'complete', by the rule of exp1. Numbers only for a complete phase:
  - per cell, P1 with a witness-level Wilson interval and a shift-level cluster-bootstrap interval (stream posthoc, as
    every Fig. 1a row), and P2 with a shift-level Wilson interval and a bootstrap over shifts (stream exp2);
  - episode-cluster bootstrap differences (stream exp2, the master seed and B of every other interval): CHECK - CONC
    (the concurrent contrast), CHECK - core NO-RECEIPT, CONC - core FORGED (the phase shift), and, cross-phase against
    the later concurrent control, exp1's X-FORGED-POLICY - CONC and X-FORGED-PROV - CONC;
  - for every P2 difference, the shift-level two-sided Fisher exact p and Newcombe interval (a shift is the unit of P2,
    so neither ignores clustering), and whether an arm sits at the bootstrap boundary (a P2 of 0 or 1: its resamples
    never vary, so the percentile interval can exclude 0 on a few shifts);
  - lookup_escalation use in X-FORGED-CHECK: witnesses who called it, calls, what each returned, and the witness turns
    whose recorded tool list held it;
  - the core's timing families (first-report round, held off past round 2, D2 reasons of never-reporters) and the top
    D2 reasons of the never-reporters of each cell.
"""
from __future__ import annotations

import dataclasses
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wtth.analysis import exp1
from wtth.analysis.bootstrap import CellData
from wtth.analysis.hypotheses import bootstrap_contrast, episode_cells, witness_cells
from wtth.analysis.intervals import newcombe_diff
from wtth.analysis.outcomes import EpisodeOutcome, WitnessOutcome, episode_outcomes, witness_outcomes
from wtth.analysis.results import CellRate, Contrast, EpisodeRate
from wtth.analysis.seeds import AnalysisSeeds
from wtth.analysis.sets import AnalysisSet, EpisodeRef, fingerprint_of

PHASE = "exp2"
JUDGE_SUBDIR = "judge_exp2"
EXPLORATORY = "exploratory (D-028)"
CROSS_PHASE = "cross-phase, against the later concurrent control"
STREAM = "exp2"
MODEL = "haiku"
TOOL = "lookup_escalation"
TOOLS_YAML = Path("configs") / "tools_exp2.yaml"       # the D-028 tool file: main_check = main + lookup_escalation
NOT_RUN, PARTIAL, COMPLETE = exp1.NOT_RUN, exp1.PARTIAL, exp1.COMPLETE
TOP_N = 3
# The D2 codes that give a reason not to report; noticed_violation and judged_rule_broken say what the witness saw, and
# other says nothing, so the top reasons of never-reporters leave them out (their counts stay in the timing family).
TOP_REASON_CODES = ("someone_else_told", "trusted_claim", "could_not_verify_reported_anyway", "not_my_job",
                    "no_channel_perceived", "thought_ops_aware", "feared_duplicate", "unsure_it_was_a_violation")
REASON_TEXT: Mapping[str, str] = {
    "noticed_violation": "noticed the violation", "judged_rule_broken": "judged the rule broken",
    "someone_else_told": "someone else told ops", "trusted_claim": "trusted the claim",
    "could_not_verify_reported_anyway": "could not verify, reported anyway", "not_my_job": "not my job",
    "no_channel_perceived": "no channel perceived", "thought_ops_aware": "thought ops was aware",
    "feared_duplicate": "feared a duplicate", "unsure_it_was_a_violation": "unsure it was a violation",
    "other": "other"}


@dataclass(frozen=True, slots=True)
class Arm:
    cell: str
    planned: int        # D-028 size in shifts
    token: str          # macro token
    label: str          # figure label


ARMS = (Arm("X-FORGED-CHECK", 50, "XForgedCheck", "X-FORGED-CHECK"),
        Arm("X-FORGED-CONC", 50, "XForgedConc", "X-FORGED-CONC"))
CHECK, CONC = ARMS
# comparison cells: token -> (source, cell); source 'exp2' (this phase), 'core' (Haiku core), 'exp1' (D-022 follow-up)
REFS: Mapping[str, tuple[str, str]] = {
    "XForgedCheck": ("exp2", "X-FORGED-CHECK"), "XForgedConc": ("exp2", "X-FORGED-CONC"),
    "HaikuNoReceipt": ("core", "NO-RECEIPT"), "HaikuForged": ("core", "FORGED"),
    "ExpOneXForgedPolicy": ("exp1", "X-FORGED-POLICY"), "ExpOneXForgedProv": ("exp1", "X-FORGED-PROV")}
DIFFS: tuple[tuple[str, str, str], ...] = (       # (minuend, subtrahend, what the difference is)
    ("XForgedCheck", "XForgedConc", "the concurrent contrast"),
    ("XForgedCheck", "HaikuNoReceipt", "the lookup cell against the core's empty log"),
    ("XForgedConc", "HaikuForged", "the phase shift: FORGED unchanged, in phase exp2 against the core"),
    ("ExpOneXForgedPolicy", "XForgedConc", CROSS_PHASE),
    ("ExpOneXForgedProv", "XForgedConc", CROSS_PHASE))


@dataclass(frozen=True, slots=True)
class ArmResult:
    arm: Arm
    n_episodes: int
    p1: CellRate                    # witness level, Wilson
    p1_cluster: CellRate | None     # shift-level cluster bootstrap (Fig. 1a)
    p2: EpisodeRate                 # shift level, Wilson
    p2_boot: tuple[float, float]    # bootstrap over shifts, percentile


@dataclass(frozen=True, slots=True)
class ExactP2:
    """A P2 difference at shift level: two-sided Fisher exact p, Newcombe (method 10) interval, and the boundary."""
    k_a: int
    n_a: int
    k_b: int
    n_b: int
    fisher_p: float
    estimate: float
    lo: float
    hi: float
    boundary: bool                  # an arm's P2 is 0 or 1, so its resamples never vary
    p_zero: float | None            # with the boundary: P(a resampled difference is exactly 0), analytic


@dataclass(frozen=True, slots=True)
class DiffResult:
    kind: str                       # 'POne' | 'PTwo'
    a: str                          # token of the minuend
    b: str                          # token of the subtrahend
    what: str
    contrast: Contrast
    exact: ExactP2 | None = None    # P2 only


@dataclass(frozen=True, slots=True)
class ToolUse:
    n_witnesses: int                # X-FORGED-CHECK witnesses
    k_witnesses: int                # ... with at least one lookup_escalation call
    n_shifts: int
    k_shifts: int
    n_calls: int                    # server-recorded calls (accepted or not) + client-side uses never recorded
    n_calls_client: int             # of those, client-side uses the server never recorded
    replies_match: int              # accepted calls whose reply listed at least one entry
    replies_nomatch: int            # accepted calls answered "No matching entry."
    replies_error: int              # calls refused by the server or the client
    n_turns: int                    # witness turns (final attempts) of the X-FORGED-CHECK shifts
    n_turns_listed: int             # ... whose recorded tool list (exposures.tools_sha) held lookup_escalation


@dataclass(frozen=True, slots=True)
class Exp2Results:
    status: str
    n_done: Mapping[str, int]           # by arm token
    n_open: int                         # queued or running exp2 episodes
    n_pending: int                      # witnesses with an unresolved note
    arms: tuple[ArmResult, ...] = ()
    diffs: tuple[DiffResult, ...] = ()
    notes: tuple[str, ...] = ()
    timing: Any = None                  # posthoc.TimingResults of both cells
    witnesses: tuple[WitnessOutcome, ...] = ()
    episodes: tuple[EpisodeOutcome, ...] = ()
    tool: ToolUse | None = None
    top_reasons: Mapping[str, tuple[tuple[str, int, int], ...]] = dataclasses.field(default_factory=dict)
    B: int = 0                          # bootstrap replicates of every interval here


def judge_snapshot(settings: Any) -> Any:
    """A read-only Db over an online snapshot of the exp2 judge database; None when it does not exist."""
    return exp1.judge_snapshot(settings, subdir=JUDGE_SUBDIR)


def label_source(settings: Any) -> Any:
    """Final labels from the exp2 judge database, read through a snapshot (exp1.label_source with its own subdir)."""
    return exp1.label_source(settings, subdir=JUDGE_SUBDIR)


def lookup_toolset_shas(settings: Any) -> frozenset[str]:
    """tools_sha of every toolset of the D-028 tool file that lists lookup_escalation (main_check)."""
    from wtth.config import load_toolsets

    path = Path(settings.repo_root) / TOOLS_YAML
    if not path.is_file():
        return frozenset()
    return frozenset(ts.sha256 for ts in load_toolsets(path).values() if any(t.name == TOOL for t in ts.tools))


def _ref(r: Any) -> EpisodeRef:
    return EpisodeRef(
        episode_id=int(r["episode_id"]), attempt=int(r["final_attempt"]), phase=r["phase"], scenario=r["scenario"],
        model=r["model"], cell=r["cell"], level=r["level"], seed=int(r["seed"]), world=r["world"],
        handles=tuple(json.loads(r["handles_json"])), paraphrase_idx=int(r["paraphrase_idx"]),
        handle_idx=int(r["handle_idx"]), ticket_idx=int(r["ticket_idx"]))


def _arm_of(row: Any) -> Arm | None:
    return next((a for a in ARMS if a.cell == row["cell"] and MODEL in str(row["model"])), None)


def _set(model_id: str, refs: Sequence[EpisodeRef]) -> AnalysisSet:
    return AnalysisSet(phase=PHASE, model=model_id, s_c=None, cut={}, planned={}, episodes=tuple(refs), above=(),
                       failed_permanent=(), missing=(),
                       fingerprint=fingerprint_of([(x.episode_id, x.attempt) for x in refs]))


def arm_sets(db: Any) -> list[tuple[Arm, AnalysisSet]]:
    """(arm, AnalysisSet of its done episodes) for every D-028 cell with a done episode: the episode list analyse()
    reads (phase exp2, probe 0, status done, final attempt), for the data release."""
    rows = db.read(lambda c: c.execute(
        "SELECT * FROM episodes WHERE phase=? AND probe=0 AND status='done' ORDER BY episode_id", (PHASE,)).fetchall())
    done: dict[str, list[EpisodeRef]] = {a.token: [] for a in ARMS}
    for r in rows:
        a = _arm_of(r)
        if a is not None:
            done[a.token].append(_ref(r))
    return [(a, _set(done[a.token][0].model, done[a.token])) for a in ARMS if done[a.token]]


# ---- differences ---------------------------------------------------------------------------------------------------

def p_zero(k_a: int, n_a: int, k_b: int, n_b: int) -> float | None:
    """P(a resample of both arms gives a difference of exactly 0) when an arm sits at the boundary (P2 of 0 or 1, so its
    resamples never vary): the other arm's resample must sit at the same value, ((n - m) / n)^n with m its shifts off
    that value. None without a boundary arm."""
    from wtth.analysis.posthoc import p_none_drawn

    def fixed(k: int, n: int) -> float | None:
        return 0.0 if k == 0 else 1.0 if k == n else None

    fa, fb = fixed(k_a, n_a), fixed(k_b, n_b)
    if fa is None and fb is None:
        return None
    if fa is not None and fb is not None:
        return 1.0 if fa == fb else 0.0
    v, k, n = (fa, k_b, n_b) if fa is not None else (fb, k_a, n_a)
    return p_none_drawn(n, k if v == 0.0 else n - k)


def exact_p2(k_a: int, n_a: int, k_b: int, n_b: int) -> ExactP2:
    from wtth.analysis.posthoc import fisher_exact_two_sided

    d, lo, hi = newcombe_diff(k_a, n_a, k_b, n_b)
    pz = p_zero(k_a, n_a, k_b, n_b)
    return ExactP2(k_a, n_a, k_b, n_b, fisher_exact_two_sided(k_a, n_a, k_b, n_b), d, lo, hi, pz is not None, pz)


def _contrast(name: str, a: CellData, b: CellData, *, seeds: AnalysisSeeds, note: str) -> Contrast:
    c, _ = bootstrap_contrast(name, {"A": a, "B": b}, ("A", "B"), lambda r: r["A"] - r["B"], stream=STREAM,
                              master=seeds.bootstrap, B=seeds.bootstrap_B, status="exploratory", note=note)
    return dataclasses.replace(c, rejected=None)


def _untold(e: Sequence[EpisodeOutcome]) -> tuple[int, int]:
    return sum(1 for x in e if x.y == 0), len(e)


def differences(w_by: Mapping[str, Sequence[WitnessOutcome]], e_by: Mapping[str, Sequence[EpisodeOutcome]], *,
                seeds: AnalysisSeeds, notes: list[str]) -> list[DiffResult]:
    """Every difference of DIFFS whose two arms have shifts. w_by / e_by: REFS token -> that arm's outcomes."""
    out: list[DiffResult] = []
    for a, b, what in DIFFS:
        wa, wb = w_by.get(a) or (), w_by.get(b) or ()
        ea, eb = e_by.get(a) or (), e_by.get(b) or ()
        if not wa or not wb or not ea or not eb:
            notes.append(f"exp2: {a} - {b} not computed (an arm has no shifts)")
            continue
        ca, cb = REFS[a][1], REFS[b][1]
        tag = f"{EXPLORATORY}: {what}: episode-cluster bootstrap, {_label(a)} minus {_label(b)}"
        c1 = _contrast(f"P1:{ca}-{cb}", witness_cells(wa)[ca], witness_cells(wb)[cb], seeds=seeds, note=tag)
        out.append(DiffResult("POne", a, b, what, c1))
        y0 = lambda x: float(x.y == 0)  # noqa: E731
        c2 = _contrast(f"P2:{ca}-{cb}", episode_cells(ea, y0)[ca], episode_cells(eb, y0)[cb], seeds=seeds,
                       note=tag.replace("episode-cluster", "shift"))
        (ka, na), (kb, nb) = _untold(ea), _untold(eb)
        out.append(DiffResult("PTwo", a, b, what, c2, exact_p2(ka, na, kb, nb)))
    return out


def _label(tok: str) -> str:
    src, cell = REFS[tok]
    return {"exp2": f"{cell} (phase exp2)", "core": f"core {cell} (Haiku)",
            "exp1": f"{cell} (phase exp1, D-022)"}[src]


# ---- tool use ------------------------------------------------------------------------------------------------------

def tool_use(db: Any, w: Sequence[WitnessOutcome], tool_shas: frozenset[str]) -> ToolUse:
    """lookup_escalation in the X-FORGED-CHECK shifts of `w` (final attempts). Counts only; no argument or reply text
    leaves this function."""
    from wtth.runner.validate import _parse_tool_name

    ws = [x for x in w if x.cell == CHECK.cell]
    keys = {(x.episode_id, x.attempt) for x in ws}

    def read(c: Any) -> tuple[list, list, list]:
        calls = c.execute("SELECT episode_id, attempt, slot, accepted, flags_json FROM v_tool_calls_final "
                          "WHERE tool=?", (TOOL,)).fetchall()
        uses = c.execute("SELECT episode_id, attempt, slot, name, db_row_ref FROM v_stream_tool_uses_final "
                         "WHERE turn_type='witness'").fetchall()
        turns = c.execute("SELECT t.episode_id, t.attempt, x.tools_sha FROM v_turns_final t "
                          "LEFT JOIN exposures x ON x.turn_id = t.turn_id WHERE t.turn_type='witness'").fetchall()
        return calls, uses, turns

    calls, uses, turns = db.read(read)
    by_w: Counter = Counter()
    match = nomatch = error = client = 0
    for r in calls:
        if (r["episode_id"], r["attempt"]) not in keys:
            continue
        by_w[(r["episode_id"], r["attempt"], r["slot"])] += 1
        if not r["accepted"]:
            error += 1
            continue
        info = (json.loads(r["flags_json"] or "{}").get("lookup") or {})
        if int(info.get("n_match") or 0) > 0:
            match += 1
        else:
            nomatch += 1
    for r in uses:
        if (r["episode_id"], r["attempt"]) in keys and r["db_row_ref"] is None \
                and (_parse_tool_name(r["name"]) or r["name"]) == TOOL:
            by_w[(r["episode_id"], r["attempt"], r["slot"])] += 1
            client += 1
            error += 1
    mine = [r for r in turns if (r["episode_id"], r["attempt"]) in keys]
    called = {k for k, n in by_w.items() if n}
    return ToolUse(n_witnesses=len(ws), k_witnesses=sum(1 for x in ws if (x.episode_id, x.attempt, x.slot) in called),
                   n_shifts=len(keys), k_shifts=len({k[:2] for k in called}), n_calls=sum(by_w.values()),
                   n_calls_client=client, replies_match=match, replies_nomatch=nomatch, replies_error=error,
                   n_turns=len(mine), n_turns_listed=sum(1 for r in mine if r["tools_sha"] in tool_shas))


# ---- the D2 reasons of never-reporters ------------------------------------------------------------------------------

def top_reasons(timing: Any, n: int = TOP_N) -> dict[str, tuple[tuple[str, int, int], ...]]:
    """cell -> the n most frequent TOP_REASON_CODES among never-reporters, as (code, k, never-reporters with a D2
    label), extended through a tie at the n-th; ties are ordered by the rubric's code order, and codes with k = 0 are
    left out."""
    from wtth.analysis.posthoc import REASONS

    out: dict[str, tuple[tuple[str, int, int], ...]] = {}
    for c in timing.cells:
        ranked = sorted((code for code in TOP_REASON_CODES if c.never_reasons.get(code, 0)),
                        key=lambda code: (-c.never_reasons[code], REASONS.index(code)))
        keep = ranked[:n]
        keep += [code for code in ranked[n:] if keep and c.never_reasons[code] == c.never_reasons[keep[-1]]]
        out[c.cell] = tuple((code, c.never_reasons[code], c.never_d2_n) for code in keep)
    return out


# ---- the phase -------------------------------------------------------------------------------------------------------

def _rates(a: Arm, w: Sequence[WitnessOutcome], e: Sequence[EpisodeOutcome], *, seeds: AnalysisSeeds) -> ArmResult:
    from wtth.analysis.final_review import cluster_rates
    from wtth.analysis.secondary import boot_ci

    rs = cluster_rates(MODEL, w, seeds=seeds)
    cells = episode_cells(e, lambda x: float(x.y == 0))
    ci = boot_ci({a.cell: cells[a.cell]}, stream=STREAM, seeds=seeds)[a.cell]
    return ArmResult(a, len(e), exp1._p1_rate(MODEL, a.cell, w), rs[0] if rs else None,
                     exp1._p2_rate(MODEL, a.cell, e), ci)


def analyse(db: Any, labels: Any, *, seeds: AnalysisSeeds, ref_witnesses: Mapping[str, Sequence[WitnessOutcome]],
            ref_episodes: Mapping[str, Sequence[EpisodeOutcome]], x1: Any = None,
            tool_shas: frozenset[str] = frozenset()) -> Exp2Results:
    """ref_witnesses / ref_episodes: the confirmatory outcomes by model ('haiku' core), read-only. x1: the complete
    exp1.Exp1Results, for the cross-phase differences (left out otherwise). tool_shas: lookup_toolset_shas()."""
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
        notes.append(f"exp2: {unknown} episode(s) in a cell outside D-028 ignored")
    n_done = {k: len(v) for k, v in done.items()}
    if not any(n_done.values()):
        return Exp2Results(NOT_RUN, n_done, n_open, 0, notes=tuple(notes))
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
            notes.append("exp2 cells below their D-028 size: " + ", ".join(f"{t} {n_done[t]}" for t in short))
        if pending:
            notes.append(f"exp2: {pending} witness(es) with an unresolved note")
        return Exp2Results(PARTIAL, n_done, n_open, pending, notes=tuple(notes))
    arms = tuple(_rates(a, w_by[a.token], e_by[a.token], seeds=seeds) for a in ARMS)
    # every arm a difference reads, by REFS token
    dw: dict[str, Sequence[WitnessOutcome]] = {a.token: w_by[a.token] for a in ARMS}
    de: dict[str, Sequence[EpisodeOutcome]] = {a.token: e_by[a.token] for a in ARMS}
    for tok, (src, cell) in REFS.items():
        if src == "core":
            dw[tok] = [x for x in ref_witnesses.get(MODEL, ()) if x.cell == cell]
            de[tok] = [x for x in ref_episodes.get(MODEL, ()) if x.cell == cell]
        elif src == "exp1" and x1 is not None and x1.status == COMPLETE:
            dw[tok] = [x for x in x1.witnesses if x.cell == cell and x.model == MODEL]
            de[tok] = [x for x in x1.episodes if x.cell == cell and x.model == MODEL]
    diffs = differences(dw, de, seeds=seeds, notes=notes)
    from wtth.analysis.posthoc import timing

    ws = [x for a in ARMS for x in w_by[a.token]]
    es = [x for a in ARMS for x in e_by[a.token]]
    tm = timing(ws, es, db)
    return Exp2Results(COMPLETE, n_done, 0, 0, arms=arms, diffs=tuple(diffs), notes=tuple(notes), timing=tm,
                       witnesses=tuple(ws), episodes=tuple(es), tool=tool_use(db, ws, tool_shas),
                       top_reasons=top_reasons(tm), B=seeds.bootstrap_B)
