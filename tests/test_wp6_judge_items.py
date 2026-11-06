"""WP6-A acceptance test 3: item extraction on wp6_synth (needs analysis/sets.py from WP6-C)."""
from __future__ import annotations

import json
import re
import time
import unittest
from collections import Counter, defaultdict

from tests.util import seed_attempt, seed_episode, seed_run
from tests.wp6_fakejudge import SEEDS, Rig
from tests.wp6_synth import SynthSpec, build_synth
from wtth import render
from wtth.analysis import sets
from wtth.db import Db
from wtth.judge import items as IM
from wtth.judge.db import open_judge_db
from wtth.judge.types import ExtractionError, ItemType
from wtth.types import PostRow

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
P2 = IM.P2_CELLS


def kw_any(js):
    v = json.loads(js)
    return any(v.values()) if isinstance(v, dict) else bool(v)


class World:
    """A synthetic main database, a judge database, and both phases extracted."""

    def __init__(self, name: str, spec: SynthSpec):
        self.rig = Rig(name)
        self.s = self.rig.settings
        self.gt = build_synth(self.s.paths.runs_dir / "wtth.sqlite", spec)
        self.jdb = open_judge_db(self.s, create=True)
        self.reports = {}
        self.extract_all()

    def extract_all(self):
        out = {}
        for phase in ("core", "sonnet"):
            snap = sets.snapshot_main_db(self.s)
            self.db = Db(snap.path)
            self.snap = snap
            aset = sets.analysis_set(self.s, self.db, phase)
            out[phase] = (aset, IM.extract(self.s, self.db, self.jdb, aset, seeds=SEEDS, snapshot=snap))
        self.reports = out
        return out

    def items(self, **kw):
        return IM.load_items(self.jdb, **kw)


def add_pilot_and_probe(gt):
    """A done g1 pilot episode and a done core probe at seed 9001, each with a note and a post."""
    db = gt.db()

    def fn(conn):
        for phase, seed, probe in (("g1", 9011, 0), ("core", 9001, 1)):
            ep = seed_episode(conn, f"{phase}/main/{HAIKU}/NONE/L2/s{seed}", phase=phase, seed=seed, status="running",
                              queue_pos=900_000 + seed)
            if probe:
                conn.execute("UPDATE episodes SET probe=1 WHERE episode_id=?", (ep,))
            seed_attempt(conn, ep, 1, "run-1")
            conn.execute("INSERT INTO notes(episode_id, attempt, round, sender, slot, clock, clock_min, subject, message,"
                         " subject_raw, message_raw, source, visible_from_round, created_at, kw_hits_json) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,'tool',?,?,NULL)",
                         (ep, 1, 1, "rd-14", 1, "21:04", 1264, "pilot note", "Hendricks split", "a", "b", 2, time.time()))
            conn.execute("INSERT INTO posts(episode_id, attempt, round, sender, slot, clock, clock_min, kind, line_id,"
                         " is_private, text, text_raw, was_cut, was_collapsed, visible_from_round, created_at) VALUES "
                         "(?,?,?,?,?,?,?,'live',NULL,0,'pilot post',NULL,0,0,2,?)",
                         (ep, 1, 1, "rd-14", 1, "21:04", 1264, time.time()))
            conn.execute("UPDATE attempts SET status='done' WHERE episode_id=?", (ep,))
            conn.execute("UPDATE episodes SET status='done', final_attempt=1 WHERE episode_id=?", (ep,))
    db.write(fn)


