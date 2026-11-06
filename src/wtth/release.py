"""Public data release (data/release/). Extends wtth.redact (same _Redactor, same variant forms) and reuses the
fragment scan of wtth.annotate.masking; it does not copy runs/ wholesale (build_release does that), it exports
curated tables: outcomes, labels and masked transcripts.

Masking rule (release): every withheld string is replaced exactly (raw and both JSON-escaped forms) by
[WITHHELD:claim-paraphrase] (C2.P2/C2.P3 and backups) or [WITHHELD:forged-line] (FORGED_LINE/BK.FORGED_LINE). The
30-character fragment rule runs for both classes; for the forged line it exempts spans that also occur in the
pre-registered public files (prereg/PREREG.md, prereg/prompts/*), which print the public log layout. Bare entry ids
are never masked. Private strings are never printed, logged or put in exceptions.
"""
from __future__ import annotations

import csv
import gzip
import json
import re
from collections import Counter
from dataclasses import fields
from pathlib import Path
from typing import Any

from wtth.annotate.masking import MIN_FRAGMENT, FRAGMENT_PLACEHOLDER, _fold, _fragment_scan
from wtth.redact import _Redactor, _variants

PH_CLAIM = "[WITHHELD:claim-paraphrase]"
PH_FORGED = "[WITHHELD:forged-line]"
_PH_RE = re.compile(r"(\[WITHHELD:(?:claim-paraphrase|forged-line)\])")


def _is_forged(pid: str) -> bool:
    return pid.endswith("FORGED_LINE")


class ReleaseMasker:
    def __init__(self, settings: Any) -> None:
        from wtth.annotate.masking import make_masker
        from wtth.prereg import load_private

        self.private = {k: v for k, v in load_private(settings).items() if v}
        base = make_masker(settings).public_lines
        pre = Path(settings.paths.prereg_dir)
        files = [pre / "PREREG.md", *sorted((pre / "prompts").glob("*"))]
        pubtexts = tuple(f.read_text(encoding="utf-8") for f in files if f.is_file())
        self.public_claim = tuple(base)
        self.public_forged = tuple(base) + pubtexts
        self._pairs = sorted(((pid, v) for pid, val in self.private.items() for v in _variants(val)),
                             key=lambda p: -len(p[1]))
        self._pf_claim = [_fold(v) for k, v in self.private.items() if not _is_forged(k)]
        self._pf_forged = [_fold(v) for k, v in self.private.items() if _is_forged(k)]
        self._grams_claim = self._grams(self._pf_claim)
        self._grams_forged = self._grams(self._pf_forged)
        self.counts: Counter = Counter()      # (cell, kind) -> n ; kind in exact-claim, exact-forged, frag-claim, frag-forged

    @staticmethod
    def _grams(folded: list[str]) -> set[str]:
        g: set[str] = set()
        for f in folded:
            for i in range(len(f) - MIN_FRAGMENT + 1):
                g.add(f[i:i + MIN_FRAGMENT])
        return g

    @staticmethod
    def _may_share(seg: str, grams: set[str]) -> bool:
        f = _fold(seg)
        return any(f[i:i + MIN_FRAGMENT] in grams for i in range(len(f) - MIN_FRAGMENT + 1))

    def _frag(self, text: str, cell: str, pf: list[str], grams: set[str], public: tuple[str, ...],
              ph: str, kind: str) -> str:
        parts = _PH_RE.split(text)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            if len(seg) < MIN_FRAGMENT or not self._may_share(seg, grams):
                continue
            new, k = _fragment_scan(seg, pf, public)
            if k:
                self.counts[(cell, kind)] += k
                parts[i] = new.replace(FRAGMENT_PLACEHOLDER, ph)
        return "".join(parts)

    def mask(self, text: str | None, cell: str) -> str | None:
        if not text:
            return text
        if FRAGMENT_PLACEHOLDER in text or "[WITHHELD:" in text:
            raise ValueError("source text contains a placeholder literal; refusing to mask ambiguously")
        for pid, needle in self._pairs:
            c = text.count(needle)
            if c:
                forged = _is_forged(pid)
                self.counts[(cell, "exact-forged" if forged else "exact-claim")] += c
                text = text.replace(needle, PH_FORGED if forged else PH_CLAIM)
        text = self._frag(text, cell, self._pf_claim, self._grams_claim, self.public_claim, PH_CLAIM, "frag-claim")
        text = self._frag(text, cell, self._pf_forged, self._grams_forged, self.public_forged, PH_FORGED, "frag-forged")
        return text

    def residual(self, data: bytes) -> dict[str, int]:
        return _Redactor(dict(self.private)).residual(data)

    def fragment_hits(self, text: str) -> int:
        """Fragment scan over an exported text: spans >= MIN_FRAGMENT shared with a private string that are not
        exempt for their class. 0 means clean."""
        n = 0
        for pf, grams, pub in ((self._pf_claim, self._grams_claim, self.public_claim),
                               (self._pf_forged, self._grams_forged, self.public_forged)):
            for seg in _PH_RE.split(text)[0::2]:
                if len(seg) >= MIN_FRAGMENT and self._may_share(seg, grams):
                    _, k = _fragment_scan(seg, pf, pub)
                    n += k
        return n


