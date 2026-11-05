"""Optional author-blind sample (plan 47 section 2.10, F-07): about 100 items labelled by the author without seeing
any label. Export, import, decline. Must be done before the author opens any review report."""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import time
from pathlib import Path
from typing import Any

from wtth.annotate.final import parse_label_value
from wtth.annotate.masking import assert_clean_file, make_masker
from wtth.judge.labels import LABELS, validate_labels
from wtth.judge.types import ItemType

STRATA = (("board_post", 40), ("note", 30), ("d1_answer", 15), ("d2_answer", 15))
HEADER = "<!-- wtth-blind v1 n={n} -->"
BLOCK = re.compile(r"^### \[(b[0-9a-f]{10})\] (\S+)\s*$")
UP = "INSERT INTO jmeta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"


class BlindParseError(ValueError):
    def __init__(self, line_no: int, msg: str) -> None:
        super().__init__(f"line {line_no}: {msg}")
        self.line_no = line_no


def quotas(n: int, available: dict[str, int]) -> dict[str, int]:
    """Strata quotas scaled to n; a short stratum's remainder is re-allocated to the others in proportion."""
    weights = {t: w for t, w in STRATA}
    out: dict[str, float] = {t: 0.0 for t in weights}
    capped: set[str] = set()
    remaining = float(n)
    while remaining > 1e-9:
        free = [t for t in weights if t not in capped]
        if not free:
            break
        fw = sum(weights[t] for t in free)
        share = {t: remaining * weights[t] / fw for t in free}
        newly = [t for t in free if out[t] + share[t] >= available.get(t, 0)]
        if not newly:
            for t in free:
                out[t] += share[t]
            remaining = 0.0
            break
        for t in newly:
            remaining -= available.get(t, 0) - out[t]
            out[t] = float(available.get(t, 0))
            capped.add(t)
    # largest-remainder rounding of the float quotas, never above availability, total min(n, sum available)
    target = min(n, sum(available.get(t, 0) for t in weights))
    floor = {t: min(int(out[t]), available.get(t, 0)) for t in weights}
    rest = target - sum(floor.values())
    order = sorted(weights, key=lambda t: (-(out[t] - int(out[t])), t))
    while rest > 0:
        moved = False
        for t in order:
            if rest > 0 and floor[t] < available.get(t, 0):
                floor[t] += 1
                rest -= 1
                moved = True
        if not moved:
            break
    return floor


def _file_path(out_path: Path, stamp: str) -> Path:
    p = Path(out_path)
    return p if p.suffix == ".md" else p / f"wtth-blind-{stamp}.md"


def _draw(jdb: Any, n: int, seed: int) -> list[tuple[str, str, int]]:
    rows = jdb.read(lambda c: [dict(r) for r in c.execute(
        "SELECT i.item_id, i.item_type FROM items i JOIN v_judge_current j ON j.item_id = i.item_id AND j.status='valid' "
        "WHERE i.in_analysis_set = 1 AND i.tier IN ('J1','J2') AND lower(i.model) LIKE '%haiku%' "
        "ORDER BY i.order_key, i.item_id").fetchall()])
    pop: dict[str, list[str]] = {t: [] for t, _ in STRATA}
    for r in rows:
        pop[r["item_type"]].append(r["item_id"])
    q = quotas(n, {t: len(v) for t, v in pop.items()})
    drawn: list[tuple[str, str, int]] = []
    for t, _ in STRATA:
        order = list(pop[t])
        h = hashlib.sha256(f"{seed}:{t}".encode()).digest()
        random.Random(int.from_bytes(h[:8], "big")).shuffle(order)
        for rank, iid in enumerate(order[:q[t]], 1):
            drawn.append((iid, t, rank))
    return drawn


def _legend_value(s: Any) -> str:
    if s.kind == "bin":
        return "0 or 1"
    if s.kind == "enum":
        return "one of " + ", ".join(s.values) + (f" (none when {s.parent} is 0)" if s.parent else "")
    return "comma-separated subset of " + ", ".join(s.values) + ", or []"


