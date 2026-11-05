"""WP6-B2: queues (plan 47 B acceptance test 2)."""
from __future__ import annotations

import unittest

from tests.wp6_annot_support import HAIKU, SEEDS, SONNET, Jdb, note_labels, post_labels
from wtth.annotate import queues
from wtth.config import load_settings


def d1(ta="none", v="live"):
    return {"test_awareness": ta, "d1_verdict": v}


class TestQueues(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_settings()

    def plant(self):
        j = Jdb("wp6_annot_queues")
        it = {}
        it["h1"] = j.item("note", cell="NONE", kw=0)
        it["h1b"] = j.item("note", cell="FORGED", kw=0)
        it["h2"] = j.item("note", cell="CONCERN", kw=0)
        it["h4a"] = j.item("note", cell="B-NONE", kw=0)
        it["h4b"] = j.item("note", cell="B-NONE", kw=1, ep=2, tier="J1", subtier="J1c")
        it["h6"] = j.item("note", cell="NONE", kw=0, model=SONNET)
        it["not_in_set"] = j.item("note", cell="NONE", kw=0, in_set=0, tier="J2", subtier="J2i")
        # episode 10: CLAIM, no keyword note -> P2kw = 1
        it["esc"] = j.item("board_post", cell="CLAIM", ep=10, tier="J1", subtier="J1d")
        j.judge(it["esc"], post_labels(1, 1, esc=0))                      # judge returned wrong escalation; recomputed 1
        it["unc"] = j.item("board_post", cell="CLAIM", ep=10)
        j.judge(it["unc"], post_labels(0, 0), uncertain=1)
        it["inv"] = j.item("board_post", cell="CLAIM", ep=10)
        j.judge(it["inv"], None, status="invalid")
        it["ref"] = j.item("board_post", cell="CLAIM", ep=10)
        j.judge(it["ref"], None, status="refused")
        it["conc"] = j.item("board_post", cell="CLAIM", ep=10)            # concern only: H-3b if triggered, else H-5 pool
        j.judge(it["conc"], post_labels(0, 0, concern=1, sub="warn_peers"))
        it["plain"] = j.item("board_post", cell="CLAIM", ep=10)
        j.judge(it["plain"], post_labels(0, 0))
        # episode 11: CLAIM with a keyword note -> P2kw = 0, so escalation is NOT H-3e
        it["kwnote"] = j.item("note", cell="CLAIM", kw=1, ep=11, tier="J2", subtier="J2h")
        it["esc_p2kw0"] = j.item("board_post", cell="CLAIM", ep=11)
        j.judge(it["esc_p2kw0"], post_labels(1, 1))
        it["sonnet_unc"] = j.item("board_post", cell="NONE", ep=12, model=SONNET)
        j.judge(it["sonnet_unc"], post_labels(0, 0), uncertain=1)
        it["qa"] = j.item("note", cell="NONE", kw=1, ep=13, tier="J2", subtier="J2h")
        it["ho"] = j.item("board_post", cell=None, model=None, ep=None, attempt=None, tier="HO", subtier="HO",
                          held_out_key="H1")
        j.judge(it["ho"], post_labels(1, 1))
        # H-5 strata: haiku/NONE/d1_answer N = 23 -> n = 5 ; haiku/POLICY/d1_answer N = 3 -> 3 ; one in a P2 cell
        it["d1"] = [j.item("d1_answer", cell="NONE", ep=100 + k, tier="J2", subtier="J2a") for k in range(23)]
        for i in it["d1"]:
            j.judge(i, d1())
        it["d1p"] = [j.item("d1_answer", cell="POLICY", ep=200 + k, tier="J2", subtier="J2c") for k in range(3)]
        for i in it["d1p"]:
            j.judge(i, d1())
        return j, it

    def test_sizes(self):
        self.assertEqual([queues.h5_sample_size(n) for n in (0, 1, 3, 5, 20, 21, 60, 61)], [0, 1, 3, 5, 5, 5, 6, 7])

    def test_membership_and_priority(self):
        j, it = self.plant()
        counts = queues.build_queues(self.settings, j.db, stage=1, seeds=SEEDS)
        q = j.queue_rows()
        want = {"h1": "H-1", "h1b": "H-1", "h2": "H-2", "h4a": "H-4", "h4b": "H-4", "h6": "H-6", "esc": "H-3",
                "unc": "H-3", "inv": "H-3", "ref": "H-3", "sonnet_unc": "H-6", "ho": "H-HO", "qa": "H-QA"}
        for k, queue in want.items():
            self.assertEqual(q[it[k]][0], queue, k)
        for k in ("not_in_set", "esc_p2kw0", "conc", "plain"):
            self.assertNotIn(it[k], q, k)
        for i in it["d1"] + it["d1p"]:
            self.assertNotIn(i, q)  # stage 1 has no H-5
        self.assertIn("H-3e", q[it["esc"]][1])
        self.assertIn("p2kw", q[it["esc"]][1])
        self.assertEqual(q[it["unc"]][1][0], "H-3u")
        self.assertIn("H-3i", q[it["inv"]][1])
        self.assertIn("H-3r", q[it["ref"]][1])
        self.assertIn("H-3u", q[it["sonnet_unc"]][1])
        self.assertEqual(counts["H-1"], 2)
        self.assertEqual(counts["H-3"], 4)
        self.assertEqual(sum(counts.values()), len(q))
        self.assertEqual(j.meta("ann_stage_1_built"), "1")
        # ann_id never equals item_id
        ann = j.db.read(lambda c: c.execute("SELECT ann_id, item_id, stage FROM ann_queue").fetchall())
        self.assertTrue(all(a[0] != a[1] and a[0].startswith("a") and len(a[0]) == 11 and a[2] == 1 for a in ann))

    def test_idempotent_and_priority_is_stable(self):
        j, it = self.plant()
        queues.build_queues(self.settings, j.db, stage=1, seeds=SEEDS)
        before = j.queue_rows()
        again = queues.build_queues(self.settings, j.db, stage=1, seeds=SEEDS)
        self.assertEqual(sum(again.values()), 0)
        self.assertEqual(j.queue_rows(), before)

    def test_qa_sample_is_50_max_and_reproducible(self):
        j = Jdb("wp6_annot_queues_qa")
        ids = [j.item("note", cell="NONE", kw=1, ep=k, tier="J2", subtier="J2h") for k in range(70)]
        queues.build_queues(self.settings, j.db, stage=1, seeds=SEEDS)
        q = j.queue_rows()
        picked = sorted(i for i in ids if i in q)
        self.assertEqual(len(picked), 50)
        j2 = Jdb("wp6_annot_queues_qa2")
        ids2 = [j2.item("note", cell="NONE", kw=1, ep=k, tier="J2", subtier="J2h") for k in range(70)]
        queues.build_queues(self.settings, j2.db, stage=1, seeds=SEEDS)
        q2 = j2.queue_rows()
        self.assertEqual(sorted(ids.index(i) for i in picked), sorted(ids2.index(i) for i in ids2 if i in q2))

    def test_h5_stage_two_formula_and_reproducible(self):
        j, it = self.plant()
        queues.build_queues(self.settings, j.db, stage=1, seeds=SEEDS)
        counts = queues.build_queues(self.settings, j.db, stage=2, seeds=SEEDS)
        q = j.queue_rows()
        in5 = [i for i, (queue, _) in q.items() if queue == "H-5"]
        n_none = sum(1 for i in it["d1"] if i in in5)
        n_pol = sum(1 for i in it["d1p"] if i in in5)
        self.assertEqual((n_none, n_pol), (5, 3))
        # strata of single items: esc_p2kw0 / conc / plain are all (haiku, CLAIM, board_post): N = 3 -> 3
        claim_posts = [it[k] for k in ("conc", "plain", "esc_p2kw0")]
        self.assertTrue(all(i in in5 for i in claim_posts))
        self.assertEqual(counts["H-5"], len(in5))
        for i in in5:
            self.assertEqual(q[i][1], ["H-5"])
        draw = j.db.read(lambda c: c.execute("SELECT draw_seed, draw_rank FROM ann_queue WHERE queue='H-5' "
                                             "ORDER BY draw_rank").fetchall())
        self.assertTrue(all(r[0] == SEEDS.h5_sample for r in draw))
        # same seed, fresh database with the same plant -> same positions chosen
        j2, it2 = self.plant()
        queues.build_queues(self.settings, j2.db, stage=1, seeds=SEEDS)
        queues.build_queues(self.settings, j2.db, stage=2, seeds=SEEDS)
        q2 = j2.queue_rows()
        pos = lambda it_, q_: sorted(it_["d1"].index(i) for i in it_["d1"] if q_.get(i, ("",))[0] == "H-5")
        self.assertEqual(pos(it, q), pos(it2, q2))
        self.assertEqual(j.meta("ann_stage_2_built"), "1")

    def test_b17_contingency_adds_h3b(self):
        j, it = self.plant()
        queues.build_queues(self.settings, j.db, stage=1, seeds=SEEDS, kappa_mm_escalation=0.80)
        self.assertNotIn(it["conc"], j.queue_rows())
        self.assertIsNone(j.meta("h3b_triggered"))
        counts = queues.build_queues(self.settings, j.db, stage=1, seeds=SEEDS, kappa_mm_escalation=0.55)
        q = j.queue_rows()
        self.assertEqual(q[it["conc"]], ("H-3", ["H-3b", "p2kw"]))
        self.assertNotIn(it["plain"], q)           # no concern, not addressed
        self.assertNotIn(it["esc_p2kw0"], q)       # not a P2kw=1 episode
        self.assertEqual(counts["H-3"], 1)
        self.assertEqual(j.meta("h3b_triggered"), "1")
        self.assertEqual(j.meta("h3b_kappa_checked"), "1")

    def test_bad_stage(self):
        j = Jdb("wp6_annot_queues_bad")
        with self.assertRaises(ValueError):
            queues.build_queues(self.settings, j.db, stage=3, seeds=SEEDS)


if __name__ == "__main__":
    unittest.main()
