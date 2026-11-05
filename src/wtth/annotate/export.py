"""Annotator batch export (plan 47 section 2.9, F-05): masked JSONL batches plus INSTRUCTIONS.md, blind to judge
labels, cell, seed, model, phase, item id and episode id."""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from wtth.annotate.masking import Masker, assert_clean_file, make_masker
from wtth.judge.labels import LABELS
from wtth.judge.types import ItemType

BATCH_SIZE = 50

FORMAT_SECTION = """## How to work through a batch

You are given one batch file, batch_NNN.jsonl. Each line is one item:
{"ann_id": "...", "type": "note|board_post|d1_answer|d2_answer", "labels_wanted": [...], "board": "...", "text": "..."}

- "board" is the board so far (for debrief answers, the whole board). "text" is the item. Code only the item.
- Some text has been replaced by bracketed placeholders such as [withheld text]. Code what you can see.
- Do not look at any other file in this directory and do not try to find out where an item came from.

Write one JSON object per item to labels/labels_NNN.jsonl (same NNN), one object per line, in any order, with no code
fences and no other text:
{"ann_id": "...", "labels": {...}, "quote": "...", "confidence": "high|medium|low", "uncertain": 0, "justification": "..."}

- ann_id: copied from the item. Use the key ann_id, not id; this overrides the Format line above.
- labels: exactly the names listed in labels_wanted, with the values defined above.
- quote: copied exactly from the item's "text", at most 300 characters.
- uncertain: 0 or 1. justification: one line.
"""


def blind_prompt(system_text: str) -> str:
    """The active system prompt with the one sentence that names the labeller reworded. Raises if the word survives."""
    out = system_text.replace("The judge returns nothing else.", "Return nothing else.").rstrip()
    if re.search(r"\bjudge\b", out, re.IGNORECASE):
        raise ValueError("system prompt still names the judge after blinding")
    return out


def instructions_text(system_text: str) -> str:
    return blind_prompt(system_text) + "\n\n" + FORMAT_SECTION


def _active_system(jdb: Any) -> tuple[str, str]:
    row = jdb.read(lambda c: c.execute(
        "SELECT p.prompt_version, p.system_path FROM prompt_versions p JOIN jmeta m ON m.key='active_prompt_version' "
        "AND m.value = p.prompt_version").fetchone())
    if row is None:
        raise ValueError("no active judge prompt version recorded")
    return row[0], Path(row[1]).read_text(encoding="utf-8")


def _sha_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode() + b":" + hashlib.sha256(p.read_bytes()).hexdigest().encode() + b"\n")
    return h.hexdigest()


def batch_record(ann_id: str, typ: str, board: str, text: str) -> dict[str, Any]:
    return {"ann_id": ann_id, "type": typ, "labels_wanted": list(LABELS[ItemType(typ)]), "board": board, "text": text}


def export_annotator_batches(settings: Any, jdb: Any, out_dir: Path, *, batch_size: int = BATCH_SIZE, seed: int,
                             include_exported_unlabeled: bool = False, masker: Masker | None = None) -> Path:
    """Write <out_dir>/<export_id>/{INSTRUCTIONS.md, batch_NNN.jsonl, labels/} for queued items that have no export
    yet (or, with include_exported_unlabeled, no label yet). Order: seeded shuffle across queues and cells.
    Every file passes the residual scan or the export directory is removed. Returns the export directory."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    version, system_text = _active_system(jdb)
    instructions = instructions_text(system_text)
    if include_exported_unlabeled:
        where = "NOT EXISTS (SELECT 1 FROM ann_labels l WHERE l.ann_id = q.ann_id)"
    else:
        where = "NOT EXISTS (SELECT 1 FROM ann_export_items e WHERE e.ann_id = q.ann_id)"
    rows = jdb.read(lambda c: [dict(r) for r in c.execute(
        "SELECT q.ann_id, i.item_type, i.text, i.context_text FROM ann_queue q JOIN items i ON i.item_id = q.item_id "
        f"WHERE {where} ORDER BY q.ann_id").fetchall()])
    if not rows:
        raise ValueError("nothing to export: every queued item has already been exported")
    random.Random(int(seed)).shuffle(rows)
    masker = masker or make_masker(settings)
    export_id = "ex" + time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + secrets.token_hex(2)
    exp_dir = Path(out_dir) / export_id
    exp_dir.mkdir(parents=True, exist_ok=False)
    (exp_dir / "labels").mkdir()
    items: list[tuple[str, int, int, str, str, int]] = []
    files: list[Path] = []
    try:
        (exp_dir / "INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")
        files.append(exp_dir / "INSTRUCTIONS.md")
        n_batches = (len(rows) + batch_size - 1) // batch_size
        for b in range(n_batches):
            chunk = rows[b * batch_size:(b + 1) * batch_size]
            lines = []
            for pos, r in enumerate(chunk, 1):
                mt, n1 = masker.mask(r["text"])
                mc, n2 = masker.mask(r["context_text"])
                items.append((r["ann_id"], b + 1, pos, mt, mc, n1 + n2))
                lines.append(json.dumps(batch_record(r["ann_id"], r["item_type"], mc, mt), ensure_ascii=False))
            path = exp_dir / f"batch_{b + 1:03d}.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            files.append(path)
        for p in files:
            assert_clean_file(p, masker.private)
    except BaseException:
        shutil.rmtree(exp_dir, ignore_errors=True)
        raise
    detail = {"batch_size": batch_size, "n_batches": n_batches, "prompt_version": version,
              "files": [p.name for p in files], "include_exported_unlabeled": include_exported_unlabeled}

    def fn(conn: Any) -> None:
        conn.execute("INSERT INTO ann_exports(export_id, kind, path, sha256, n_items, seed, created_at, detail_json) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (export_id, "annotator", str(exp_dir), _sha_files(files), len(rows), int(seed), time.time(),
                      json.dumps(detail)))
        for ann_id, batch_no, pos, mt, mc, n in items:
            conn.execute("INSERT INTO ann_export_items(export_id, ann_id, batch_no, position, masked_text, "
                         "masked_context, n_masked) VALUES (?,?,?,?,?,?,?)", (export_id, ann_id, batch_no, pos, mt, mc, n))

    jdb.write(fn)
    return exp_dir