# ---- outcome tables --------------------------------------------------------------------------------------------

def _flat(v: Any) -> Any:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (tuple, list)):
        return "|".join(str(x) for x in v)
    return v


def write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> int:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def outcome_tables(settings: Any, db: Any, labels: Any, out: Path) -> dict[str, Any]:
    from wtth.analysis.outcomes import episode_outcomes, witness_outcomes
    from wtth.analysis.sets import analysis_set

    wrows: list[list[Any]] = []
    erows: list[list[Any]] = []
    whead = ehead = None
    sets = {}
    for phase in ("core", "sonnet"):
        aset = analysis_set(settings, db, phase)
        sets[phase] = aset
        w = witness_outcomes(db, aset, labels, strict=True)
        e = episode_outcomes(w, db, aset, labels, strict=True)
        wskip = {"notes", "posts"}
        whead = [f.name for f in fields(w[0]) if f.name not in wskip] + ["n_posts"]
        for x in w:
            wrows.append([_flat(getattr(x, f.name)) for f in fields(x) if f.name not in wskip] + [len(x.posts)])
        ehead = ["phase"] + [f.name for f in fields(e[0])]
        for x in e:
            erows.append([phase] + [_flat(getattr(x, f.name)) for f in fields(x)])
    n_w = write_csv(out / "outcomes_per_witness.csv", whead, wrows)
    n_e = write_csv(out / "outcomes_per_shift.csv", ehead, erows)
    return {"witness_rows": n_w, "shift_rows": n_e, "sets": sets, "wrows": wrows, "whead": whead,
            "erows": erows, "ehead": ehead}


# ---- labels ----------------------------------------------------------------------------------------------------

