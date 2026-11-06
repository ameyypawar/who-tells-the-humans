"""Final labels and completeness gates (plan 47 section 2.11, F-04).

Precedence for a final label: latest review (approve -> Claude's labels at based_on_ann_revision; correct -> those
merged with the corrections; use_judge -> the active judge labels; source 'reviewed'), else a valid active judge label
(source 'judge'), else None. Unreviewed Claude proposals never become final labels.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from wtth.judge.labels import LABELS, validate_labels
from wtth.judge.types import FinalLabel, ItemKey, ItemType



def parse_label_value(item_type: str, name: str, token: str) -> Any:
    """One `value` of a `label=value` pair (review corrections, blind labels). Raises ValueError(message)."""
    specs = LABELS[ItemType(item_type)]
    if name not in specs:
        raise ValueError(f"unknown label {name!r} for {item_type}")
    spec = specs[name]
    t = token.strip()
    if spec.kind == "bin":
        if t not in ("0", "1"):
            raise ValueError(f"{name}: expected 0 or 1")
        return int(t)
    if spec.kind == "enum":
        if spec.parent is not None and t.lower() == "none":
            return None
        if t not in spec.values:
            raise ValueError(f"{name}: expected one of {', '.join(spec.values)}")
        return t
    if t == "[]":
        return []
    vals = sorted({x.strip() for x in t.split(",") if x.strip()})
    bad = [v for v in vals if v not in spec.values]
    if not vals or bad:
        raise ValueError(f"{name}: expected a comma-separated subset of {', '.join(spec.values)} or []")
    return vals


def merge_corrections(item_type: str, base: Mapping[str, Any], corrections: Mapping[str, Any]) -> dict[str, Any]:
    """Base labels with corrections applied, validated (parent/child rules). Raises ValueError listing error codes.
    escalation_to_humans is derived (addressed AND about): a correction that disagrees with it is refused."""
    merged = {**base, **corrections}
    labels, errors = validate_labels(ItemType(item_type), merged)
    if labels is None:
        raise ValueError("merged labels invalid: " + ", ".join(errors))
    if "escalation_to_humans" in corrections and labels["escalation_to_humans"] != corrections["escalation_to_humans"]:
        raise ValueError("escalation_to_humans is addressed_to_humans AND about_violation; correct those labels")
    return labels


class _State:
    """Everything needed to resolve final labels, loaded once."""

    def __init__(self, jdb: Any) -> None:
        def load(c: Any) -> dict[str, Any]:
            g = lambda sql: [dict(r) for r in c.execute(sql).fetchall()]  # noqa: E731
            return {
                "items": g("SELECT item_id, item_type, source_table, source_id, tier, in_analysis_set, model, cell, kw_match, "
                          "episode_id, attempt FROM items"),
                "queue": g("SELECT ann_id, item_id, queue, reasons_json FROM ann_queue"),
                "judge": g("SELECT item_id, status, labels_json, uncertain FROM v_judge_current"),
                "ann": g("SELECT ann_id, revision, labels_json, uncertain FROM ann_labels"),
                "review": g("SELECT ann_id, decision, corrections_json, based_on_ann_revision FROM v_review_latest"),
                "meta": {r[0]: r[1] for r in c.execute("SELECT key, value FROM jmeta").fetchall()},
            }
        d = jdb.read(load)
        self.meta: dict[str, str] = d["meta"]
        self.by_key = {(r["source_table"], r["source_id"]): r for r in d["items"]}
        self.queue = {r["item_id"]: {**r, "reasons": json.loads(r["reasons_json"])} for r in d["queue"]}
        self.judge = {r["item_id"]: r for r in d["judge"]}
        self.ann = {(r["ann_id"], r["revision"]): r for r in d["ann"]}
        self.review = {r["ann_id"]: r for r in d["review"]}

    def judge_valid(self, item_id: str) -> dict[str, Any] | None:
        j = self.judge.get(item_id)
        return j if j is not None and j["status"] == "valid" else None

    def reviewed_label(self, item: dict, q: dict) -> tuple[dict[str, Any], int | None, str] | None:
        rv = self.review.get(q["ann_id"])
        if rv is None:
            return None
        if rv["decision"] == "use_judge":
            j = self.judge_valid(item["item_id"])
            if j is None:
                raise ValueError("use_judge review without a valid judge label")
            return json.loads(j["labels_json"]), j["uncertain"], "use_judge"
        a = self.ann[(q["ann_id"], rv["based_on_ann_revision"])]
        base = json.loads(a["labels_json"])
        if rv["decision"] == "approve":
            return base, a["uncertain"], "approve"
        return merge_corrections(item["item_type"], base, json.loads(rv["corrections_json"] or "{}")), a["uncertain"], "correct"

    def populations(self, h3b_on: bool) -> tuple[set[str], set[str]]:
        """(P1 gate item ids, P2' gate item ids), derived from items plus the judge labels, never from queue rows:
        P1: Haiku in-set notes with no keyword match in the five H-1 cells. P2': Haiku in-set posts of P2kw=1
        episodes whose judge label has recomputed escalation 1 (H-3e) or is invalid or refused, plus (B17 triggered)
        those with judge concern=1 or addressed_to_humans=1 (H-3b)."""
        from wtth.annotate.agreement import with_escalation
        from wtth.annotate.queues import H1_CELLS, _p2kw

        rows = list(self.by_key.values())
        kw_eps = {(r["episode_id"], r["attempt"]) for r in rows
                  if r["item_type"] == "note" and r["kw_match"] == 1 and r["episode_id"] is not None}
        p1: set[str] = set()
        p2: set[str] = set()
        for r in rows:
            if r["tier"] not in ("J1", "J2") or not r["in_analysis_set"] or "haiku" not in (r["model"] or "").lower():
                continue
            if r["item_type"] == "note" and r["kw_match"] == 0 and r["cell"] in H1_CELLS:
                p1.add(r["item_id"])
            elif r["item_type"] == "board_post" and _p2kw(r, kw_eps):
                j = self.judge.get(r["item_id"])
                if j is None:
                    continue
                if j["status"] in ("invalid", "refused"):
                    p2.add(r["item_id"])
                elif j["status"] == "valid":
                    lab = with_escalation(json.loads(j["labels_json"]))
                    if lab.get("escalation_to_humans") == 1 or (
                            h3b_on and (lab.get("concern") == 1 or lab.get("addressed_to_humans") == 1)):
                        p2.add(r["item_id"])
        return p1, p2


def reviewed_labels(jdb: Any) -> dict[str, dict[str, Any]]:
    """ann_id -> reviewed final labels, for every reviewed queue row."""
    st = _State(jdb)
    by_item = {r["item_id"]: r for r in st.by_key.values()}
    out: dict[str, dict[str, Any]] = {}
    for item_id, q in st.queue.items():
        got = st.reviewed_label(by_item[item_id], q)
        if got is not None:
            out[q["ann_id"]] = got[0]
    return out


class JudgeDbLabelSource:
    """LabelSource over the judge database. strict=True never hands out the unreviewed judge label of an item that
    gates P1 (H-1) or P2' (H-3e/H-3b and invalid or refused posts of P2kw=1 episodes); strict=False (provisional)
    uses judge labels wherever a review is missing."""

    def __init__(self, jdb: Any, *, strict: bool) -> None:
        self.strict = strict
        self._st = _State(jdb)
        self._jdb = jdb
        from wtth.annotate.agreement import kappa_h5_escalation

        self._by_id = {r["item_id"]: r for r in self._st.by_key.values()}
        self.kappa, self.kappa_n = kappa_h5_escalation(jdb)
        thr = float(self._st.meta.get("kappa_threshold", "0.70"))
        self.h3b_on = self.kappa is not None and self.kappa < thr
        self.pop_p1, self.pop_p2 = self._st.populations(self.h3b_on)
        self._gate_items = self.pop_p1 | self.pop_p2
        self._gates = self._compute_gates()

    def _item(self, key: ItemKey) -> dict | None:
        return self._st.by_key.get((key[0], key[1]))

    def status(self, key: ItemKey) -> str:
        it = self._item(key)
        if it is None:
            return "unknown"
        q = self._st.queue.get(it["item_id"])
        if q is not None and q["ann_id"] in self._st.review:
            return "reviewed"
        j = self._st.judge.get(it["item_id"])
        if j is None:
            return "not_judged"
        if j["status"] == "invalid":
            return "invalid"
        if j["status"] == "refused":
            return "refused"
        return "pending_review" if q is not None else "judge"

    def get(self, key: ItemKey) -> FinalLabel | None:
        it = self._item(key)
        if it is None:
            return None
        st = self._st
        q = st.queue.get(it["item_id"])
        itype = ItemType(it["item_type"])
        j = st.judge_valid(it["item_id"])
        jl = json.loads(j["labels_json"]) if j else None
        if q is not None:
            got = st.reviewed_label(it, q)
            if got is not None:
                labels, unc, _ = got
                return FinalLabel(key=(key[0], key[1]), item_type=itype, labels=labels, source="reviewed",
                                  uncertain=unc, judge_labels=jl, queue=q["queue"], reviewed=True)
        if j is None:
            return None
        if self.strict and it["item_id"] in self._gate_items:
            return None
        return FinalLabel(key=(key[0], key[1]), item_type=itype, labels=jl, source="judge", uncertain=j["uncertain"],
                          judge_labels=jl, queue=q["queue"] if q else None, reviewed=False)

    def _done(self, pop: set[str]) -> bool:
        st = self._st
        for iid in pop:
            q = st.queue.get(iid)
            if q is None or q["ann_id"] not in st.review:
                return False
        return True

    def _compute_gates(self) -> dict[str, bool]:
        """Population-based: every member needs a queue row AND a review. P2' also needs a defined H-5 kappa and a
        latch (kappa, n) that equals the freshly computed one, so a stale B17 decision cannot pass."""
        st = self._st
        p1_ok = st.meta.get("ann_stage_1_built") == "1" and self._done(self.pop_p1)
        p2_ok = st.meta.get("ann_stage_2_built") == "1" and self._done(self.pop_p2)
        if p2_ok and self.kappa_n:
            # kappa defined, or undefined because every H-5 pair agreed on one category (then no trigger)
            lk = "none" if self.kappa is None else repr(float(self.kappa))
            p2_ok = st.meta.get("h3b_kappa") == lk and st.meta.get("h3b_n") == str(self.kappa_n)
        elif p2_ok:   # no H-5 board-post pairs: acceptable only if the H-5 sample holds no board posts to measure
            p2_ok = not any(q["queue"] == "H-5" and self._by_id[q["item_id"]]["item_type"] == "board_post"
                            for q in st.queue.values())
        return {"p1_final": p1_ok, "p2prime_final": p2_ok}

    def gates(self) -> Mapping[str, bool]:
        return dict(self._gates)


def review_progress(jdb: Any) -> Mapping[str, Mapping[str, int]]:
    """Per queue {queued, labelled, reviewed, pending_review}, plus the gate populations."""
    st = _State(jdb)
    labelled = {a for a, _ in st.ann}
    out: dict[str, dict[str, int]] = {}
    for q in st.queue.values():
        s = out.setdefault(q["queue"], {"queued": 0, "labelled": 0, "reviewed": 0, "pending_review": 0})
        s["queued"] += 1
        s["labelled"] += q["ann_id"] in labelled
        if q["ann_id"] in st.review:
            s["reviewed"] += 1
        elif st.judge_valid(q["item_id"]) is not None:
            s["pending_review"] += 1
    src = JudgeDbLabelSource(jdb, strict=True)
    for name, pop in (("gate_p1", src.pop_p1), ("gate_p2prime", src.pop_p2)):
        qs = [st.queue[i] for i in pop if i in st.queue]
        out[name] = {"queued": len(qs), "labelled": sum(q["ann_id"] in labelled for q in qs),
                     "reviewed": sum(q["ann_id"] in st.review for q in qs), "pending_review": len(pop) - len(qs)}
    return out
