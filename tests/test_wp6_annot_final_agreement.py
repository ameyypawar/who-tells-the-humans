"""WP6-B3: final labels, completeness gates, model-model agreement summary (plan 47 B acceptance tests 7 and 8)."""
from __future__ import annotations

import json
import re
import unittest

from tests.test_wp6_annot_report import ReportBase, TestReviewImport
from tests.wp6_annot_support import SEEDS, Jdb, key_of, label_all, note_labels, post_labels, set_meta
from wtth.annotate import agreement, final, report
from wtth.judge.types import DictLabelSource, FinalLabel, ItemType


class TestFinal(ReportBase):
    def reviewed_setup(self, name):
        j, it, ann = self.plant(name)
        path = j.dir / "r.md"
        report.write_review_report(self.settings, j.db, path, seed=9)
        t = path.read_text(encoding="utf-8")
        t = TestReviewImport.decide(t, ann["conflict"], "use-judge")                       # -> judge labels
        t = TestReviewImport.decide(t, ann["unc"], "correct test_awareness=suspected")     # -> Claude + correction
        t = TestReviewImport.decide(t, ann["esc"], "approve")                              # -> Claude labels (esc 0)
        path.write_text(t, encoding="utf-8")
        report.import_review(self.settings, j.db, path)
        return j, it, ann

    def test_precedence_statuses_and_provenance(self):
        j, it, ann = self.reviewed_setup("wp6_annot_final")
        src = final.JudgeDbLabelSource(j.db, strict=False)
        k = lambda name: key_of(j, it[name])  # noqa: E731
        got = src.get(k("conflict"))
        self.assertEqual((got.source, got.reviewed, got.queue), ("reviewed", True, "H-1"))
        self.assertEqual(got.labels["refers_to_violation"], 0)                  # use_judge
        self.assertEqual(src.status(k("conflict")), "reviewed")
        got = src.get(k("unc"))
        self.assertEqual((got.labels["refers_to_violation"], got.labels["test_awareness"]), (1, "suspected"))
        got = src.get(k("esc"))
        self.assertEqual(got.labels["escalation_to_humans"], 0)                 # Claude (1, 0) approved over judge (1, 1)
        self.assertEqual(got.judge_labels["escalation_to_humans"], 1)
        self.assertIsInstance(got, FinalLabel)
        self.assertEqual(got.item_type, ItemType.BOARD_POST)
        # unreviewed with a valid judge label and a queue row: pending_review, provisional gets the judge label
        self.assertEqual(src.status(k("agree_h2")), "pending_review")
        got = src.get(k("agree_h2"))
        self.assertEqual((got.source, got.reviewed), ("judge", False))
        self.assertEqual(src.status(k("invalid")), "invalid")
        self.assertIsNone(src.get(k("invalid")))
        self.assertEqual(src.status(k("nojudge")), "not_judged")
        self.assertIsNone(src.get(k("nojudge")))
        self.assertEqual(src.status(("posts", "nope")), "unknown")
        self.assertIsNone(src.get(("posts", "nope")))

    def test_unqueued_judged_item_is_judge_status(self):
        j = Jdb("wp6_annot_final_unq")
        iid = j.item("d1_answer", tier="J2", subtier="J2a")
        j.judge(iid, {"test_awareness": "none", "d1_verdict": "live"})
        src = final.JudgeDbLabelSource(j.db, strict=True)
        self.assertEqual(src.status(key_of(j, iid)), "judge")
        self.assertEqual(src.get(key_of(j, iid)).source, "judge")

    def test_refused_status_and_reviewed_claude_only_item(self):
        j = Jdb("wp6_annot_final_ref")
        r = j.item("board_post", ep=3)
        j.judge(r, None, status="refused")
        a = j.enqueue(r, "H-3", ["H-3r", "p2kw"])
        label_all(j, self.settings, {a: post_labels(0, 0)})
        src = final.JudgeDbLabelSource(j.db, strict=False)
        self.assertEqual(src.status(key_of(j, r)), "refused")
        # review it: approve -> reviewed, Claude labels used
        path = j.dir / "r.md"
        report.write_review_report(self.settings, j.db, path, seed=1)
        path.write_text(TestReviewImport.decide(path.read_text(encoding="utf-8"), a, "approve"), encoding="utf-8")
        report.import_review(self.settings, j.db, path)
        src = final.JudgeDbLabelSource(j.db, strict=True)
        self.assertEqual(src.status(key_of(j, r)), "reviewed")
        self.assertEqual(src.get(key_of(j, r)).labels["concern"], 0)

    def test_gates_and_strict_vs_provisional(self):
        j = Jdb("wp6_annot_gates")
        h1 = j.item("note", cell="NONE", kw=0)
        j.judge(h1, note_labels(viol=1))
        h2 = j.item("note", cell="CONCERN", kw=0)
        j.judge(h2, note_labels(viol=0))
        e = j.item("board_post", cell="CLAIM", ep=9)
        j.judge(e, post_labels(1, 1))
        a1 = j.enqueue(h1, "H-1")
        j.enqueue(h2, "H-2")
        ae = j.enqueue(e, "H-3", ["H-3e", "p2kw"])
        label_all(j, self.settings, {})
        prov, strict = final.JudgeDbLabelSource(j.db, strict=False), final.JudgeDbLabelSource(j.db, strict=True)
        # queues not built yet -> not final even though nothing is unreviewed in an empty gate
        self.assertEqual(prov.gates(), {"p1_final": False, "p2prime_final": False})
        set_meta(j, "ann_stage_1_built", "1")
        set_meta(j, "ann_stage_2_built", "1")
        self.assertEqual(final.JudgeDbLabelSource(j.db, strict=False).gates(), {"p1_final": False, "p2prime_final": False})
        # strict withholds the unreviewed judge label of gate items only
        self.assertIsNone(strict.get(key_of(j, h1)))
        self.assertIsNone(strict.get(key_of(j, e)))
        self.assertEqual(strict.get(key_of(j, h2)).source, "judge")             # H-2 is not gating
        self.assertEqual(prov.get(key_of(j, h1)).source, "judge")
        # review H-1 only -> p1_final
        path = j.dir / "r.md"
        report.write_review_report(self.settings, j.db, path, seed=1)
        t = path.read_text(encoding="utf-8")
        t = TestReviewImport.decide(t, a1, "approve")
        path.write_text(t, encoding="utf-8")
        report.import_review(self.settings, j.db, path)
        self.assertEqual(final.JudgeDbLabelSource(j.db, strict=True).gates(), {"p1_final": True, "p2prime_final": False})
        # then the H-3e post
        report.write_review_report(self.settings, j.db, j.dir / "r2.md", seed=2)
        t = (j.dir / "r2.md").read_text(encoding="utf-8")
        t = TestReviewImport.decide(t, ae, "approve")
        (j.dir / "r2.md").write_text(t, encoding="utf-8")
        report.import_review(self.settings, j.db, j.dir / "r2.md")
        s2 = final.JudgeDbLabelSource(j.db, strict=True)
        # a model-model kappa on escalation exists now (one paired post): H-3b must have been checked first
        self.assertEqual(s2.gates()["p1_final"], True)
        prog = final.review_progress(j.db)
        self.assertEqual(prog["gate_p1"], {"queued": 1, "labelled": 1, "reviewed": 1, "pending_review": 0})
        self.assertEqual(prog["H-2"]["pending_review"], 1)
        self.assertEqual(prog["gate_p2prime"]["reviewed"], 1)

    def test_p2prime_requires_h3b_check_once_kappa_exists(self):
        j = Jdb("wp6_annot_gates_b17")
        posts = []
        for k, (jl, cl) in enumerate([(post_labels(1, 1), post_labels(1, 1)), (post_labels(0, 0), post_labels(0, 0)),
                                      (post_labels(1, 1), post_labels(1, 1))]):
            p = j.item("board_post", cell="CLAIM", ep=20 + k)
            j.judge(p, jl)
            posts.append(j.enqueue(p, "H-5") if k else j.enqueue(p, "H-3", ["H-3e", "p2kw"]))
        label_all(j, self.settings, {})
        self.assertIsNotNone(agreement.kappa_mm_escalation(j.db))
        for m in ("ann_stage_1_built", "ann_stage_2_built"):
            set_meta(j, m, "1")
        path = j.dir / "r.md"
        report.write_review_report(self.settings, j.db, path, seed=1)
        t = path.read_text(encoding="utf-8")
        t = TestReviewImport.decide(t, posts[0], "approve")
        path.write_text(t, encoding="utf-8")
        report.import_review(self.settings, j.db, path)
        self.assertFalse(final.JudgeDbLabelSource(j.db, strict=True).gates()["p2prime_final"])
        set_meta(j, "h3b_kappa_checked", "1")
        self.assertTrue(final.JudgeDbLabelSource(j.db, strict=True).gates()["p2prime_final"])

    def test_dict_label_source_matches_protocol_shape(self):
        src = DictLabelSource({})
        self.assertEqual(src.gates(), {"p1_final": True, "p2prime_final": True})


