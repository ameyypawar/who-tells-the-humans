"""Author review report and its re-import (plan 47 section 2.10, F-06).

ONE Markdown file. Part A first: items where Claude and the judge disagree on a primary label, where either coder is
uncertain, or where no valid judge label exists. Part B: agreements. Private text is masked, the file passes the
residual scan. Labels are "proposed by a model (Claude) and reviewed and approved by the author; the author saw the
model label" and, on conflicts only, the judge value; they are never blind or independent hand labels.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wtth.annotate.agreement import with_escalation
from wtth.annotate.blind import blind_pending_items
from wtth.annotate.final import merge_corrections, parse_label_value
from wtth.annotate.masking import assert_clean_file, make_masker
from wtth.annotate.queues import PRIORITY
from wtth.judge.labels import LABELS, PRIMARY
from wtth.judge.types import ItemType

HEADER_RE = re.compile(r"^<!-- wtth-review v1 export_id=(\S+) items=(\d+) sha256=([0-9a-f]{64}) -->\s*$")
BLOCK_RE = re.compile(r"^### \[(a[0-9a-f]{10})\] (\S+) · (\S+)(?: · (.*))?$")
DECISION_RE = re.compile(r"^DECISION:(.*)$")
NOTE_RE = re.compile(r"^NOTE:(.*)$")
JUDGE_PREFIX = "Judge (shown on conflict):"
_QRANK = {q: i for i, q in enumerate(PRIORITY)}

INTRO = """# wtth author review

These labels were proposed by a model (Claude) from masked text, without seeing the judge's labels. You review each
one and approve or correct it. You see the Claude label and, only where the two models disagree on a primary label,
the judge's value. They are not blind or independent hand labels.

Part A comes first: conflicts, uncertain items and items with no valid judge label. Part B holds the agreements.

How to decide. Edit the DECISION line of each block (indented lines are text; only unindented DECISION and NOTE lines
count):

    DECISION: pending            ignored, nothing is recorded
    DECISION: approve            Claude's labels stand
    DECISION: use-judge          take the judge's labels (only where a Judge line is shown)
    DECISION: correct label=value [label=value ...]
                                 Claude's labels with these changes. Set labels take comma-separated values, an
                                 empty set is [], a null child label is none. escalation_to_humans is derived
                                 (addressed AND about): correct those two instead.
    NOTE: free text on one line  optional

Then run: python -I -m wtth.annotate import-review <this file>. Re-importing writes a new revision.
"""


class ReviewParseError(ValueError):
    def __init__(self, line_no: int, msg: str) -> None:
        super().__init__(f"line {line_no}: {msg}")
        self.line_no = line_no
        self.msg = msg


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    ann_id: str
    decision: str  # approve | correct | use_judge
    corrections: Mapping[str, Any]
    note: str | None
    line_no: int


# ---- rendering ---------------------------------------------------------------------------------------------

def fmt_value(v: Any) -> str:
    if v is None:
        return "none"
    if isinstance(v, list):
        return ",".join(v) if v else "[]"
    return str(v)


def fmt_labels(labels: Mapping[str, Any], names: Any = None) -> str:
    return " ".join(f"{k}={fmt_value(labels[k])}" for k in (names or labels))


def _indent(text: str) -> list[str]:
    return ["    " + ln for ln in text.split("\n")]


def _legend() -> list[str]:
    out = ["## Label legend", ""]
    for t, specs in LABELS.items():
        out.append(f"- {t.value} (primary: {', '.join(PRIMARY[t])})")
        for name, s in specs.items():
            vals = "0 or 1" if s.kind == "bin" else ", ".join(s.values) + (" (set)" if s.kind == "set" else "")
            out.append(f"    - {name}: {vals}" + (f"; none when {s.parent} = 0" if s.parent else ""))
    return out + [""]


def _load_rows(jdb: Any, include_reviewed: bool) -> list[dict[str, Any]]:
    sql = (
        "SELECT q.ann_id, q.queue, i.item_id, i.item_type, ei.masked_text, ei.masked_context, ei.batch_no, ei.position, "
        "a.revision AS ann_rev, a.labels_json AS ann_labels, a.quote, a.confidence, a.uncertain AS ann_unc, "
        "a.justification, j.status AS jstatus, j.labels_json AS jlabels, j.uncertain AS junc, "
        "r.revision AS review_rev FROM ann_queue q JOIN items i ON i.item_id = q.item_id "
        "JOIN v_ann_latest a ON a.ann_id = q.ann_id "
        "JOIN ann_export_items ei ON ei.ann_id = q.ann_id AND ei.export_id = "
        "(SELECT e.export_id FROM ann_export_items x JOIN ann_exports e ON e.export_id = x.export_id "
        " AND e.kind='annotator' WHERE x.ann_id = q.ann_id ORDER BY e.created_at DESC, e.export_id DESC LIMIT 1) "
        "LEFT JOIN v_judge_current j ON j.item_id = i.item_id LEFT JOIN v_review_latest r ON r.ann_id = q.ann_id "
        "WHERE q.queue != 'H-HO'")
    rows = jdb.read(lambda c: [dict(r) for r in c.execute(sql).fetchall()])
    if not include_reviewed:
        rows = [r for r in rows if r["review_rev"] is None]
    hidden = blind_pending_items(jdb)
    rows = [r for r in rows if r["item_id"] not in hidden]
    rows.sort(key=lambda r: (_QRANK[r["queue"]], r["batch_no"], r["position"], r["ann_id"]))
    return rows


def _conflicts(r: dict[str, Any]) -> dict[str, Any]:
    """label -> judge value, for primary labels where a valid judge label differs from Claude's."""
    if r["jstatus"] != "valid":
        return {}
    t = ItemType(r["item_type"])
    ann, jud = json.loads(r["ann_labels"]), json.loads(r["jlabels"])
    if t == ItemType.BOARD_POST:
        ann, jud = with_escalation(ann), with_escalation(jud)
    return {k: jud[k] for k in PRIMARY[t] if ann.get(k) != jud.get(k)}