def label_tables(jdb: Any, labels: Any, episodes: dict[tuple[int, int], dict], out: Path) -> dict[str, int]:
    items = [dict(r) for r in jdb.read(lambda c: c.execute(
        "SELECT item_id,item_type,source_table,source_id,episode_id,attempt,round,slot,handle,kw_match,tier "
        "FROM items WHERE source_table != 'held_out' ORDER BY order_key, item_id").fetchall())]
    items = [i for i in items if (i["episode_id"], i["attempt"]) in episodes]
    judge = {r["item_id"]: dict(r) for r in jdb.read(lambda c: c.execute(
        "SELECT item_id,prompt_version,status,labels_json,confidence,uncertain FROM v_judge_current").fetchall())}

    def lab(d: dict | None) -> dict[str, Any]:
        return {k: _flat(v) for k, v in (d or {}).items()}

    jrows, frows = [], []
    keys: set[str] = set()
    for it in items:
        j = judge.get(it["item_id"])
        if j and j["status"] == "valid":
            keys |= set(json.loads(j["labels_json"]))
        fl = labels.get((it["source_table"], it["source_id"]))
        if fl is not None:
            keys |= set(fl.labels)
    lcols = sorted(keys)
    base = ["item_id", "item_type", "source_table", "source_id", "episode_id", "attempt", "phase", "model", "cell",
            "seed", "world", "round", "slot", "handle", "kw_match"]

    def head(it: dict) -> list[Any]:
        e = episodes[(it["episode_id"], it["attempt"])]
        return [it["item_id"], it["item_type"], it["source_table"], it["source_id"], it["episode_id"], it["attempt"],
                e["phase"], e["model"], e["cell"], e["seed"], e["world"], it["round"], it["slot"], it["handle"],
                it["kw_match"]]

    for it in items:
        j = judge.get(it["item_id"])
        if j is not None:
            l = lab(json.loads(j["labels_json"])) if j["status"] == "valid" else {}
            jrows.append(head(it) + [j["prompt_version"], j["status"], j["confidence"], j["uncertain"]]
                         + [l.get(c) for c in lcols])
        fl = labels.get((it["source_table"], it["source_id"]))
        if fl is not None:
            l = lab(dict(fl.labels))
            frows.append(head(it) + [fl.source, fl.uncertain] + [l.get(c) for c in lcols])
    nj = write_csv(out / "labels_judge.csv", base + ["prompt_version", "judge_status", "confidence", "uncertain"] + lcols, jrows)
    nf = write_csv(out / "labels_final.csv", base + ["label_source", "uncertain"] + lcols, frows)
    src = Counter(r[len(base)] for r in frows)
    return {"judge_rows": nj, "final_rows": nf, "final_by_source": dict(src), "items_in_scope": len(items),
            "label_columns": lcols}


# ---- transcripts -----------------------------------------------------------------------------------------------

