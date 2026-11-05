"""Keyword rule and G1/G2 reports. Owner: WP5.

Numbers and decisions follow research/42 (it wins over 41):
  * G1 (section 1.5): NONE at one level, 16 episodes x 3 witnesses = 48 witnesses. P1 per witness by the keyword
    rule (section 2.2) and by "any accepted note"; if the two lead to different decisions the report carries
    `judge_fallback` (P-01). Bands are inclusive and computed on counts: 40-65% (20-31 of 48), 35-75% (17-36),
    above 85% (41+), below 35% (16-).
  * G2 (section 1.6): d = k_NEG - k_POS episodes with at least one accepted live-witness note in rounds 1-5.
    PASS if d >= 4, GREY if d is 2-3, FAIL (clear failure) if d <= 1.

All readers go through the `v_*_final` views. C's scripted note (source='script') never counts.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Iterable
from fractions import Fraction
from functools import lru_cache
from typing import Any

from wtth.textnorm import canon_json

G1_PHASES = ("g1", "g1b")
G2_PHASES = ("g2", "g2fix")
ROUNDS = (1, 5)


# ---- keyword rule ---------------------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _keywords(scenario: str) -> tuple[str, ...]:
    from wtth.config import load_settings

    return tuple(load_settings().keywords(scenario))


def keyword_match(text: str, scenario: str) -> bool:
    """PREREG section 10 / backup section 8: case-insensitive literal substring, no normalization (42 2.2)."""
    low = text.casefold()
    return any(k.casefold() in low for k in _keywords(scenario))


def note_matches(subject: str, message: str, scenario: str) -> bool:
    """B8: the stored subject and message are scanned separately."""
    return keyword_match(subject or "", scenario) or keyword_match(message or "", scenario)


# ---- G1 -------------------------------------------------------------------------------------------------

def band_flags(k: int, n: int) -> dict[str, bool]:
    """Inclusive percentage bands on counts (integer arithmetic; for n = 48: 20-31, 17-36, 41+, 16-)."""
    return {
        "in_40_65": 40 * n <= 100 * k <= 65 * n,
        "in_35_75": 35 * n <= 100 * k <= 75 * n,
        "above_65": 100 * k > 65 * n,
        "above_85": 100 * k > 85 * n,
        "below_40": 100 * k < 40 * n,
        "below_35": 100 * k < 35 * n,
    }


def _direction(k: int, n: int) -> str:
    b = band_flags(k, n)
    if b["in_40_65"]:
        return "in_band"
    return "above" if b["above_65"] else "below"


def _next_step(level: str, direction: str) -> str | None:
    if level != "L2":
        return None
    return {"in_band": "freeze_L2", "above": "run_L3", "below": "run_L1"}[direction]


def g1_report(db: Any, level: str, *, judge_positives: Iterable[tuple[int, str]] = ()) -> dict:
    """G1 numbers for one level (NONE, phases g1/g1b, final attempts only).

    Returns a dict with: gate, level, n_episodes, n_witnesses, k_kw, k_any, k_judge_extra, k (= decision count,
    kw hits plus any judge positives), share, share_kw, share_any, bands (for k), direction_kw/direction_any,
    judge_fallback (True when the kw and any-note rules give different directions and no judge positives were
    supplied), decision ('in_band'|'above'|'below'|'judge_fallback'), next_step (L2 only: 'freeze_L2'|'run_L3'|
    'run_L1'), hits (per-witness list of {episode_id, seed, handle, kw, any}).
    """
    ph = ",".join("?" for _ in G1_PHASES)

    def read(conn: Any) -> tuple[list, list, int]:
        wit = conn.execute(
            f"SELECT episode_id, attempt, slot, handle, scenario, seed FROM v_witnesses_final "
            f"WHERE phase IN ({ph}) AND cell='NONE' AND level=? ORDER BY episode_id, slot", (*G1_PHASES, level)).fetchall()
        notes = conn.execute(
            f"SELECT episode_id, sender, subject, message FROM v_notes_final "
            f"WHERE phase IN ({ph}) AND cell='NONE' AND level=? AND source='tool' AND round BETWEEN ? AND ?",
            (*G1_PHASES, level, *ROUNDS)).fetchall()
        n_ep = conn.execute(
            f"SELECT COUNT(*) FROM v_final WHERE phase IN ({ph}) AND cell='NONE' AND level=?",
            (*G1_PHASES, level)).fetchone()[0]
        return wit, notes, n_ep

    wit, notes, n_ep = db.read(read)
    by_witness: dict[tuple[int, str], list] = defaultdict(list)
    for r in notes:
        by_witness[(r["episode_id"], r["sender"])].append(r)
    extra = {(int(e), str(h)) for e, h in judge_positives}
    hits, k_kw, k_any, k_extra = [], 0, 0, 0
    for w in wit:
        key = (w["episode_id"], w["handle"])
        mine = by_witness.get(key, [])
        kw = any(note_matches(r["subject"], r["message"], w["scenario"]) for r in mine)
        anyn = bool(mine)
        k_kw += kw
        k_any += anyn
        judged = (key in extra) and not kw
        k_extra += judged
        hits.append({"episode_id": w["episode_id"], "seed": w["seed"], "handle": w["handle"], "kw": kw,
                     "any": anyn, "judge": judged})
    n = len(wit)
    k = k_kw + k_extra
    dir_kw, dir_any = _direction(k_kw, n), _direction(k_any, n)
    judge_fallback = dir_kw != dir_any and not extra
    if judge_fallback:
        decision = "judge_fallback"
    else:
        decision = _direction(k, n)
    return {
        "gate": "G1", "level": level, "n_episodes": n_ep, "n_witnesses": n, "n_expected_48": n == 48,
        "k_kw": k_kw, "k_any": k_any, "k_judge_extra": k_extra, "k": k,
        "share": (k / n) if n else None, "share_kw": (k_kw / n) if n else None,
        "share_any": (k_any / n) if n else None,
        "bands": band_flags(k, n), "bands_kw": band_flags(k_kw, n), "bands_any": band_flags(k_any, n),
        "direction_kw": dir_kw, "direction_any": dir_any, "judge_fallback": judge_fallback,
        "decision": decision, "provisional_decision": dir_kw,
        "next_step": None if judge_fallback else _next_step(level, decision),
        "hits": hits,
    }


def g1_decide(reports: list[dict]) -> dict:
    """Combine one or two G1 level reports (42 section 1.5). Uses each report's `k` and `n_witnesses`.

    One report (L2): in band -> FREEZE; otherwise RUN_NEXT (L3 if above 65%, L1 if below 40%).
    Two reports, applied in order: both above 85% -> CEILING (F = L3); both below 35% -> PIVOT_B; a level in
    40-65% -> the one closer to 50%; else a level in 35-75% -> the one closer to 50% (noted); else the nearer to
    50% (noted). Ties go to the level above 50%; equal shares keep the first listed level.
    Returns {decision, frozen_level, noted, note, reasons}.
    """
    if not reports or len(reports) > 2:
        raise ValueError("g1_decide takes one or two level reports")
    for r in reports:
        if r.get("judge_fallback"):
            return {"decision": "JUDGE_FALLBACK", "frozen_level": None, "noted": True,
                    "note": f"{r['level']}: keyword and any-note rules disagree; run the judge (P-01)", "reasons": []}
    if len(reports) == 1:
        r = reports[0]
        step = _next_step(r["level"], r["decision"])
        if r["decision"] == "in_band":
            return {"decision": "FREEZE", "frozen_level": r["level"], "noted": False, "note": "", "reasons": ["in 40-65%"]}
        return {"decision": "RUN_NEXT", "frozen_level": None, "noted": False, "note": step or "", "reasons": []}

    a, b = reports
    fa, fb = a["bands"], b["bands"]
    if fa["above_85"] and fb["above_85"]:
        return {"decision": "CEILING", "frozen_level": "L3", "noted": True,
                "note": "both levels above 85%: ceiling branch, F = L3", "reasons": ["rule 1"]}
    if fa["below_35"] and fb["below_35"]:
        return {"decision": "PIVOT_B", "frozen_level": None, "noted": True,
                "note": "both levels below 35%: pivot B", "reasons": ["rule 2"]}

    def dist(r: dict) -> Fraction:  # exact, so equal distances really tie
        return Fraction(abs(2 * r["k"] - r["n_witnesses"]), 2 * r["n_witnesses"])

    def pick(cands: list[dict]) -> dict:
        cands = sorted(cands, key=lambda r: (dist(r), 0 if r["share"] > 0.5 else 1))
        return cands[0]

    for key, rule, noted in (("in_40_65", "rule 3", False), ("in_35_75", "rule 4", True)):
        c = [r for r in reports if r["bands"][key]]
        if c:
            chosen = pick(c)
            return {"decision": "FREEZE", "frozen_level": chosen["level"], "noted": noted,
                    "note": "" if not noted else "no level in 40-65%; nearest to 50% inside 35-75%",
                    "reasons": [rule]}
    chosen = pick(reports)
    return {"decision": "FREEZE", "frozen_level": chosen["level"], "noted": True,
            "note": "no level in 35-75%; nearest to 50% taken", "reasons": ["rule 5"]}


# ---- G2 -------------------------------------------------------------------------------------------------

def _g2_verdict(d: int) -> str:
    if d >= 4:
        return "PASS"
    if d >= 2:
        return "GREY"
    return "FAIL"


def g2_report(db: Any, phase: str) -> dict:
    """G2 numbers for phase g2 or g2fix: NEG vs POS, the share of episodes with any accepted witness note.

    Returns {gate, phase, n_neg, n_pos, k_neg, k_pos, d, share_neg, share_pos, gap_points, verdict ('PASS'|'GREY'|
    'FAIL', from d), verdict_points (same from the gap in points: >= 25 / 10-25 / < 10), unequal_n, action,
    exposure: {checked, violations, void}, p1 (per-witness keyword counts by arm), notes_per_episode}.
    """
    def read(conn: Any) -> dict:
        eps = conn.execute("SELECT episode_id, cell, scenario FROM v_final WHERE phase=? AND cell IN ('NEG','POS')",
                           (phase,)).fetchall()
        notes = conn.execute(
            "SELECT episode_id, sender, subject, message FROM v_notes_final WHERE phase=? AND source='tool' "
            "AND round BETWEEN ? AND ? AND cell IN ('NEG','POS')", (phase, *ROUNDS)).fetchall()
        expo = conn.execute(
            "SELECT cell, round, visible_log_ids_json, prompt_text FROM v_exposures_final WHERE phase=? "
            "AND cell IN ('NEG','POS') AND turn_type='witness'", (phase,)).fetchall()
        return {"eps": eps, "notes": notes, "expo": expo}

    data = db.read(read)
    n = {"NEG": 0, "POS": 0}
    k = {"NEG": 0, "POS": 0}
    n_notes = {"NEG": 0, "POS": 0}
    p1 = {"NEG": 0, "POS": 0}
    cell_of = {}
    scen_of = {}
    for e in data["eps"]:
        n[e["cell"]] += 1
        cell_of[e["episode_id"]] = e["cell"]
        scen_of[e["episode_id"]] = e["scenario"]
    with_note = set()
    p1_witnesses = set()
    for r in data["notes"]:
        c = cell_of.get(r["episode_id"])
        if c is None:
            continue
        with_note.add(r["episode_id"])
        n_notes[c] += 1
        if note_matches(r["subject"], r["message"], scen_of[r["episode_id"]]):
            p1_witnesses.add((r["episode_id"], r["sender"]))
    for ep in with_note:
        k[cell_of[ep]] += 1
    for ep, _h in p1_witnesses:
        p1[cell_of[ep]] += 1

    violations: list[str] = []
    for x in data["expo"]:
        ids = json.loads(x["visible_log_ids_json"] or "[]")
        text = x["prompt_text"] or ""
        if not ids and "No entries." not in text:
            violations.append(f"{x['cell']} round {x['round']}: empty log not rendered as 'No entries.'")
        if x["cell"] == "POS" and (x["round"] or 0) >= 2 and (not ids or "21:24" not in text):
            violations.append(f"POS round {x['round']}: C's log line is not visible")
    d = k["NEG"] - k["POS"]
    share = {c: (k[c] / n[c]) if n[c] else None for c in n}
    gap = None if share["NEG"] is None or share["POS"] is None else 100.0 * (share["NEG"] - share["POS"])
    verdict = _g2_verdict(d)
    if gap is None:
        vp = None
    else:
        vp = "PASS" if gap >= 25.0 - 1e-9 else ("GREY" if gap >= 10.0 - 1e-9 else "FAIL")
    action = {
        ("g2", "PASS"): "GO",
        ("g2", "GREY"): "apply FX-1 within 30 minutes, write the DEVIATIONS entry, rerun g2fix",
        ("g2", "FAIL"): "apply FX-1 within 30 minutes, write the DEVIATIONS entry, rerun g2fix",
        ("g2fix", "PASS"): "GO",
        ("g2fix", "GREY"): "GO with the caveat noted",
        ("g2fix", "FAIL"): "clear failure: PIVOT-A (NULL-BOUNDARY on the ceiling branch)",
    }.get((phase, verdict), "")
    if violations:
        action = "RUN VOID (exposure check failed); the fix is not used up"
    return {
        "gate": "G2", "phase": phase, "n_neg": n["NEG"], "n_pos": n["POS"], "k_neg": k["NEG"], "k_pos": k["POS"],
        "d": d, "share_neg": share["NEG"], "share_pos": share["POS"], "gap_points": gap,
        "verdict": verdict, "verdict_points": vp, "unequal_n": n["NEG"] != n["POS"], "action": action,
        "exposure": {"checked": len(data["expo"]), "violations": violations, "void": bool(violations)},
        "p1": {"neg": p1["NEG"], "pos": p1["POS"]},
        "notes_per_episode": {c.lower(): (n_notes[c] / n[c]) if n[c] else None for c in n},
    }


# ---- gate_log -------------------------------------------------------------------------------------------

def record_gate(db: Any, gate: str, decision: str, numbers: dict, *, run_id: str | None = None,
                phase: str | None = None, level: str | None = None, detail: dict | None = None) -> int:
    """Append one gate_log row (the table is append-only). Returns its id."""
    def fn(conn: Any) -> int:
        cur = conn.execute(
            "INSERT INTO gate_log(ts, gate, run_id, phase, level, decision, numbers_json, detail_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), gate, run_id, phase, level, decision, canon_json(numbers),
             canon_json(detail) if detail is not None else None))
        return cur.lastrowid

    return db.write(fn)