def _tags(r: dict[str, Any], conflicts: Mapping[str, Any]) -> list[str]:
    tags = []
    if conflicts:
        tags.append("CONFLICT " + ",".join(conflicts))
    if r["ann_unc"] == 1 or (r["jstatus"] == "valid" and r["junc"] == 1):
        tags.append("UNCERTAIN")
    if r["jstatus"] != "valid":
        tags.append("NO-JUDGE " + ("(" + r["jstatus"] + ")" if r["jstatus"] else "(not judged)"))
    return tags


def report_path(out_path: Path, now: float | None = None) -> Path:
    p = Path(out_path)
    return p if p.suffix == ".md" else p / ("wtth-review-" + time.strftime("%Y%m%d-%H%M", time.localtime(now)) + ".md")


def write_review_report(settings: Any, jdb: Any, out_path: Path, *, include_reviewed: bool = False,
                        seed: int) -> Mapping[str, int]:
    """Write the review file (out_path is a file ending in .md, or a directory that gets wtth-review-<stamp>.md).
    Returns counts: items, part_a, part_b, conflicts, uncertain, no_judge, hidden_blind."""
    masker = make_masker(settings)
    rows = _load_rows(jdb, include_reviewed)
    if not rows:
        raise ValueError("nothing to review: no labelled, unreviewed, unblinded items")
    hidden = len(blind_pending_items(jdb))
    prepared = []
    for r in rows:
        c = _conflicts(r)
        prepared.append((r, c, _tags(r, c)))
    part_a = [x for x in prepared if x[2]]
    part_b = [x for x in prepared if not x[2]]
    ids = sorted(r["ann_id"] for r, _, _ in prepared)
    id_hash = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    export_id = "rv" + time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + secrets.token_hex(2)
    path = report_path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def block(r: dict[str, Any], conflicts: Mapping[str, Any], tags: list[str]) -> list[str]:
        t = ItemType(r["item_type"])
        ann = json.loads(r["ann_labels"])
        head = f"### [{r['ann_id']}] {t.value} · {r['queue']}" + (" · " + " · ".join(tags) if tags else " · AGREE")
        quote, _ = masker.mask(json.dumps(r["quote"], ensure_ascii=False))
        why, _ = masker.mask(r["justification"])
        out = [head, "Board (masked):", *_indent(r["masked_context"]), "Item:", *_indent(r["masked_text"]),
               f"Claude: {fmt_labels(ann, list(LABELS[t]))} · uncertain={r['ann_unc']} · confidence={r['confidence']}",
               f"Claude quote: {quote}", f"Claude why: {why}"]
        if conflicts:
            out.append(f"{JUDGE_PREFIX} {fmt_labels(conflicts)}")
        out += ["DECISION: pending", "NOTE:", ""]
        return out

    lines = [f"<!-- wtth-review v1 export_id={export_id} items={len(prepared)} sha256={id_hash} -->", "", INTRO,
             *_legend(), f"## Part A: conflicts, uncertain and no-judge items ({len(part_a)})", ""]
    for r, c, tg in part_a:
        lines += block(r, c, tg)
    lines += [f"## Part B: agreements ({len(part_b)})", ""]
    for r, c, tg in part_b:
        lines += block(r, c, tg)
    path.write_text("\n".join(lines), encoding="utf-8")
    assert_clean_file(path, masker.private)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    detail = {"ann_ids": ids, "revisions": {r["ann_id"]: r["ann_rev"] for r, _, _ in prepared},
              "judge_shown": {r["ann_id"]: int(bool(c)) for r, c, _ in prepared},
              "item_types": {r["ann_id"]: r["item_type"] for r, _, _ in prepared}, "include_reviewed": include_reviewed}
    jdb.write(lambda cn: cn.execute(
        "INSERT INTO ann_exports(export_id, kind, path, sha256, n_items, seed, created_at, detail_json) "
        "VALUES (?,?,?,?,?,?,?,?)", (export_id, "review", str(path), sha, len(prepared), int(seed), time.time(),
                                     json.dumps(detail))))
    return {"items": len(prepared), "part_a": len(part_a), "part_b": len(part_b),
            "conflicts": sum(1 for _, c, _ in prepared if c),
            "uncertain": sum(1 for _, _, tg in prepared if "UNCERTAIN" in tg),
            "no_judge": sum(1 for r, _, _ in prepared if r["jstatus"] != "valid"), "hidden_blind": hidden}