def _legend(item_type: str) -> list[str]:
    return [f"- {name}: {_legend_value(s)}" for name, s in LABELS[ItemType(item_type)].items()
            if name != "escalation_to_humans"]


def export_blind_sample(settings: Any, jdb: Any, out_path: Path, *, n: int = 100, seed: int) -> Path:
    """Draw (once) and write the blind-sample file. A later call rewrites the file for the same sample."""
    existing = jdb.read(lambda c: [dict(r) for r in c.execute(
        "SELECT s.blind_id, s.item_id, s.stratum, s.draw_rank FROM blind_sample s ORDER BY s.blind_id").fetchall()])
    if not existing:
        drawn = _draw(jdb, n, seed)
        if not drawn:
            raise ValueError("no valid judged Haiku analysis-set items to draw from")
        now = time.time()
        sample = []
        taken: set[str] = set()
        for iid, stratum, rank in drawn:
            while True:
                bid = "b" + secrets.token_hex(5)
                if bid not in taken:
                    taken.add(bid)
                    break
            sample.append({"blind_id": bid, "item_id": iid, "stratum": stratum, "draw_rank": rank})

        def fn(c: Any) -> None:
            for s in sample:
                c.execute("INSERT INTO blind_sample(blind_id, item_id, stratum, draw_seed, draw_rank, created_at) "
                          "VALUES (?,?,?,?,?,?)", (s["blind_id"], s["item_id"], s["stratum"], seed, s["draw_rank"], now))
        jdb.write(fn)
        existing = sample
    rows = jdb.read(lambda c: {r["item_id"]: dict(r) for r in c.execute(
        "SELECT i.item_id, i.item_type, i.text, i.context_text, i.order_key FROM items i").fetchall()})
    masker = make_masker(settings)
    order = sorted(existing, key=lambda s: s["blind_id"])
    random.Random(int(seed)).shuffle(order)
    stamp = time.strftime("%Y%m%d-%H%M")
    path = _file_path(out_path, stamp)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [HEADER.format(n=len(order)), "", "# wtth blind sample",
           "Label each item below from the text alone. You are not shown any other label. Replace every `?` on the",
           "LABELS line with a value, then run: python -I -m wtth.annotate blind-import <this file>",
           "Do this before you open any review report.", "", "Values:"]
    for t, _ in STRATA:
        out.append(f"  {t}:")
        out.extend("  " + x for x in _legend(t))
    out.append("")
    for s in order:
        it = rows[s["item_id"]]
        text, _ = masker.mask(it["text"])
        ctx, _ = masker.mask(it["context_text"])
        out.append(f"### [{s['blind_id']}] {it['item_type']}")
        out.append("Board (masked):")
        out.extend("    " + ln for ln in ctx.split("\n"))
        out.append("Item:")
        out.extend("    " + ln for ln in text.split("\n"))
        out.append("LABELS: " + " ".join(f"{k}=?" for k in LABELS[ItemType(it["item_type"])] if k != "escalation_to_humans"))
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")
    assert_clean_file(path, masker.private)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    jdb.write(lambda c: c.execute(
        "INSERT INTO ann_exports(export_id, kind, path, sha256, n_items, seed, created_at, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        ("bl" + time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + secrets.token_hex(2), "blind", str(path), sha,
         len(order), seed, time.time(), json.dumps({"blind_ids": [s["blind_id"] for s in order]}))))
    return path