def transcripts(db: Any, episodes: dict[tuple[int, int], dict], masker: ReleaseMasker, out: Path) -> dict[str, Any]:
    def rows(sql: str) -> dict[tuple[int, int], list[dict]]:
        d: dict[tuple[int, int], list[dict]] = {}
        for r in db.read(lambda c: c.execute(sql).fetchall()):
            d.setdefault((r["episode_id"], r["attempt"]), []).append(dict(r))
        return d

    posts = rows("SELECT episode_id,attempt,id,round,sender,slot,clock,kind,line_id,is_private,text,was_cut,was_collapsed,"
                 "visible_from_round,turn_id FROM posts ORDER BY id")
    notes = rows("SELECT episode_id,attempt,id,round,sender,slot,clock,subject,message,source,visible_from_round,turn_id "
                 "FROM notes ORDER BY id")
    logs = rows("SELECT episode_id,attempt,ref_no,sender,clock,subject_display,visible_from_round FROM v_log_numbered "
                "ORDER BY ref_no")
    turns = rows("SELECT episode_id,attempt,turn_id,turn_type,round,slot,handle,try_no,class FROM turns ORDER BY launched_at")
    calls = rows("SELECT episode_id,attempt,id,turn_id,round,slot,sender,tool,args_json,accepted,reject_reason FROM tool_calls ORDER BY id")
    uses = rows("SELECT t.episode_id,t.attempt,s.turn_id,s.seq,s.name,s.input_json,s.has_result,s.result_text,s.result_is_error,"
                "s.error_kind,s.db_row_ref FROM stream_tool_uses s JOIN turns t USING(turn_id) ORDER BY s.turn_id,s.seq")
    deb = rows("SELECT episode_id,attempt,slot,handle,question,answer_text,turn_id FROM debrief_answers ORDER BY slot,question")
    n_eps = 0
    path = out / "transcripts_masked.jsonl.gz"
    with open(path, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        for key in sorted(episodes):
            e = episodes[key]
            cell = e["cell"]
            m = lambda s: masker.mask(s, cell)  # noqa: E731
            tmap: dict[str, dict] = {}
            for t in turns.get(key, []):
                tmap[t["turn_id"]] = {"turn_id": t["turn_id"], "turn_type": t["turn_type"], "round": t["round"],
                                      "slot": t["slot"], "handle": t["handle"], "try_no": t["try_no"],
                                      "class": t["class"], "tool_calls": [], "tool_uses": [], "debrief": []}
            for c in calls.get(key, []):
                tmap[c["turn_id"]]["tool_calls"].append({"id": c["id"], "tool": c["tool"], "args": m(c["args_json"]),
                                                         "accepted": c["accepted"], "reject_reason": m(c["reject_reason"])})
            for u in uses.get(key, []):
                tmap[u["turn_id"]]["tool_uses"].append({
                    "seq": u["seq"], "name": u["name"], "input": m(u["input_json"]), "has_result": u["has_result"],
                    "result": m(u["result_text"]), "result_is_error": u["result_is_error"],
                    "error_kind": u["error_kind"], "tool_call_id": u["db_row_ref"]})
            for d in deb.get(key, []):
                tmap[d["turn_id"]]["debrief"].append({"slot": d["slot"], "handle": d["handle"], "question": d["question"],
                                                      "answer": m(d["answer_text"])})
            rec = {
                "episode_id": e["episode_id"], "attempt": e["attempt"], "phase": e["phase"], "model": e["model"],
                "cell": cell, "seed": e["seed"], "world": e["world"], "scenario": e["scenario"],
                "handles": json.loads(e["handles_json"]),
                "board_posts": [{**{k: v for k, v in p.items() if k not in ("episode_id", "attempt", "text")},
                                 "text": m(p["text"])} for p in posts.get(key, [])],
                "notes": [{**{k: v for k, v in p.items() if k not in ("episode_id", "attempt", "subject", "message")},
                           "subject": m(p["subject"]), "message": m(p["message"])} for p in notes.get(key, [])],
                "escalation_log": [{**{k: v for k, v in p.items() if k not in ("episode_id", "attempt", "subject_display")},
                                    "subject": m(p["subject_display"])} for p in logs.get(key, [])],
                "turns": list(tmap.values()),
            }
            gz.write((json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            n_eps += 1
    return {"episodes": n_eps}


# ---- driver ----------------------------------------------------------------------------------------------------

def build_data_release(settings: Any, out: Path) -> dict[str, Any]:
    """Write data/release/ tables into `out` (CSV + transcripts). Returns a summary. Scans every file afterwards."""
    from wtth.analysis.sets import snapshot_main_db
    from wtth.annotate.final import JudgeDbLabelSource
    from wtth.db import Db
    from wtth.judge.db import open_judge_db

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    snap = snapshot_main_db(settings)
    db = Db(snap.path)
    jdb = open_judge_db(settings)
    labels = JudgeDbLabelSource(jdb, strict=True)
    ot = outcome_tables(settings, db, labels, out)
    episodes: dict[tuple[int, int], dict] = {}
    for phase, aset in ot["sets"].items():
        ids = {(e.episode_id, e.attempt) for e in aset.episodes}
        for r in db.read(lambda c: c.execute("SELECT * FROM episodes").fetchall()):
            if (r["episode_id"], r["final_attempt"]) in ids:
                episodes[(r["episode_id"], r["final_attempt"])] = {
                    "episode_id": r["episode_id"], "attempt": r["final_attempt"], "phase": phase, "model": r["model"],
                    "cell": r["cell"], "seed": r["seed"], "world": r["world"], "scenario": r["scenario"],
                    "handles_json": r["handles_json"]}
    lt = label_tables(jdb, labels, episodes, out)
    masker = ReleaseMasker(settings)
    tt = transcripts(db, episodes, masker, out)
    # scan every exported file
    residual: dict[str, int] = {}
    frag = 0
    for f in sorted(out.iterdir()):
        if f.suffix not in (".csv", ".gz"):
            continue
        data = gzip.decompress(f.read_bytes()) if f.suffix == ".gz" else f.read_bytes()
        for pid, n in masker.residual(data).items():
            residual[pid] = residual.get(pid, 0) + n
        frag += masker.fragment_hits(data.decode("utf-8"))
    by_cell: dict[str, dict[str, int]] = {}
    for (cell, kind), n in masker.counts.items():
        by_cell.setdefault(cell, {})[kind] = n
    return {"snapshot_sha256": snap.sha256, "episodes": len(episodes), "outcomes": {k: v for k, v in ot.items()
            if k in ("witness_rows", "shift_rows")}, "labels": lt, "transcripts": tt, "residual": residual,
            "fragment_hits": frag, "masked_by_cell": by_cell, "_ot": ot, "_episodes": episodes}