class ItemsOnSynth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = SynthSpec.small(6, queued_at=(("NONE", 5),), extra_done={"B-NONE": (21, 22)})
        cls.w = World("items_synth", spec)
        add_pilot_and_probe(cls.w.gt)
        cls.w.extract_all()          # a second extraction after the pilot/probe rows exist
        cls.items = cls.w.items()
        cls.by_key = {i.key: i for i in cls.items}
        cls.gt = cls.w.gt

    def test_ids_unique_12_hex(self):
        ids = [i.item_id for i in self.items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{12}", i) for i in ids))

    def test_exactly_the_planted_items(self):
        want = {k: fl.item_type for k, fl in self.gt.labels.items()}
        got = {i.key: i.item_type for i in self.items if i.tier in ("J1", "J2")}
        self.assertEqual(got, want)

    def test_probes_pilots_and_decoys_absent(self):
        eps = {r[0]: (r[1], r[2]) for r in self.w.db.read(lambda c: c.execute(
            "SELECT episode_id, phase, seed FROM episodes").fetchall())}
        for it in self.items:
            if it.episode_id is None:
                continue
            ph, sd = eps[it.episode_id]
            self.assertIn(ph, ("core", "sonnet"))
            self.assertLess(sd, 9000)
        decoys = self.w.db.read(lambda c: [str(r[0]) for r in c.execute(
            "SELECT id FROM notes WHERE subject='decoy' OR subject='pilot note'").fetchall()])
        self.assertTrue(decoys)
        self.assertFalse({k for k in self.by_key if k[0] == "notes" and k[1] in decoys})
        failed_posts = self.w.db.read(lambda c: [str(r[0]) for r in c.execute(
            "SELECT p.id FROM posts p JOIN episodes e ON e.episode_id=p.episode_id WHERE p.attempt <> e.final_attempt "
            "OR p.text='pilot post'").fetchall()])
        self.assertTrue(failed_posts)
        self.assertFalse({k for k in self.by_key if k[0] == "posts" and k[1] in failed_posts})

    def test_analysis_set_flag_and_J2i(self):
        outside = {e["episode_id"] for e in self.gt.episodes if not e["inside"]}
        self.assertTrue(outside)
        for it in self.items:
            if it.tier not in ("J1", "J2"):
                continue
            self.assertEqual(it.in_analysis_set, it.episode_id not in outside, it.key)
            self.assertEqual(it.subtier == "J2i", it.episode_id in outside, it.key)
            self.assertEqual(it.tier, "J1" if it.subtier.startswith("J1") else "J2")

    def test_subtier_counts_equal_planted(self):
        db = self.w.db
        model = {r[0]: ("haiku" if "haiku" in r[1] else "sonnet") for r in db.read(
            lambda c: c.execute("SELECT episode_id, model FROM episodes").fetchall())}
        cell = {r[0]: r[1] for r in db.read(lambda c: c.execute("SELECT episode_id, cell FROM episodes").fetchall())}
        inside = {e["episode_id"] for e in self.gt.episodes if e["inside"]}
        valid = {r[0] for r in db.read(lambda c: c.execute(
            "SELECT episode_id FROM episodes WHERE phase IN ('core','sonnet') AND seed < 9000 AND probe=0 AND "
            "status='done'").fetchall())}
        notes = db.read(lambda c: c.execute(
            "SELECT n.id, n.episode_id, n.slot, n.kw_hits_json FROM notes n JOIN episodes e ON e.episode_id=n.episode_id"
            " AND n.attempt=e.final_attempt WHERE n.source='tool'").fetchall())
        exp = Counter()
        ep_has_kw = defaultdict(bool)
        slots_with_note = defaultdict(set)
        for n in notes:
            if n["episode_id"] in inside:
                ep_has_kw[n["episode_id"]] |= kw_any(n["kw_hits_json"])
                slots_with_note[n["episode_id"]].add(n["slot"])
        notes = [n for n in notes if n["episode_id"] in valid]
        for n in notes:
            e = n["episode_id"]
            if e not in inside:
                exp["J2i"] += 1
                continue
            kw = kw_any(n["kw_hits_json"])
            if not kw:
                exp["J1a" if model[e] == "haiku" else "J1b"] += 1
            elif model[e] == "haiku" and cell[e] == "B-NONE":
                exp["J1c"] += 1
            else:
                exp["J2h"] += 1
        posts = db.read(lambda c: c.execute(
            "SELECT p.id, p.episode_id, p.slot FROM posts p JOIN episodes e ON e.episode_id=p.episode_id AND "
            "p.attempt=e.final_attempt WHERE p.kind='live'").fetchall())
        for p in posts:
            e = p["episode_id"]
            if e not in valid:
                continue
            if e not in inside:
                exp["J2i"] += 1
            elif not ep_has_kw[e] and cell[e] in P2:
                exp["J1d" if model[e] == "haiku" else "J1e"] += 1
            elif model[e] == "sonnet":
                exp["J2g"] += 1
            elif p["slot"] not in slots_with_note[e]:
                exp["J2d"] += 1
            else:
                exp["J2e"] += 1
        deb = db.read(lambda c: c.execute(
            "SELECT d.episode_id, d.question, d.answer_text FROM debrief_answers d JOIN episodes e ON "
            "e.episode_id=d.episode_id AND d.attempt=e.final_attempt").fetchall())
        n_empty = 0
        for d in deb:
            if d["episode_id"] not in valid:
                continue
            if not d["answer_text"].strip():
                n_empty += 1
                continue
            e = d["episode_id"]
            if e not in inside:
                exp["J2i"] += 1
            elif model[e] == "sonnet":
                exp["J2f"] += 1
            else:
                exp["J2a" if cell[e] in ("NONE", "CLAIM") else "J2b" if cell[e] in ("NONE-R", "NO-RECEIPT", "FORGED")
                    else "J2c"] += 1
        got = Counter(i.subtier for i in self.items if i.tier in ("J1", "J2"))
        self.assertEqual(dict(got), dict(exp))
        self.assertGreater(len(got), 10)
        planted_empty = sum(1 for w in self.gt.witnesses if w["d1_verdict"] == "no_answer")
        self.assertEqual(n_empty, planted_empty)
        self.assertEqual(sum(r[1].n_empty_answers for r in self.w.reports.values()), n_empty)
        self.assertFalse([i for i in self.items if not i.text.strip()])

    def test_empty_answer_not_extracted(self):
        empties = self.w.db.read(lambda c: [(r[0], r[1], r[2]) for r in c.execute(
            "SELECT episode_id, attempt, slot FROM debrief_answers WHERE answer_text=''").fetchall()])
        self.assertTrue(empties)
        for ep, att, slot in empties:
            self.assertNotIn(("debrief_answers", f"{ep}:{att}:{slot}:D1"), self.by_key)

    def test_kw_match_equals_stored_hits(self):
        rows = {str(r[0]): kw_any(r[1]) for r in self.w.db.read(lambda c: c.execute(
            "SELECT id, kw_hits_json FROM notes WHERE source='tool' AND kw_hits_json IS NOT NULL").fetchall())}
        n = 0
        for it in self.items:
            if it.item_type == ItemType.NOTE and it.key[1] in rows:
                self.assertEqual(it.kw_match, rows[it.key[1]], it.key)
                n += 1
            if it.item_type != ItemType.NOTE:
                self.assertIsNone(it.kw_match)
        self.assertGreater(n, 50)

    def test_kw_mismatch_raises(self):
        row = {"id": 1, "subject": "x", "message": "the Hendricks batch", "kw_hits_json": json.dumps(
            {"subject": [], "message": []})}
        with self.assertRaises(ExtractionError):
            IM._kw_match(row, "main")
        row["kw_hits_json"] = json.dumps({"subject": [], "message": ["hendricks"]})
        self.assertTrue(IM._kw_match(row, "main"))

    def test_boards(self):
        db = self.w.db
        posts_by_ep = defaultdict(list)
        for r in db.read(lambda c: c.execute(
                "SELECT p.* FROM posts p JOIN episodes e ON e.episode_id=p.episode_id AND p.attempt=e.final_attempt"
        ).fetchall()):
            posts_by_ep[r["episode_id"]].append(PostRow(r["id"], r["round"], r["sender"], r["slot"], r["clock"],
                                                        r["clock_min"], r["kind"], r["line_id"], r["text"],
                                                        r["visible_from_round"]))
        seen = Counter()
        for it in self.items:
            if it.tier not in ("J1", "J2"):
                continue
            ps = posts_by_ep[it.episode_id]
            if it.item_type == ItemType.BOARD_POST:
                pid = int(it.key[1])
                want = render.board_lines([p for p in ps if p.round <= it.round and p.id != pid])
            elif it.item_type == ItemType.NOTE:
                want = render.board_lines([p for p in ps if p.round <= it.round])
            else:
                want = render.board_lines(ps)
            self.assertEqual(it.context_text, want, it.key)
            seen[it.item_type] += 1
            stamps = [(ln[1:6]) for ln in it.context_text.split("\n") if ln]
            self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(set(seen), set(ItemType))
        # a post's own text line is not in its board unless another post says the same words
        post = next(i for i in self.items if i.item_type == ItemType.BOARD_POST and i.tier == "J2")
        own = f"{post.handle}: {post.text}"
        same = [p for p in posts_by_ep[post.episode_id] if p.round <= post.round and p.sender == post.handle
                and p.text == post.text and p.id != int(post.key[1])]
        self.assertEqual(post.context_text.count(own), len(same))

    def test_order_cell_balanced_prefix(self):
        for sub in IM.SUBTIERS:
            its = [i for i in self.items if i.subtier == sub]
            if not its:
                continue
            its.sort(key=lambda i: i.order_key)
            self.assertEqual([i.order_key for i in its], list(range(len(its))), sub)
            units = []
            for i in its:
                u = self.unit_of(i)
                if not units or units[-1][0] != u:
                    units.append((u, i.cell))
            self.assertEqual(len({u for u, _ in units}), len(units), f"{sub}: a unit is not contiguous")
            cells = sorted({c for _, c in units})
            first = [c for _, c in units[:len(cells)]]
            self.assertEqual(sorted(first), cells, f"{sub}: first {len(cells)} units must cover all cells present")

    @staticmethod
    def unit_of(i):
        if i.subtier in ("J1d", "J1e", "J2e", "J2g"):
            return ("e", i.episode_id)
        if i.subtier in ("J2a", "J2b", "J2c", "J2d", "J2f"):
            return ("w", i.episode_id, i.slot)
        return ("i", i.key)

    def test_eleven_cells_in_first_eleven_units(self):
        its = sorted((i for i in self.items if i.subtier == "J1d"), key=lambda i: i.order_key)
        units = []
        for i in its:
            if not units or units[-1][0] != i.episode_id:
                units.append((i.episode_id, i.cell))
        self.assertGreaterEqual(len({c for _, c in units}), 6)     # eight P2 cells at most, J1d only has those
        self.assertTrue(set(c for _, c in units) <= set(P2))
        # all 11 cells through a sub-tier that holds every cell: D answers of J2a..J2c together are 11 cells
        d_cells = {i.cell for i in self.items if i.subtier in ("J2a", "J2b", "J2c")}
        self.assertEqual(len(d_cells), 11)

    def test_j1d_posts_contiguous_per_episode(self):
        its = sorted((i for i in self.items if i.subtier == "J1d"), key=lambda i: i.order_key)
        runs = [k for k, _ in __import__("itertools").groupby(i.episode_id for i in its)]
        self.assertEqual(len(runs), len(set(runs)))
        for _, grp in __import__("itertools").groupby(its, key=lambda i: i.episode_id):
            g = list(grp)
            self.assertEqual([i.round for i in g], sorted(i.round for i in g))

    def test_reextraction_keeps_ids_and_order(self):
        before = {r["item_id"]: (r["order_key"], r["source_id"], r["text_sha256"]) for r in self.w.jdb.read(
            lambda c: c.execute("SELECT * FROM items").fetchall())}
        n_ext = self.w.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM extractions").fetchone()[0])
        reps = self.w.extract_all()
        after = {r["item_id"]: (r["order_key"], r["source_id"], r["text_sha256"]) for r in self.w.jdb.read(
            lambda c: c.execute("SELECT * FROM items").fetchall())}
        self.assertEqual(before, after)
        self.assertEqual(sum(r[1].n_new for r in reps.values()), 0)
        self.assertEqual(self.w.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM extractions").fetchone()[0]),
                         n_ext + 2)

    def test_extraction_row_records_snapshot_and_fingerprint(self):
        r = self.w.jdb.read(lambda c: c.execute("SELECT * FROM extractions ORDER BY ts DESC LIMIT 1").fetchone())
        self.assertEqual(r["snapshot_sha256"], self.w.snap.sha256)
        self.assertEqual(len(r["analysis_set_sha256"]), 64)
        self.assertIn("by_subtier", json.loads(r["detail_json"]))

    def test_reasons(self):
        for it in self.items:
            if it.subtier in ("J1a", "J1b"):
                self.assertIn("p1_path", it.reasons)
            if it.subtier == "J2i":
                self.assertIn("above_sc", it.reasons)
            if it.subtier == "J1d":
                self.assertIn("p2kw_post", it.reasons)
            if it.item_type == ItemType.D1_ANSWER:
                self.assertIn("d1", it.reasons)


class Ordering(unittest.TestCase):
    def test_assign_order_round_robin_and_deterministic(self):
        cells = ["A", "B", "C"]
        units = [(c, [(c, n, 0), (c, n, 1)]) for c in cells for n in range(4)]
        a = IM.assign_order(units, seed=5, cell_order=cells)
        b = IM.assign_order(list(reversed(units)), seed=5, cell_order=cells)
        self.assertEqual(len(a), 24)
        firsts = [a[i][0] for i in range(0, 12, 2)]
        self.assertEqual(firsts, ["A", "B", "C", "A", "B", "C"])
        self.assertEqual(a[0][2:], (0,))               # unit-internal order is kept
        self.assertEqual([x[2] for x in a[:2]], [0, 1])
        self.assertNotEqual(a, IM.assign_order(units, seed=6, cell_order=cells))
        self.assertEqual(sorted(a), sorted(b))

    def test_uneven_cells(self):
        units = [("A", ["a1"]), ("A", ["a2"]), ("A", ["a3"]), ("B", ["b1"])]
        out = IM.assign_order(units, seed=1, cell_order=["A", "B"])
        self.assertEqual(out[1], "b1")
        self.assertEqual(len(out), 4)

    def test_subtier_seed_is_stable(self):
        import hashlib
        want = int.from_bytes(hashlib.sha256(b"42:J1a").digest()[:8], "big")
        self.assertEqual(IM.subtier_seed(42, "J1a"), want)
        self.assertNotEqual(IM.subtier_seed(42, "J1b"), want)


class HeldOutAndFirstBatch(unittest.TestCase):
    def setUp(self):
        self.rig = Rig("items_ho")
        self.s = self.rig.settings
        self.jdb = open_judge_db(self.s, create=True)

    def test_held_out_items(self):
        ho = IM.ensure_held_out(self.s, self.jdb, "L2")
        self.assertEqual(len(ho), 12)
        self.assertEqual({i.held_out_key for i in ho}, {f"H{n}" for n in range(1, 13)})
        self.assertEqual(len({i.context_text for i in ho}), 1)
        self.assertTrue(all(i.item_type == ItemType.BOARD_POST and i.tier == "HO" for i in ho))
        self.assertEqual({i.key[0] for i in ho}, {"held_out"})
        again = IM.ensure_held_out(self.s, self.jdb, "L2")
        self.assertEqual({i.item_id for i in again}, {i.item_id for i in ho})
        with self.assertRaises(ExtractionError):
            IM.ensure_held_out(self.s, self.jdb, "L3")

    def test_gate_copies_are_separate(self):
        ho = IM.ensure_held_out(self.s, self.jdb, "L2")
        g = IM.ensure_held_out(self.s, self.jdb, "L3", gate=True)
        self.assertEqual(len(g), 12)
        self.assertTrue({i.item_id for i in g}.isdisjoint({i.item_id for i in ho}))
        self.assertTrue(all(i.tier == "G1" for i in g))

    def test_first_batch_composition_and_determinism(self):
        w = World("items_fb", SynthSpec.small(6))
        IM.ensure_held_out(w.s, w.jdb, "L2")
        b1 = IM.first_batch(w.jdb, seed=77)
        b2 = IM.first_batch(w.jdb, seed=77)
        self.assertEqual([i.item_id for i in b1], [i.item_id for i in b2])
        self.assertEqual(len(b1), 50)
        self.assertEqual(sum(1 for i in b1 if i.tier == "HO"), 12)
        j1 = sorted((i for i in b1 if i.tier == "J1"), key=lambda i: (IM.SUBTIERS.index(i.subtier), i.order_key))
        self.assertEqual([i.item_id for i in j1], [i.item_id for i in IM.load_items(w.jdb, tier="J1")[:38]])
        self.assertNotEqual([i.item_id for i in b1], [i.item_id for i in IM.first_batch(w.jdb, seed=78)])
        self.assertEqual(sorted(i.subtier for i in j1)[0], "J1a")


if __name__ == "__main__":
    unittest.main()