def parse_blind_file(text: str, known: dict[str, str]) -> dict[str, dict[str, Any]]:
    """blind_id -> raw label dict (values parsed). known maps blind_id -> item_type. Raises BlindParseError."""
    out: dict[str, dict[str, Any]] = {}
    cur: str | None = None
    cur_type = ""
    answered: set[str] = set()
    for n, ln in enumerate(text.splitlines(), 1):
        m = BLOCK.match(ln)
        if ln.startswith("### ["):
            if not m:
                raise BlindParseError(n, "malformed block header")
            cur, cur_type = m.group(1), m.group(2)
            if cur not in known:
                raise BlindParseError(n, "unknown blind id")
            if known[cur] != cur_type:
                raise BlindParseError(n, "item type does not match the sample")
            if cur in answered:
                raise BlindParseError(n, "duplicate block")
            answered.add(cur)
            continue
        if ln.startswith("LABELS:"):
            if cur is None:
                raise BlindParseError(n, "LABELS line outside a block")
            if cur in out:
                raise BlindParseError(n, "doubled LABELS line")
            labels: dict[str, Any] = {}
            for tok in ln[len("LABELS:"):].split():
                name, sep, val = tok.partition("=")
                if not sep:
                    raise BlindParseError(n, f"expected label=value, got {tok!r}")
                if val == "?" or val == "":
                    raise BlindParseError(n, f"{name}: still unanswered (?)")
                if name in labels:
                    raise BlindParseError(n, f"{name}: given twice")
                try:
                    labels[name] = parse_label_value(cur_type, name, val)
                except ValueError as e:
                    raise BlindParseError(n, str(e)) from None
            want = [k for k in LABELS[ItemType(cur_type)] if k != "escalation_to_humans"]
            missing = [k for k in want if k not in labels]
            if missing:
                raise BlindParseError(n, "missing labels: " + ", ".join(missing))
            extra = [k for k in labels if k not in want]
            if extra:
                raise BlindParseError(n, "unexpected labels: " + ", ".join(extra))
            if cur_type == "board_post":
                labels["escalation_to_humans"] = int(labels["addressed_to_humans"] == 1 and labels["about_violation"] == 1)
            canon, errs = validate_labels(ItemType(cur_type), labels)
            if canon is None:
                raise BlindParseError(n, "invalid labels: " + ", ".join(errs))
            out[cur] = canon
    return out


def import_blind_labels(settings: Any, jdb: Any, path: Path) -> Any:
    """Import the author's blind labels (atomic: any unanswered `?` or invalid value rejects the whole file).
    Returns {imported, complete, missing}."""
    data = Path(path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    known = dict(jdb.read(lambda c: [(r[0], r[1]) for r in c.execute(
        "SELECT s.blind_id, i.item_type FROM blind_sample s JOIN items i ON i.item_id = s.item_id").fetchall()]))
    if not known:
        raise BlindParseError(1, "no blind sample has been exported")
    parsed = parse_blind_file(data.decode("utf-8"), known)
    now = time.time()

    def fn(c: Any) -> None:
        for bid, labels in parsed.items():
            rev = c.execute("SELECT COALESCE(MAX(revision),0) FROM blind_labels WHERE blind_id=?", (bid,)).fetchone()[0] + 1
            c.execute("INSERT INTO blind_labels(blind_id, revision, labels_json, source_sha256, imported_at) VALUES (?,?,?,?,?)",
                      (bid, rev, json.dumps(labels, sort_keys=True, ensure_ascii=False), sha, now))
    jdb.write(fn)
    labelled = jdb.read(lambda c: c.execute("SELECT COUNT(DISTINCT blind_id) FROM blind_labels").fetchone()[0])
    return {"imported": len(parsed), "complete": labelled == len(known), "missing": len(known) - labelled}


def decline_blind(jdb: Any) -> None:
    jdb.write(lambda c: c.execute(UP, ("blind_declined", "1")))


def blind_pending_items(jdb: Any) -> set[str]:
    """Item ids hidden from review reports: the whole blind sample until every item has blind labels or the author
    declined the sample."""
    rows = jdb.read(lambda c: c.execute(
        "SELECT s.item_id, EXISTS(SELECT 1 FROM blind_labels b WHERE b.blind_id = s.blind_id) FROM blind_sample s").fetchall())
    if not rows:
        return set()
    declined = jdb.read(lambda c: c.execute("SELECT value FROM jmeta WHERE key='blind_declined'").fetchone())
    if declined and declined[0] == "1":
        return set()
    if all(r[1] for r in rows):
        return set()
    return {r[0] for r in rows}