class TestAgreementSummary(ReportBase):
    def test_summary_numbers(self):
        j = Jdb("wp6_annot_summary")
        # 12 notes in H-5: judge refers = 1 for 6 of them; Claude disagrees on 2 (one each way)
        judge_viol = [1] * 6 + [0] * 6
        claude_viol = [1, 1, 1, 1, 1, 0] + [0, 0, 0, 0, 0, 1]
        labels = {}
        for k in range(12):
            i = j.item("note", cell="NONE", kw=0, ep=k, tier="J2", subtier="J2a")
            j.judge(i, note_labels(viol=judge_viol[k]))
            a = j.enqueue(i, "H-5")
            labels[a] = note_labels(viol=claude_viol[k])
        # posts for the correction rate / precision: judge escalation positives in H-3
        posts = []
        for k, (jl, cl) in enumerate([(post_labels(1, 1), post_labels(1, 1)), (post_labels(1, 1), post_labels(0, 0)),
                                      (post_labels(1, 1), post_labels(1, 1))]):
            p = j.item("board_post", cell="CLAIM", ep=50 + k)
            j.judge(p, jl)
            a = j.enqueue(p, "H-3", ["H-3e", "p2kw"])
            posts.append(a)
            labels[a] = cl
        label_all(j, self.settings, labels)
        s = agreement.agreement_summary(j.db, seeds=SEEDS)
        self.assertEqual(s["label"], "model-model (judge vs Claude proposal)")
        r = s["model_model"]["note"]["refers_to_violation"]
        self.assertEqual(r["n"], 12)
        self.assertEqual(r["confusion"], [[5, 1], [1, 5]])
        self.assertAlmostEqual(r["raw_agreement"], 10 / 12)
        self.assertAlmostEqual(r["kappa"], 2 / 3, places=9)
        self.assertLessEqual(r["kappa_lo"], r["kappa"])
        self.assertGreaterEqual(r["kappa_hi"], r["kappa"])
        self.assertEqual(s["model_model_h5"]["note"]["refers_to_violation"]["n"], 12)
        self.assertNotIn("board_post", s["model_model_h5"])
        self.assertEqual(s["model_model"]["board_post"]["escalation_to_humans"]["confusion"], [[0, 0], [1, 2]])
        self.assertEqual(agreement.kappa_mm_escalation(j.db, seeds=SEEDS) is None, False)
        # reproducible
        self.assertEqual(s, agreement.agreement_summary(j.db, seeds=SEEDS))
        # review: approve post0, correct post1 (Claude 0,0 -> 1,1 agrees with judge), approve post2; precision from H-3
        path = j.dir / "r.md"
        report.write_review_report(self.settings, j.db, path, seed=1)
        t = path.read_text(encoding="utf-8")
        t = TestReviewImport.decide(t, posts[0], "approve")
        t = TestReviewImport.decide(t, posts[1], "use-judge")
        t = TestReviewImport.decide(t, posts[2], "correct addressed_to_humans=0")
        path.write_text(t, encoding="utf-8")
        report.import_review(self.settings, j.db, path)
        s = agreement.agreement_summary(j.db, seeds=SEEDS)
        c = s["correction"]
        self.assertEqual((c["reviewed"], c["corrected"]), (3, 2))
        self.assertAlmostEqual(c["rate"], 2 / 3)
        self.assertEqual(c["by_queue"]["H-3"], {"reviewed": 3, "corrected": 2})
        # judge escalation positives: 3 reviewed; final escalation = 1, 1, 0 -> precision 2/3
        p = s["precision_escalation_h3"]
        self.assertEqual((p["n_judge_positive_reviewed"], p["n_confirmed"]), (3, 2))
        self.assertEqual(s["blind"], {"status": "not run", "by_type": {}})


if __name__ == "__main__":
    unittest.main()
