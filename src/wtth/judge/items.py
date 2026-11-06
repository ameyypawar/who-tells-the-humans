"""Item extraction, tiers, sub-tiers, opaque ids and seeded cell-balanced order (plan 47 section 2.3)."""
from __future__ import annotations

import hashlib
import json
import random
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from wtth.judge import prompt as prompt_mod
from wtth.judge.db import active_version, ensure_meta, get_meta, log_event, set_meta
from wtth.judge.types import ExtractionError, Item, ItemType
from wtth.textnorm import sha256_text
from wtth.types import PostRow

SUBTIERS: tuple[str, ...] = ("HO", "G1", "J1a", "J1b", "J1c", "J1d", "J1e", "J2a", "J2b", "J2c", "J2d", "J2e",
                             "J2f", "J2g", "J2h", "J2i")
P2_CELLS: tuple[str, ...] = ("NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "NO-RECEIPT", "FORGED", "NEG")
_J2_GROUP = {"NONE": "J2a", "CLAIM": "J2a", "NONE-R": "J2b", "NO-RECEIPT": "J2b", "FORGED": "J2b"}
FIRST_BATCH_REAL = 38
ROUNDS = (1, 5)


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    extraction_id: str
    n_new: int
    by_subtier: Mapping[str, int]
    n_empty_answers: int


@dataclass(slots=True)
class _Cand:
    item_type: ItemType
    key: tuple[str, str]
    episode_id: int | None
    attempt: int | None
    phase: str | None
    scenario: str | None
    model: str | None
    cell: str | None
    level: str | None
    seed: int | None
    world: str | None
    round: int | None
    slot: int | None
    handle: str | None
    in_analysis_set: bool
    kw_match: bool | None
    text: str
    context_text: str
    tier: str = ""
    subtier: str = ""
    reasons: list[str] = field(default_factory=list)
    unit: tuple = ()
    sort: tuple = ()


# ---- ordering ---------------------------------------------------------------------------------------------------

def subtier_seed(judge_order_seed: int, subtier: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{judge_order_seed}:{subtier}".encode()).digest()[:8], "big")


def assign_order(units: Sequence[tuple[str, Sequence[Any]]], *, seed: int, cell_order: Sequence[str]) -> list[Any]:
    """Units are (cell, items). Grouped by cell in `cell_order`, each group shuffled with Random(seed), then
    interleaved round-robin across cells. Items inside a unit keep their order. Any prefix of the result is a
    cell-balanced random subsample of units."""
    rng = random.Random(seed)
    groups: dict[str, list[Sequence[Any]]] = defaultdict(list)
    for cell, items in units:
        groups[cell].append(items)
    order = [c for c in cell_order if c in groups] + sorted(c for c in groups if c not in cell_order)
    for c in order:
        rng.shuffle(groups[c])
    out: list[Any] = []
    depth = max((len(g) for g in groups.values()), default=0)
    for i in range(depth):
        for c in order:
            if i < len(groups[c]):
                out.extend(groups[c][i])
    return out


# ---- reading the main database --------------------------------------------------------------------------------

def _post_rows(conn: sqlite3.Connection, ep: int, att: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, round, sender, slot, clock, clock_min, kind, line_id, text, visible_from_round "
        "FROM v_posts_final WHERE episode_id=? AND attempt=? ORDER BY clock_min, id", (ep, att)).fetchall()


def _to_post(r: sqlite3.Row) -> PostRow:
    return PostRow(r["id"], r["round"], r["sender"], r["slot"], r["clock"], r["clock_min"], r["kind"], r["line_id"],
                   r["text"], r["visible_from_round"])


def _kw_any(kw_json: str | None) -> bool | None:
    if kw_json is None:
        return None
    v = json.loads(kw_json)
    if isinstance(v, dict):
        return any(bool(x) for x in v.values())
    return bool(v)


def _note_text(subject: str, message: str) -> str:
    return "Subject: " + subject + "\nMessage: " + message


def _kw_match(row: sqlite3.Row, scenario: str) -> bool:
    from wtth import gates

    m = gates.note_matches(row["subject"], row["message"], scenario)
    stored = _kw_any(row["kw_hits_json"])
    if stored is not None and stored != m:
        raise ExtractionError(f"kw_match differs from stored kw_hits for note {row['id']}")
    return m


def _episode_cands(settings: Any, main_db: Any, ref: Any, in_set: bool) -> tuple[list[_Cand], int]:
    """Candidates of one final episode attempt (every type) and the number of empty D answers."""
    def read(conn: sqlite3.Connection):
        posts = _post_rows(conn, ref.episode_id, ref.attempt)
        notes = conn.execute(
            "SELECT id, round, sender, slot, subject, message, kw_hits_json FROM v_notes_final "
            "WHERE episode_id=? AND attempt=? AND source='tool' AND round BETWEEN ? AND ? ORDER BY id",
            (ref.episode_id, ref.attempt, *ROUNDS)).fetchall()
        deb = conn.execute(
            "SELECT slot, handle, question, answer_text FROM v_debrief_final WHERE episode_id=? AND attempt=? "
            "AND question IN ('D1','D2') ORDER BY slot, question", (ref.episode_id, ref.attempt)).fetchall()
        return posts, notes, deb

    posts_all, notes, deb = main_db.read(read)
    board = [_to_post(r) for r in posts_all]
    sonnet = ref.model == settings.models.sonnet
    if ref.model not in (settings.models.haiku, settings.models.sonnet):
        raise ExtractionError(f"episode {ref.episode_id}: unexpected model")
    base = dict(episode_id=ref.episode_id, attempt=ref.attempt, phase=ref.phase, scenario=ref.scenario,
                model=ref.model, cell=ref.cell, level=ref.level, seed=ref.seed, world=ref.world,
                in_analysis_set=in_set)
    kw = {n["id"]: _kw_match(n, ref.scenario) for n in notes}
    p2kw = not any(kw.values())
    note_slots = {n["slot"] for n in notes}
    out: list[_Cand] = []
    n_empty = 0

    def ctx(upto: int | None, exclude: int | None) -> str:
        return prompt_mod.board_context(board, upto_round=upto, exclude_post_id=exclude)

    def unit_of(subtier: str, c: _Cand) -> tuple:
        if subtier in ("J2a", "J2b", "J2c", "J2f", "J2d"):
            return ("w", ref.episode_id, ref.attempt, c.slot)
        if subtier in ("J1d", "J1e", "J2e", "J2g"):
            return ("e", ref.episode_id, ref.attempt)
        return ("i",) + c.key

    for n in notes:
        c = _Cand(ItemType.NOTE, ("notes", str(n["id"])), round=n["round"], slot=n["slot"], handle=n["sender"],
                  kw_match=kw[n["id"]], text=_note_text(n["subject"], n["message"]),
                  context_text=ctx(n["round"], None), **base)
        if not in_set:
            sub, rs = "J2i", ["above_sc"]
        elif not c.kw_match:
            sub, rs = ("J1b" if sonnet else "J1a"), ["p1_path"]
        elif not sonnet and ref.cell == "B-NONE":
            sub, rs = "J1c", ["bnone_note", "matched_note"]
        else:
            sub, rs = "J2h", ["matched_note"]
        if ref.cell == "B-NONE" and "bnone_note" not in rs:
            rs.append("bnone_note")
        c.subtier, c.reasons = sub, rs
        c.sort = (n["round"], n["id"])
        out.append(c)

    for p in posts_all:
        if p["kind"] != "live" or not (ROUNDS[0] <= p["round"] <= ROUNDS[1]):
            continue
        c = _Cand(ItemType.BOARD_POST, ("posts", str(p["id"])), round=p["round"], slot=p["slot"], handle=p["sender"],
                  kw_match=None, text=p["text"], context_text=ctx(p["round"], p["id"]), **base)
        noteless = p["slot"] not in note_slots
        rs = []
        if not in_set:
            sub, rs = "J2i", ["above_sc"]
        elif p2kw and ref.cell in P2_CELLS:
            sub, rs = ("J1e" if sonnet else "J1d"), ["p2kw_post"]
        elif sonnet:
            sub = "J2g"
        elif noteless:
            sub, rs = "J2d", ["noteless_post"]
        else:
            sub = "J2e"
        if noteless and "noteless_post" not in rs:
            rs.append("noteless_post")
        c.subtier, c.reasons = sub, rs
        c.sort = (p["round"], p["clock_min"], p["id"])
        out.append(c)

    for d in deb:
        if not d["answer_text"].strip():
            n_empty += 1
            continue
        q = d["question"]
        c = _Cand(ItemType.D1_ANSWER if q == "D1" else ItemType.D2_ANSWER,
                  ("debrief_answers", f"{ref.episode_id}:{ref.attempt}:{d['slot']}:{q}"), round=None,
                  slot=d["slot"], handle=d["handle"], kw_match=None, text=d["answer_text"],
                  context_text=ctx(None, None), **base)
        if not in_set:
            sub = "J2i"
        elif sonnet:
            sub = "J2f"
        else:
            sub = _J2_GROUP.get(ref.cell, "J2c")
        c.subtier, c.reasons = sub, (["above_sc"] if not in_set else []) + [q.lower()]
        c.sort = (d["slot"], q)
        out.append(c)

    for c in out:
        c.tier = "J1" if c.subtier.startswith("J1") else "J2"
        c.unit = unit_of(c.subtier, c)
    return out, n_empty


# ---- persistence ------------------------------------------------------------------------------------------------

_INSERT = ("INSERT OR IGNORE INTO items(item_id, item_type, source_table, source_id, held_out_key, episode_id, "
           "attempt, phase, scenario, model, cell, level, seed, world, round, slot, handle, in_analysis_set, "
           "kw_match, text, text_sha256, context_text, context_sha256, tier, subtier, reasons_json, order_key, "
           "extraction_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _new_id(taken: set[str]) -> str:
    while True:
        i = secrets.token_hex(6)
        if i not in taken:
            taken.add(i)
            return i


def _row_args(c: _Cand, item_id: str, order_key: int, extraction_id: str, now: float,
              held_out_key: str | None = None) -> tuple:
    return (item_id, c.item_type.value, c.key[0], c.key[1], held_out_key, c.episode_id, c.attempt, c.phase,
            c.scenario, c.model, c.cell, c.level, c.seed, c.world, c.round, c.slot, c.handle,
            1 if c.in_analysis_set else 0, None if c.kw_match is None else int(c.kw_match), c.text,
            sha256_text(c.text), c.context_text, sha256_text(c.context_text), c.tier, c.subtier,
            json.dumps(c.reasons), order_key, extraction_id, now)


def _order_cands(cands: list[_Cand], subtier: str, seed: int, cell_order: Sequence[str]) -> list[_Cand]:
    """Units of one sub-tier in their cell-balanced shuffled order; items inside a unit by `sort`."""
    by_unit: dict[tuple, list[_Cand]] = defaultdict(list)
    for c in sorted(cands, key=lambda c: (c.episode_id or 0, c.unit, c.sort)):
        by_unit[c.unit].append(c)
    units = [(items[0].cell or "", items) for _, items in sorted(by_unit.items(), key=lambda kv: kv[0])]
    return assign_order(units, seed=subtier_seed(seed, subtier), cell_order=cell_order)


def _cell_order(settings: Any) -> tuple[str, ...]:
    return tuple(settings.core_cells)


def _insert_cands(jdb: Any, cands: list[_Cand], *, extraction_row: tuple, order_seed: int, cell_order,
                  extra_detail: dict | None = None, held_out: bool = False) -> tuple[int, dict[str, int]]:
    existing = jdb.read(lambda c: {(r[0], r[1]) for r in c.execute("SELECT source_table, source_id FROM items")})
    taken = jdb.read(lambda c: {r[0] for r in c.execute("SELECT item_id FROM items")})
    maxk = jdb.read(lambda c: {r[0]: r[1] for r in c.execute("SELECT subtier, MAX(order_key) FROM items GROUP BY subtier")})
    new = [c for c in cands if c.key not in existing]
    by_sub: dict[str, list[_Cand]] = defaultdict(list)
    for c in new:
        by_sub[c.subtier].append(c)
    ordered: list[tuple[_Cand, int]] = []
    for sub, lst in by_sub.items():
        base = (maxk.get(sub) + 1) if maxk.get(sub) is not None else 0
        for i, c in enumerate(_order_cands(lst, sub, order_seed, cell_order)):
            ordered.append((c, base + i))
    ext_id = extraction_row[0]
    now = time.time()
    counts = {s: len(v) for s, v in by_sub.items()}
    detail = {"by_subtier": counts, **(extra_detail or {})}
    ext = list(extraction_row) + [len(new), json.dumps(detail, sort_keys=True)]

    def fn(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO extractions(extraction_id, ts, phase, snapshot_path, snapshot_sha256, s_c, "
                     "analysis_set_sha256, n_new_items, detail_json) VALUES (?,?,?,?,?,?,?,?,?)", ext)
        for c, ok in ordered:
            conn.execute(_INSERT, _row_args(c, _new_id(taken), ok, ext_id, now))

    jdb.write(fn)
    return len(new), counts


def extract(settings: Any, main_db: Any, jdb: Any, aset: Any, *, seeds: Any, snapshot: Any) -> ExtractionReport:
    """Extract every item of the analysis set and of the exploratory episodes. Idempotent: existing items keep
    their ids and order keys."""
    ensure_meta(jdb)
    yaml_path = Path(settings.repo_root) / "configs" / "analysis.yaml"
    if yaml_path.is_file():
        set_meta(jdb, "analysis_yaml_sha256", hashlib.sha256(yaml_path.read_bytes()).hexdigest())
    cands: list[_Cand] = []
    n_empty = 0
    for in_set, refs in ((True, aset.episodes), (False, aset.above)):
        for ref in refs:
            cs, ne = _episode_cands(settings, main_db, ref, in_set)
            cands.extend(cs)
            n_empty += ne
    ext_id = uuid.uuid4().hex
    row = (ext_id, time.time(), aset.phase, str(snapshot.path), snapshot.sha256, aset.s_c, aset.fingerprint)
    n_new, counts = _insert_cands(jdb, cands, extraction_row=row, order_seed=seeds.judge_order,
                                  cell_order=_cell_order(settings), extra_detail={"n_empty_answers": n_empty})
    log_event(jdb, "extraction", detail={"extraction_id": ext_id, "n_new": n_new, "by_subtier": counts,
                                         "n_empty_answers": n_empty})
    return ExtractionReport(ext_id, n_new, counts, n_empty)


# ---- D-022 exploratory follow-up ---------------------------------------------------------------------------------

EXPLORATORY_PHASES: tuple[str, ...] = ("exp1",)
EXPLORATORY_SUBTIERS: tuple[str, ...] = ("J1a", "J1b", "J2c", "J2f")   # non-keyword notes; D2 answers


@dataclass(frozen=True, slots=True)
class EpisodeKey:
    """The fields of an analysis-set EpisodeRef that _episode_cands reads."""
    episode_id: int
    attempt: int
    phase: str
    scenario: str
    model: str
    cell: str
    level: str
    seed: int
    world: str


def exploratory_refs(main_db: Any, phase: str) -> list[EpisodeKey]:
    """Done, non-probe episodes of an exploratory phase, final attempts, by episode id."""
    if phase not in EXPLORATORY_PHASES:
        raise ExtractionError(f"{phase!r} is not an exploratory phase")
    rows = main_db.read(lambda c: c.execute(
        "SELECT episode_id, final_attempt, phase, scenario, model, cell, level, seed, world FROM episodes "
        "WHERE phase=? AND probe=0 AND status='done' AND final_attempt IS NOT NULL ORDER BY episode_id",
        (phase,)).fetchall())
    return [EpisodeKey(int(r[0]), int(r[1]), r[2], r[3], r[4], r[5], r[6], int(r[7]), r[8]) for r in rows]


def inherit_validation(settings: Any, jdb: Any) -> dict[str, Any]:
    """Copy the passed held-out validation of the confirmatory judge database (read only) into an exploratory one:
    same judge model, same prompt version and system text, same context level. Idempotent; refuses when the core
    validation has not passed or the prompt text differs."""
    from wtth.judge.db import core_judge_db_path

    core = core_judge_db_path(settings)
    if Path(jdb.path).resolve() == core.resolve():
        raise ExtractionError("exploratory items never go to the confirmatory judge database")
    if not core.is_file():
        raise ExtractionError(f"no confirmatory judge database at {core}")
    conn = sqlite3.connect(f"file:{core}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM jmeta").fetchall())
        ver = meta.get("active_prompt_version") or "v1"
        val = conn.execute(
            "SELECT v.validation_id, v.ts, v.n_pass, v.n_items, v.context_level FROM validations v JOIN batches b "
            "ON b.batch_id=v.batch_id WHERE b.kind='first' AND v.prompt_version=? AND v.passed=1 ORDER BY v.ts DESC "
            "LIMIT 1", (ver,)).fetchone()
        pv = conn.execute("SELECT system_sha256 FROM prompt_versions WHERE prompt_version=?", (ver,)).fetchone()
    finally:
        conn.close()
    if meta.get("validation_passed") != "1" or val is None:
        raise ExtractionError("the confirmatory judge validation has not passed: nothing to inherit")
    want = prompt_mod.load_revision(settings, ver).system_sha256
    if pv is None or pv[0] != want:
        raise ExtractionError(f"prompt {ver}: the confirmatory system text differs from this build's")
    detail = {"from": str(core), "validation_id": val[0], "validation_ts": val[1], "n_pass": val[2],
              "n_items": val[3], "prompt_version": ver, "system_sha256": want,
              "level": meta.get("validation_level") or val[4]}
    ensure_meta(jdb)
    if get_meta(jdb, "validation_inherited_from"):
        prev = json.loads(get_meta(jdb, "validation_inherited_from") or "{}")
        if prev.get("validation_id") != detail["validation_id"] or prev.get("prompt_version") != ver:
            raise ExtractionError("this judge database inherited a different validation")
        return prev
    set_meta(jdb, "active_prompt_version", ver)
    set_meta(jdb, "validation_level", str(detail["level"]))
    set_meta(jdb, "validation_passed", "1")
    set_meta(jdb, "validation_inherited_from", json.dumps(detail, sort_keys=True))
    log_event(jdb, "validation_inherited", detail=detail)
    return detail


def extract_exploratory(settings: Any, main_db: Any, jdb: Any, phase: str, *, seeds: Any, snapshot: Any) -> ExtractionReport:
    """D-022 items of an exploratory phase, into its own judge database: the notes the keyword rule does not match
    (J1a Haiku, J1b Sonnet: the P1 and P2 judge path, as in the core) and the D2 answers (J2c Haiku, J2f Sonnet).
    Keyword-matched notes need no judge for P1; board posts and D1 answers are not exp1 outcomes. Idempotent."""
    from wtth.judge.db import core_judge_db_path

    if Path(jdb.path).resolve() == core_judge_db_path(settings).resolve():
        raise ExtractionError("exploratory items never go to the confirmatory judge database (set paths.judge_subdir)")
    ensure_meta(jdb)
    refs = exploratory_refs(main_db, phase)
    if not refs:
        raise ExtractionError(f"phase {phase!r} has no done episodes")
    cands: list[_Cand] = []
    n_empty = 0
    for ref in refs:
        cs, ne = _episode_cands(settings, main_db, ref, True)
        n_empty += ne
        for c in cs:
            keep = ((c.item_type == ItemType.NOTE and c.kw_match is False) or c.item_type == ItemType.D2_ANSWER)
            if keep:
                if c.subtier not in EXPLORATORY_SUBTIERS:
                    raise ExtractionError(f"unexpected sub-tier {c.subtier} for an exploratory item")
                c.reasons = list(c.reasons) + ["exploratory"]
                cands.append(c)
    fp = sha256_text(json.dumps([[r.episode_id, r.attempt, r.model, r.cell, r.seed] for r in refs]))
    ext_id = uuid.uuid4().hex
    row = (ext_id, time.time(), phase, str(snapshot.path), snapshot.sha256, None, fp)
    n_new, counts = _insert_cands(jdb, cands, extraction_row=row, order_seed=seeds.judge_order,
                                  cell_order=_cell_order(settings),
                                  extra_detail={"n_empty_answers": n_empty, "n_episodes": len(refs)})
    log_event(jdb, "extraction", detail={"extraction_id": ext_id, "phase": phase, "n_new": n_new,
                                         "by_subtier": counts, "n_empty_answers": n_empty})
    return ExtractionReport(ext_id, n_new, counts, n_empty)


def extract_g1(settings: Any, main_db: Any, jdb: Any, level: str, *, seeds: Any) -> ExtractionReport:
    """Pilot (G1) notes with no keyword hit, final attempts, for the gate P1 rule (D-006, D-007 A-44)."""
    from wtth import gates

    ensure_meta(jdb)

    def read(conn: sqlite3.Connection):
        notes = conn.execute(
            "SELECT id, episode_id, attempt, round, sender, slot, subject, message, kw_hits_json, phase, scenario, "
            "model, cell, level, seed, world FROM v_notes_final WHERE phase IN (?,?) AND cell='NONE' AND level=? "
            "AND source='tool' AND round BETWEEN ? AND ? ORDER BY episode_id, id",
            (*gates.G1_PHASES, level, *ROUNDS)).fetchall()
        posts = {}
        for ep, att in {(n["episode_id"], n["attempt"]) for n in notes}:
            posts[(ep, att)] = [_to_post(r) for r in _post_rows(conn, ep, att)]
        return notes, posts

    notes, posts = main_db.read(read)
    cands = []
    for n in notes:
        if _kw_match(n, n["scenario"]):
            continue
        c = _Cand(ItemType.NOTE, ("notes", str(n["id"])), n["episode_id"], n["attempt"], n["phase"], n["scenario"],
                  n["model"], n["cell"], n["level"], n["seed"], n["world"], n["round"], n["slot"], n["sender"],
                  False, False, _note_text(n["subject"], n["message"]),
                  prompt_mod.board_context(posts[(n["episode_id"], n["attempt"])], upto_round=n["round"],
                                           exclude_post_id=None))
        c.tier, c.subtier, c.reasons, c.unit = "G1", "G1", ["g1_gate", "p1_path"], ("i", "notes", str(n["id"]))
        c.sort = (n["round"], n["id"])
        cands.append(c)
    ext_id = uuid.uuid4().hex
    row = (ext_id, time.time(), "g1", str(main_db.path), "", None, "")
    n_new, counts = _insert_cands(jdb, cands, extraction_row=row, order_seed=seeds.judge_order,
                                  cell_order=_cell_order(settings), extra_detail={"level": level})
    log_event(jdb, "extraction", detail={"extraction_id": ext_id, "g1": True, "level": level, "n_new": n_new})
    return ExtractionReport(ext_id, n_new, counts, 0)


def ensure_held_out(settings: Any, jdb: Any, level: str, *, gate: bool = False) -> list[Item]:
    """The 12 held-out items (tier HO). With gate=True: separate G1-tier copies at `level` (pilot fallback).
    Gate copies use source_id 'G1:H<n>@<level>' so a pilot-level board never fixes the context of the
    # core's HO items (plan 2.1 lists plain 'H1'..'H12' for the core items; those are unchanged)."""
    ensure_meta(jdb)
    board = prompt_mod.held_out_board(settings, level)
    specs = prompt_mod.held_out_specs(settings)
    tier = "G1" if gate else "HO"
    sid = (lambda k: f"G1:{k}@{level}") if gate else (lambda k: k)
    existing = jdb.read(lambda c: {r[0]: r[1] for r in c.execute(
        "SELECT source_id, context_text FROM items WHERE source_table='held_out'")})
    for k, _t, _e in specs:
        old = existing.get(sid(k))
        if old is not None and old != board:
            raise ExtractionError(f"held-out items exist with a different board (level changed to {level}?)")
    cands = []
    for key, post, _exp in specs:
        c = _Cand(ItemType.BOARD_POST, ("held_out", sid(key)), None, None, "heldout", "main", settings.models.judge,
                  None, level, None, None, None, None, None, False, None, post, board)
        c.tier = c.subtier = tier
        c.reasons, c.unit, c.sort = ["held_out"], ("i", "held_out", sid(key)), (int(key[1:]),)
        cands.append(c)
    new = [c for c in cands if c.key[1] not in existing]
    if new:
        ext_id = uuid.uuid4().hex
        taken = jdb.read(lambda c: {r[0] for r in c.execute("SELECT item_id FROM items")})
        now = time.time()
        keyed = {sid(k): k for k, _t, _e in specs}
        base = jdb.read(lambda c: c.execute("SELECT COALESCE(MAX(order_key)+1,0) FROM items WHERE subtier=?",
                                            (tier,)).fetchone()[0])

        def fn(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO extractions(extraction_id, ts, phase, snapshot_path, snapshot_sha256, s_c, "
                         "analysis_set_sha256, n_new_items, detail_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         (ext_id, now, "heldout", "", "", None, "", len(new), json.dumps({"level": level})))
            for i, c in enumerate(new):
                conn.execute(_INSERT, _row_args(c, _new_id(taken), base + i, ext_id, now,
                                                held_out_key=keyed[c.key[1]]))

        jdb.write(fn)
    return [i for i in load_items(jdb, tier=tier) if i.source_table_is_held_out] if False else \
        [i for i in load_items(jdb, tier=tier) if i.key[0] == "held_out"]


# ---- reading items back ---------------------------------------------------------------------------------------

def _to_item(r: sqlite3.Row) -> Item:
    return Item(
        item_id=r["item_id"], item_type=ItemType(r["item_type"]), key=(r["source_table"], r["source_id"]),
        held_out_key=r["held_out_key"], episode_id=r["episode_id"], attempt=r["attempt"], phase=r["phase"],
        scenario=r["scenario"], model=r["model"], cell=r["cell"], level=r["level"], seed=r["seed"],
        world=r["world"], round=r["round"], slot=r["slot"], handle=r["handle"],
        in_analysis_set=bool(r["in_analysis_set"]), kw_match=None if r["kw_match"] is None else bool(r["kw_match"]),
        text=r["text"], context_text=r["context_text"], tier=r["tier"], subtier=r["subtier"],
        reasons=tuple(json.loads(r["reasons_json"])), order_key=r["order_key"])


def load_items(jdb: Any, *, tier: str | None = None, subtier: str | None = None,
               unsettled_only: bool = False) -> list[Item]:
    """Items ordered by (sub-tier, order_key). `unsettled_only`: no judge_labels row for the active prompt."""
    where, args = [], []
    if tier is not None:
        where.append("tier=?")
        args.append(tier)
    if subtier is not None:
        where.append("subtier=?")
        args.append(subtier)
    if unsettled_only:
        where.append("item_id NOT IN (SELECT item_id FROM judge_labels WHERE prompt_version=?)")
        args.append(active_version(jdb))
    sql = "SELECT * FROM items" + (" WHERE " + " AND ".join(where) if where else "")
    rows = jdb.read(lambda c: c.execute(sql, args).fetchall())
    items = [_to_item(r) for r in rows]
    items.sort(key=lambda i: (SUBTIERS.index(i.subtier), i.order_key))
    return items


def first_batch(jdb: Any, *, seed: int) -> list[Item]:
    """The 12 held-out items plus the first 38 J1 items in tier order, shuffled with Random(seed)."""
    ho = [i for i in load_items(jdb, tier="HO")]
    j1 = load_items(jdb, tier="J1")[:FIRST_BATCH_REAL]
    batch = ho + j1
    return random.Random(seed).sample(batch, len(batch))