# ---- parsing -----------------------------------------------------------------------------------------------

def _parse_correct(item_type: str, rest: str, line_no: int) -> dict[str, Any]:
    toks = rest.split()
    if not toks:
        raise ReviewParseError(line_no, "correct needs at least one label=value")
    out: dict[str, Any] = {}
    for tok in toks:
        name, sep, val = tok.partition("=")
        if not sep or not val:
            raise ReviewParseError(line_no, f"expected label=value, got {tok!r}")
        if name in out:
            raise ReviewParseError(line_no, f"{name}: given twice")
        try:
            out[name] = parse_label_value(item_type, name, val)
        except ValueError as e:
            raise ReviewParseError(line_no, str(e)) from None
    return out


def _parse(text: str) -> list[dict[str, Any]]:
    """Every block, pending ones included: ann_id, type, line_no, judge_shown, decision, corrections, note."""
    blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur: dict[str, Any] | None = None

    def close() -> None:
        if cur is not None and cur["decision"] is None:
            raise ReviewParseError(cur["line_no"], "block has no DECISION line")

    for n, ln in enumerate(text.splitlines(), 1):
        if ln.startswith("### ["):
            m = BLOCK_RE.match(ln)
            if not m:
                raise ReviewParseError(n, "malformed block header")
            close()
            ann_id, typ = m.group(1), m.group(2)
            if typ not in {t.value for t in ItemType}:
                raise ReviewParseError(n, f"unknown item type {typ!r}")
            if ann_id in seen:
                raise ReviewParseError(n, f"duplicate block for {ann_id}")
            seen.add(ann_id)
            cur = {"ann_id": ann_id, "type": typ, "line_no": n, "judge_shown": False, "decision": None,
                   "corrections": {}, "note": None, "note_seen": False}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        if ln.startswith(JUDGE_PREFIX):
            cur["judge_shown"] = True
            continue
        m = DECISION_RE.match(ln)
        if m:
            if cur["decision"] is not None:
                raise ReviewParseError(n, "doubled DECISION line")
            word, _, rest = m.group(1).strip().partition(" ")
            word = word.lower()
            if word == "pending" and not rest.strip():
                cur["decision"] = "pending"
            elif word == "approve" and not rest.strip():
                cur["decision"] = "approve"
            elif word == "use-judge" and not rest.strip():
                cur["decision"] = "use_judge"
            elif word == "correct":
                cur["decision"] = "correct"
                cur["corrections"] = _parse_correct(cur["type"], rest, n)
            else:
                raise ReviewParseError(n, f"unknown decision {m.group(1).strip()!r}")
            cur["decision_line"] = n
            continue
        m = NOTE_RE.match(ln)
        if m:
            if cur["note_seen"]:
                raise ReviewParseError(n, "doubled NOTE line")
            cur["note_seen"] = True
            cur["note"] = m.group(1).strip() or None
    close()
    for b in blocks:
        if b["decision"] == "use_judge" and not b["judge_shown"]:
            raise ReviewParseError(b["decision_line"], "use-judge is only valid where a Judge line is shown")
    return blocks


