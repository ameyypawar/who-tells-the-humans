"""D-019: H-5 drawn once, voided duplicate redraw, focused review report."""
from __future__ import annotations

import re
import unittest

from tests.test_wp6_annot_queues import TestQueues
from tests.test_wp6_annot_report import ReportBase
from tests.wp6_annot_support import SEEDS, Jdb, d1_labels, label_all, note_labels, post_labels
from wtth.annotate import agreement, final, queues, report, void
from wtth.config import load_settings


class TestH5Once(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_settings()

    def test_second_stage2_inserts_no_h5_and_refreshes_latch(self):
        j, it = TestQueues.plant(self)
        queues.build_queues(self.settings, j.db, stage=1, seeds=SEEDS)
        first = queues.build_queues(self.settings, j.db, stage=2, seeds=SEEDS)
        self.assertGreater(first["H-5"], 0)
        # a late item appears (not queued); a rebuild must not draw it into H-5
        late = j.item("d1_answer", cell="NONE", ep=999, tier="J2", subtier="J2a")
        j.judge(late, d1_labels())
        second = queues.build_queues(self.settings, j.db, stage=2, seeds=SEEDS, kappa_mm_escalation=0.9, kappa_n=7)
        self.assertEqual(second["H-5"], 0)
        self.assertEqual(j.meta("h3b_kappa"), "0.9")
        self.assertEqual(j.meta("h3b_n"), "7")
        self.assertNotIn(late, j.queue_rows())


class TestVoid(ReportBase):
    def planted_redraw(self):
        j = Jdb("wp6_void")
        ids = [j.item("d1_answer", cell="NONE", ep=k, tier="J2", subtier="J2a") for k in range(12)]
        for i in ids:
            j.judge(i, d1_labels())
        a1 = [j.enqueue(i, "H-5", created_at=100.0) for i in ids[:6]]
        a2 = [j.enqueue(i, "H-5", created_at=200.0) for i in ids[6:]]
        return j, ids, a1, a2

    def test_void_is_idempotent_logged_and_insert_only(self):
        j, ids, a1, a2 = self.planted_redraw()
        with self.assertRaises(ValueError):
            void.void_h5_redraw(j.db, expect=5)
        r = void.void_h5_redraw(j.db, expect=6)
        self.assertEqual((r["first_draw"], r["voided_now"], r["voided_total"]), (6, 6, 6))
        again = void.void_h5_redraw(j.db)
        self.assertEqual((again["voided_now"], again["voided_total"]), (0, 6))
        voided = {r[0]: r[1] for r in j.db.read(lambda c: c.execute("SELECT ann_id, reason FROM ann_queue_void").fetchall())}
        self.assertEqual(set(voided), set(a2))
        self.assertEqual(set(voided.values()), {"duplicate H-5 draw on queue rebuild (D-019)"})
        self.assertEqual(j.db.read(lambda c: c.execute("SELECT COUNT(*) FROM jevents WHERE kind='void_h5_redraw'").fetchone()[0]), 1)
        import sqlite3
        for sql in ("DELETE FROM ann_queue_void", "UPDATE ann_queue_void SET reason='x'"):
            with self.assertRaises(sqlite3.DatabaseError):
                j.db.write(lambda c: c.execute(sql))

    def test_migration_on_a_database_without_the_table(self):
        j, ids, a1, a2 = self.planted_redraw()
        j.db.write(lambda c: (c.execute("DROP VIEW v_ann_queue"), c.execute("DROP TABLE ann_queue_void")))
        r = void.void_h5_redraw(j.db)
        self.assertEqual(r["voided_now"], 6)
        self.assertEqual(j.db.read(lambda c: c.execute("SELECT COUNT(*) FROM v_ann_queue").fetchone()[0]), 6)

    def test_every_consumer_excludes_voided_rows(self):
        j, ids, a1, a2 = self.planted_redraw()
        # Claude disagrees on the voided rows only: voiding must keep them out of kappa and counts
        label_all(j, self.settings, {a: d1_labels("asserted") for a in a2})
        before = agreement.agreement_summary(j.db, seeds=SEEDS)["model_model_h5"]["d1_answer"]["test_awareness"]["n"]
        self.assertEqual(before, 12)
        void.void_h5_redraw(j.db)
        s = agreement.agreement_summary(j.db, seeds=SEEDS)
        self.assertEqual(s["model_model_h5"]["d1_answer"]["test_awareness"]["n"], 6)
        self.assertEqual(s["model_model_h5"]["d1_answer"]["test_awareness"]["kappa"], None)    # six agreeing pairs
        prog = final.review_progress(j.db)
        self.assertEqual(prog["H-5"]["queued"], 6)
        src = final.JudgeDbLabelSource(j.db, strict=False)
        from tests.wp6_annot_support import key_of
        self.assertEqual(src.status(key_of(j, ids[0])), "pending_review")
        self.assertEqual(src.status(key_of(j, ids[7])), "judge")       # voided: no longer queued
        path = j.dir / "r.md"
        stats = report.write_review_report(self.settings, j.db, path, seed=1)
        self.assertEqual(stats["items"], 6)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(all(f"[{a}]" in text for a in a1) and not any(f"[{a}]" in text for a in a2))
        from wtth.annotate import export
        with self.assertRaises(ValueError):                              # nothing left to export: voided rows skipped
            export.export_annotator_batches(self.settings, j.db, j.dir / "x2", seed=1)


class TestFocusedReport(ReportBase):
    def test_queues_slice_and_gate_members_kept(self):
        j = Jdb("wp6_focus")
        ann = {}
        h1 = j.item("note", cell="NONE", kw=0)
        j.judge(h1, note_labels(viol=1))
        ann["h1"] = j.enqueue(h1, "H-1")
        h4 = j.item("note", cell="B-NONE", kw=0)
        j.judge(h4, note_labels(viol=0))
        ann["h4"] = j.enqueue(h4, "H-4")
        h2 = j.item("note", cell="CONCERN", kw=0)
        j.judge(h2, note_labels(viol=0))
        ann["h2"] = j.enqueue(h2, "H-2")
        h5 = []
        for cell in ("NONE", "CLAIM"):
            for k in range(4):
                i = j.item("d1_answer", cell=cell, ep=hash((cell, k)) % 10000, tier="J2", subtier="J2a")
                j.judge(i, d1_labels())
                h5.append(j.enqueue(i, "H-5", draw_rank=k + 1))
        label_all(j, self.settings, {})
        everything = report.write_review_report(self.settings, j.db, j.dir / "all.md", seed=1)
        self.assertEqual(everything["items"], 3 + 8)
        dry = report.write_review_report(self.settings, j.db, j.dir / "none.md", seed=1, queues=["H-1", "H-4", "H-5"],
                                         max_per_queue={"H-5": 3}, dry_run=True)
        self.assertEqual(dry["items"], 2 + 3)
        self.assertFalse((j.dir / "none.md").exists())
        path = j.dir / "f.md"
        report.write_review_report(self.settings, j.db, path, seed=1, queues=["H-1", "H-4", "H-5"], max_per_queue={"H-5": 3})
        text = path.read_text(encoding="utf-8")
        self.assertIn(f"[{ann['h1']}]", text)
        self.assertNotIn(f"[{ann['h2']}]", text)
        got5 = [a for a in h5 if f"[{a}]" in text]
        self.assertEqual(len(got5), 3)
        # round-robin across strata: both cells represented, lowest draw_ranks first
        ranks = {a: (n % 4 + 1) for n, a in enumerate(h5)}
        self.assertEqual({a in h5[:4] for a in got5}, {True, False})
        self.assertEqual(max(ranks[a] for a in got5), 2)
        with self.assertRaises(ValueError):
            report.write_review_report(self.settings, j.db, path, seed=1, queues=["H-9"])
        # import only reads ids, revisions and judge_shown, so reports from earlier versions keep importing
        from tests.test_wp6_annot_report import TestReviewImport
        path.write_text(TestReviewImport.decide(text, ann["h1"], "approve"), encoding="utf-8")
        self.assertEqual(report.import_review(self.settings, j.db, path)["imported"], 1)


if __name__ == "__main__":
    unittest.main()
