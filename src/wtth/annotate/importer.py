"""Import Claude label files (plan 47 section 2.9): strict per-line validation against the MASKED exported text,
revisions increment, invalid lines are reported with line numbers and written to redo_NNN.jsonl."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from wtth.annotate.export import batch_record
from wtth.annotate.masking import assert_clean_file, make_masker
from wtth.judge.labels import parse_output
from wtth.judge.types import ItemType

ANN_KEYS = ("ann_id", "labels", "quote", "confidence", "uncertain", "justification")


def _export_item(jdb: Any, ann_id: str) -> dict[str, Any] | None:
    r = jdb.read(lambda c: c.execute(
        "SELECT e.export_id, ei.masked_text, ei.masked_context, ei.batch_no, i.item_type FROM ann_export_items ei "
        "JOIN ann_exports e ON e.export_id = ei.export_id AND e.kind = 'annotator' "
        "JOIN ann_queue q ON q.ann_id = ei.ann_id JOIN items i ON i.item_id = q.item_id "
        "WHERE ei.ann_id = ? ORDER BY e.created_at DESC, e.export_id DESC LIMIT 1", (ann_id,)).fetchone())
    return dict(r) if r else None


def check_line(jdb: Any, line: str) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    """(ann_id, record, errors). record has canonical labels, quote, confidence, uncertain, justification."""
    if line.lstrip().startswith("```"):
        return None, None, ["json:fence_not_allowed"]
    try:
        obj = json.loads(line)
    except ValueError:
        return None, None, ["json:not_one_object"]
    if not isinstance(obj, dict):
        return None, None, ["json:not_one_object"]
    ann_id = obj.get("ann_id")
    if not isinstance(ann_id, str):
        return None, None, ["top:missing:ann_id"]
    ex = _export_item(jdb, ann_id)
    if ex is None:
        return ann_id, None, ["ann_id:unknown_or_not_exported"]
    if "id" in obj:
        return ann_id, None, ["top:unexpected:id"]
    o2 = {k: v for k, v in obj.items() if k != "ann_id"}
    o2["id"] = ann_id
    po = parse_output(json.dumps(o2), item_id=ann_id, item_type=ItemType(ex["item_type"]), item_text=ex["masked_text"],
                      allow_fence=False)
    if not po.ok:
        return ann_id, None, list(po.errors)
    return ann_id, {"labels": dict(po.labels), "quote": po.quote, "confidence": po.confidence,
                    "uncertain": po.uncertain, "justification": po.justification, "export": ex}, []


def import_label_files(settings: Any, jdb: Any, paths: Sequence[Path], *, annotator: str) -> Mapping[str, Any]:
    """Validate and import label files. Valid lines become revision n+1 (duplicates of an ann_id inside one call
    are rejected). Invalid or missing lines of a labels_NNN.jsonl are written, in export-batch form, to
    <export dir>/redo_NNN.jsonl. Returns {imported, invalid: [{file, line, ann_id, errors}], redo: [paths], files}."""
    masker = make_masker(settings)
    imported = 0
    invalid: list[dict[str, Any]] = []
    redo: list[str] = []
    todo: list[tuple[str, str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for p in map(Path, paths):
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        bad_ids: list[str] = []
        ok_ids: set[str] = set()
        for n, raw in enumerate(data.decode("utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            ann_id, rec, errs = check_line(jdb, raw)
            if not errs and ann_id in seen:
                errs = ["ann_id:duplicate_in_import"]
            if errs:
                invalid.append({"file": str(p), "line": n, "ann_id": ann_id, "errors": errs})
                if ann_id and _export_item(jdb, ann_id):
                    bad_ids.append(ann_id)
                continue
            seen.add(ann_id)
            ok_ids.add(ann_id)
            todo.append((ann_id, str(p), sha, rec))
        m = re.fullmatch(r"labels_(\d+)\.jsonl", p.name)
        if m:
            exp_dir = p.parent.parent if p.parent.name == "labels" else p.parent
            batch = exp_dir / f"batch_{m.group(1)}.jsonl"
            if batch.is_file():
                wanted = [json.loads(x) for x in batch.read_text(encoding="utf-8").splitlines() if x.strip()]
                missing = [w["ann_id"] for w in wanted if w["ann_id"] not in ok_ids]
                if missing:
                    ex_by = {w["ann_id"]: w for w in wanted}
                    rp = exp_dir / f"redo_{m.group(1)}.jsonl"
                    rp.write_text("\n".join(json.dumps(batch_record(a, ex_by[a]["type"], ex_by[a]["board"],
                                                                      ex_by[a]["text"]), ensure_ascii=False)
                                            for a in missing) + "\n", encoding="utf-8")
                    assert_clean_file(rp, masker.private)
                    redo.append(str(rp))
    now = time.time()

    def fn(conn: Any) -> int:
        n = 0
        for ann_id, src, sha, rec in todo:
            rev = (conn.execute("SELECT COALESCE(MAX(revision),0) FROM ann_labels WHERE ann_id=?", (ann_id,))
                   .fetchone()[0]) + 1
            conn.execute("INSERT INTO ann_labels(ann_id, revision, annotator, export_id, labels_json, quote, confidence, "
                         "uncertain, justification, source_file, source_sha256, imported_at) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ann_id, rev, annotator, rec["export"]["export_id"],
                          json.dumps(rec["labels"], sort_keys=True, ensure_ascii=False), rec["quote"],
                          rec["confidence"], rec["uncertain"], rec["justification"], src, sha, now))
            n += 1
        return n

    imported = jdb.write(fn)
    return {"imported": imported, "invalid": invalid, "redo": redo, "files": [str(p) for p in paths]}