def parse_review_file(text: str) -> list[ReviewDecision]:
    """Pure. The non-pending decisions, in file order. Raises ReviewParseError(line_no, msg) on a malformed block,
    duplicate block, missing or doubled DECISION, unknown label, invalid value or use-judge without a Judge line."""
    return [ReviewDecision(b["ann_id"], b["decision"], b["corrections"], b["note"], b["decision_line"])
            for b in _parse(text) if b["decision"] != "pending"]


def import_review(settings: Any, jdb: Any, path: Path) -> Mapping[str, Any]:
    """Atomic import of the decisions in a report file: reviews revision n+1 per decided block."""
    data = Path(path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    text = data.decode("utf-8")
    hm = next((HEADER_RE.match(ln) for ln in text.splitlines() if ln.startswith("<!-- wtth-review")), None)
    if hm is None:
        raise ReviewParseError(1, "missing or malformed wtth-review header")
    export_id = hm.group(1)
    row = jdb.read(lambda c: c.execute("SELECT detail_json FROM ann_exports WHERE export_id=? AND kind='review'",
                                       (export_id,)).fetchone())
    if row is None:
        raise ReviewParseError(1, "this report was not written by this database (unknown export id)")
    detail = json.loads(row[0])
    blocks = _parse(text)
    known = set(detail["ann_ids"])
    ready: list[dict[str, Any]] = []
    for b in blocks:
        if b["ann_id"] not in known:
            raise ReviewParseError(b["line_no"], f"unknown ann_id {b['ann_id']}")
        if detail["item_types"][b["ann_id"]] != b["type"]:
            raise ReviewParseError(b["line_no"], "item type differs from the report")
        if b["decision"] == "pending":
            continue
        if b["decision"] == "use_judge" and not detail["judge_shown"][b["ann_id"]]:
            raise ReviewParseError(b["decision_line"], "no judge label was shown for this item")
        if b["decision"] == "correct":
            rev = detail["revisions"][b["ann_id"]]
            base = jdb.read(lambda c: c.execute("SELECT labels_json FROM ann_labels WHERE ann_id=? AND revision=?",
                                                (b["ann_id"], rev)).fetchone())
            try:
                merge_corrections(b["type"], json.loads(base[0]), b["corrections"])
            except ValueError as e:
                raise ReviewParseError(b["decision_line"], str(e)) from None
        ready.append(b)
    now = time.time()
    counts = {"approve": 0, "correct": 0, "use_judge": 0}

    def fn(c: Any) -> None:
        for b in ready:
            rev = c.execute("SELECT COALESCE(MAX(revision),0) FROM reviews WHERE ann_id=?", (b["ann_id"],)).fetchone()[0] + 1
            c.execute("INSERT INTO reviews(ann_id, revision, decision, corrections_json, note, based_on_ann_revision, "
                      "judge_shown, report_export_id, source_sha256, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (b["ann_id"], rev, b["decision"],
                       json.dumps(b["corrections"], sort_keys=True, ensure_ascii=False) if b["decision"] == "correct" else None,
                       b["note"], detail["revisions"][b["ann_id"]], detail["judge_shown"][b["ann_id"]], export_id, sha, now))
            counts[b["decision"]] += 1
    jdb.write(fn)
    return {"imported": len(ready), **counts, "pending": sum(1 for b in blocks if b["decision"] == "pending"),
            "missing_blocks": len(known) - len(blocks), "export_id": export_id}
