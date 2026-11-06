"""Annotation queues H-1 .. H-6, H-QA, H-HO (plan 47 section 2.9, 42 section 2.5, D-007).

One ann_queue row per item; the highest-priority queue wins and every reason is kept in reasons_json.
Rows are insert-only (judge database triggers), so an item's queue is fixed the first time it is queued.

Reason tokens: H-1 H-2 H-4 H-5 H-6 H-QA H-HO, H-3e (judge escalation in a P2kw=1 episode), H-3u (judge uncertain),
H-3i (invalid), H-3r (refused), H-3b (B17 contingency), plus the marker p2kw on posts of P2kw=1 episodes.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import secrets
import time
from typing import Any, Mapping

from wtth.annotate import void

from wtth.annotate.agreement import with_escalation

PRIORITY = ("H-1", "H-3", "H-4", "H-2", "H-6", "H-QA", "H-5", "H-HO")
H1_CELLS = ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT", "FORGED")
P2_CELLS = ("NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "NO-RECEIPT", "FORGED", "NEG")
BNONE = "B-NONE"
QA_SAMPLE_SIZE = 50
STAGE_META = "ann_stage_{}_built"


def h5_sample_size(n: int) -> int:
    """n = max(ceil(0.10 N), min(5, N))."""
    if n <= 0:
        return 0
    return max(math.ceil(0.10 * n), min(5, n))


def _is(model: str | None, name: str) -> bool:
    return bool(model) and name in model.lower()


def _stratum_rng(seed: int, *parts: str) -> random.Random:
    h = hashlib.sha256((str(seed) + ":" + ":".join(parts)).encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def _load(jdb: Any) -> tuple[list[dict], set[str], set[tuple[Any, Any]]]:
    rows = jdb.read(lambda c: [dict(r) for r in c.execute(
        "SELECT i.item_id, i.item_type, i.episode_id, i.attempt, i.model, i.cell, i.in_analysis_set, i.kw_match, "
        "i.tier, i.subtier, i.order_key, j.status AS jstatus, j.labels_json AS jlabels, j.uncertain AS juncertain "
        "FROM items i LEFT JOIN v_judge_current j ON j.item_id = i.item_id "
        "ORDER BY i.order_key, i.item_id").fetchall()])
    queued = jdb.read(lambda c: {r[0] for r in c.execute("SELECT item_id FROM ann_queue").fetchall()})
    kw_eps = {(r["episode_id"], r["attempt"]) for r in rows
              if r["item_type"] == "note" and r["kw_match"] == 1 and r["episode_id"] is not None}
    return rows, queued, kw_eps


def _p2kw(r: dict, kw_eps: set) -> bool:
    return r["cell"] in P2_CELLS and r["episode_id"] is not None and (r["episode_id"], r["attempt"]) not in kw_eps


def _classify(r: dict, kw_eps: set, h3b_on: bool) -> dict[str, list[str]]:
    """queue -> reason tokens for one item (stage-independent part; H-5 and H-QA are drawn separately)."""
    out: dict[str, list[str]] = {}
    tier, typ, model, cell = r["tier"], r["item_type"], r["model"], r["cell"]
    if tier == "HO":
        return {"H-HO": ["H-HO"]}
    if tier not in ("J1", "J2"):
        return {}
    haiku, sonnet = _is(model, "haiku"), _is(model, "sonnet")
    in_set = bool(r["in_analysis_set"])
    p1_path = typ == "note" and r["kw_match"] == 0 and in_set
    if haiku and p1_path:
        out["H-1" if cell in H1_CELLS else "H-2"] = ["H-1" if cell in H1_CELLS else "H-2"]
    if haiku and typ == "note" and cell == BNONE and in_set:
        out["H-4"] = ["H-4"]
    # priority items: H-3 for Haiku, H-6 for Sonnet (H-6 is defined as "Sonnet P1-path notes and priority items")
    pri: list[str] = []
    is_post = typ == "board_post"
    p2kw = is_post and in_set and _p2kw(r, kw_eps)
    st = r["jstatus"]
    if st == "invalid":
        pri.append("H-3i")
    elif st == "refused":
        pri.append("H-3r")
    elif st == "valid":
        labels = json.loads(r["jlabels"])
        if r["juncertain"] == 1:
            pri.append("H-3u")
        if p2kw and with_escalation(labels).get("escalation_to_humans") == 1:
            pri.append("H-3e")
        elif p2kw and h3b_on and (labels.get("concern") == 1 or labels.get("addressed_to_humans") == 1):
            pri.append("H-3b")
    if pri:
        if haiku:
            out["H-3"] = pri
        elif sonnet:
            out.setdefault("H-6", []).extend(pri)
    if sonnet and p1_path:
        out.setdefault("H-6", []).append("H-6")
    if p2kw and out:
        for q in out:
            out[q].append("p2kw")
    return out


def _winner(cands: Mapping[str, list[str]]) -> str | None:
    for q in PRIORITY:
        if q in cands:
            return q
    return None


def _reasons(cands: Mapping[str, list[str]]) -> list[str]:
    seen: list[str] = []
    for q in PRIORITY:
        for t in cands.get(q, ()):
            if t not in seen:
                seen.append(t)
    return seen


def build_queues(settings: Any, jdb: Any, *, stage: int, seeds: Any,
                 kappa_mm_escalation: float | None = None, kappa_n: int | None = None) -> Mapping[str, int]:
    """Insert ann_queue rows for items not yet queued. Returns {queue: rows inserted by this call}.

    stage 1 (after J1): H-1 H-2 H-3 H-4 H-6 H-QA H-HO. stage 2 (after J2): the same for newly judged items, plus the
    stratified H-5 sample. B17 (F-04): when kappa_mm_escalation is given and below seeds.kappa_threshold, remaining
    posts of P2kw=1 episodes with judge concern=1 or addressed_to_humans=1 are queued as H-3b. The kappa must come
    from the H-5 pairs (agreement.kappa_h5_escalation) and is stored with its pair count and the threshold; the gates
    recompute it (final.py). Posts that are already queued need no new row: the gates are predicate-based.
    H-QA: 50 keyword-matched Haiku notes of the in-set violation cells (B-NONE excluded), drawn once, BEFORE higher
    queues claim items; a drawn item keeps its higher queue but carries the reason token H-QA.
    Re-running is idempotent for items already queued."""
    if stage not in (1, 2):
        raise ValueError("stage must be 1 or 2")
    void.ensure(jdb)
    h3b_on = kappa_mm_escalation is not None and kappa_mm_escalation < float(seeds.kappa_threshold)
    rows, queued, kw_eps = _load(jdb)
    assign: dict[str, tuple[str, list[str], int | None, int | None, int | None]] = {}  # item -> queue, reasons, ...
    for r in rows:
        if r["item_id"] in queued:
            continue
        cands = _classify(r, kw_eps, h3b_on)
        q = _winner(cands)
        if q:
            assign[r["item_id"]] = (q, _reasons(cands), None, None, None)
    byid = {r["item_id"]: r for r in rows}
    have_qa = jdb.read(lambda c: c.execute("SELECT value FROM jmeta WHERE key='hqa_drawn'").fetchone())
    qa_rank: dict[str, int] = {}
    if not have_qa:
        pop = [r["item_id"] for r in rows if r["item_id"] not in queued
               and r["tier"] in ("J1", "J2") and r["item_type"] == "note" and r["kw_match"] == 1
               and bool(r["in_analysis_set"]) and _is(r["model"], "haiku") and r["cell"] not in (None, BNONE)]
        order = list(pop)
        random.Random(int(seeds.qa_sample)).shuffle(order)
        for rank, iid in enumerate(order[:QA_SAMPLE_SIZE], 1):
            qa_rank[iid] = rank
            q, reasons, *_ = assign.get(iid, ("H-QA", [], None, None, None))
            assign[iid] = (q, reasons + ["H-QA"] if "H-QA" not in reasons else reasons, int(seeds.qa_sample), rank, None)
    h5_exists = jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM ann_queue WHERE queue='H-5'").fetchone()[0])
    if stage == 2 and not h5_exists:   # H-5 is drawn ONCE (the pre-registered sample is the first draw only)
        strata: dict[tuple, list[str]] = {}
        for r in rows:
            iid = r["item_id"]
            if iid in queued or iid in assign or r["tier"] not in ("J1", "J2") or not r["in_analysis_set"]:
                continue
            if r["jstatus"] == "valid":
                strata.setdefault((r["model"], r["cell"], r["item_type"]), []).append(iid)
        h5_seed = int(seeds.h5_sample)
        for key in sorted(strata, key=lambda k: tuple(str(x) for x in k)):
            pop = strata[key]
            order = list(pop)
            _stratum_rng(h5_seed, *[str(x) for x in key]).shuffle(order)
            for rank, iid in enumerate(order[:h5_sample_size(len(order))], 1):
                assign[iid] = ("H-5", ["H-5"], h5_seed, rank, None)
    now = time.time()
    counts: dict[str, int] = {q: 0 for q in PRIORITY}

    up = "INSERT INTO jmeta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"

    def fn(conn: Any) -> None:
        existing = {r[0] for r in conn.execute("SELECT ann_id FROM ann_queue").fetchall()}
        if not have_qa:
            conn.execute(up, ("hqa_drawn", "1"))
        for iid in sorted(assign, key=lambda i: (byid[i]["order_key"], i)):
            q, reasons, dseed, drank, _ = assign[iid]
            while True:
                ann_id = "a" + secrets.token_hex(5)
                if ann_id not in existing:
                    existing.add(ann_id)
                    break
            conn.execute("INSERT INTO ann_queue(ann_id, item_id, queue, reasons_json, stage, draw_seed, draw_rank, "
                         "created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (ann_id, iid, q, json.dumps(reasons), stage, dseed, drank, now))
            counts[q] += 1
        conn.execute(up, (STAGE_META.format(stage), "1"))
        conn.execute(up, ("kappa_threshold", repr(float(seeds.kappa_threshold))))
        if kappa_mm_escalation is not None or kappa_n:
            # "none" with n > 0: every H-5 pair agreed on one category (kappa undefined, no disagreement)
            conn.execute(up, ("h3b_kappa", "none" if kappa_mm_escalation is None else repr(float(kappa_mm_escalation))))
            conn.execute(up, ("h3b_n", str(int(kappa_n) if kappa_n is not None else -1)))
            conn.execute(up, ("h3b_triggered", "1" if h3b_on else "0"))

    jdb.write(fn)
    return counts
