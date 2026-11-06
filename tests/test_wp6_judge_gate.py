"""WP6-A acceptance test 9: gate-g1 on a synthetic G1 database where the keyword and any-note rules disagree."""
from __future__ import annotations

import json
import unittest

from tests.wp5_support import add_done_episode
from tests.wp6_fakejudge import Rig, SEEDS, default_labels, answer_json, held_out_label_over, patch_seeds
from wtth import db as dbmod
from wtth.judge import gate
from wtth.judge import prompt as P
from wtth.judge.types import JudgeStop

HANDLES = ("rd-14", "rd-21", "rd-26")


class GateBase(unittest.TestCase):
    def build(self, n_kw=5, n_unmatched=17, n_ep=16):
        patch_seeds(self, SEEDS)
        self.rig = Rig(self.id().split(".")[-1][:36])
        self.s = self.rig.settings
        dbmod.create_db(self.s.paths.db_path)
        self.main = dbmod.Db(self.s.paths.db_path)
        self.unmatched = []
        slot = 0
        for ep in range(n_ep):
            notes = []
            for h in HANDLES:
                if slot < n_kw:
                    notes.append((h, 2, "Hendricks", "Hendricks split into batches"))
                elif slot < n_kw + n_unmatched:
                    token = f"UNM{slot:03d}"
                    notes.append((h, 3, f"Payment {token}", f"A transfer {token} lacked its sign-off"))
                    self.unmatched.append(token)
                slot += 1
            add_done_episode(self.main, phase="g1", cell="NONE", level="L2", seed=9011 + ep, notes=notes)
        return self


class GateG1(GateBase):
    """The id inside each answer must equal the item id, which is only known after extraction: the fake answers are
    generated lazily by running the extraction first (extract_g1 is idempotent)."""

    def prepare(self, positives):
        from wtth.judge import items as IM
        from wtth.judge.db import open_judge_db

        jdb = open_judge_db(self.s, create=True)
        IM.extract_g1(self.s, self.main, jdb, "L2", seeds=SEEDS)
        g1 = IM.ensure_held_out(self.s, jdb, "L2", gate=True)
        notes = [i for i in IM.load_items(jdb, tier="G1") if i.held_out_key is None]
        exp = {k: e for k, _t, e in P.held_out_specs(self.s)}
        for it in g1:
            self.rig.rule(it, answer_json(it.item_id, it.text, default_labels(
                "board_post", **held_out_label_over(exp[it.held_out_key]))))
        for it in notes:
            tok = it.text.split("UNM")[1][:3]
            self.rig.rule(it, answer_json(it.item_id, it.text, default_labels(
                "note", refers_to_violation=1 if f"UNM{tok}" in positives else 0)))
        self.rig.write()
        return notes

    def test_judge_positives_flip_the_decision_and_row_is_logged(self):
        self.build()
        report = __import__("wtth.gates", fromlist=["g1_report"]).g1_report(self.main, "L2")
        self.assertTrue(report["judge_fallback"])
        self.assertEqual((report["k_kw"], report["k_any"], report["n_witnesses"]), (5, 22, 48))
        pos = set(self.unmatched)                       # all 17 unmatched notes refer to the violation
        notes = self.prepare(pos)
        self.assertEqual(len(notes), 17)
        out = gate.g1_fallback(self.s, "L2", k=3)
        self.assertTrue(out["judge_used"])
        self.assertEqual(len(out["positives"]), 17)
        self.assertEqual(out["report"]["k"], 22)
        self.assertEqual(out["decision"]["decision"], "FREEZE")
        self.assertEqual(out["holdout"], 12)
        rows = self.main.read(lambda c: c.execute("SELECT * FROM gate_log WHERE gate='G1'").fetchall())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["decision"], r["level"]), ("FREEZE", "L2"))
        detail, nums = json.loads(r["detail_json"]), json.loads(r["numbers_json"])
        self.assertIs(detail["judge_used"], True)
        self.assertEqual(detail["review"], "pending")
        self.assertIs(nums["judge_used"], True)
        self.assertEqual(nums["review"], "pending")
        self.assertEqual((nums["k_kw"], nums["k_any"], nums["k_judge_extra"], nums["k"]), (5, 22, 17, 22))
        self.assertEqual(out["gate_log_id"], r["id"])

    def test_zero_positives_decide_from_k_kw(self):
        self.build()
        self.prepare(set())
        out = gate.g1_fallback(self.s, "L2", k=3)
        self.assertTrue(out["judge_used"])
        self.assertEqual(out["positives"], [])
        rep = out["report"]
        self.assertIs(rep["judge_fallback"], False)
        self.assertEqual(rep["decision"], rep["direction_kw"])
        self.assertEqual(out["decision"]["decision"], "RUN_NEXT")            # 5 of 48 is below the band
        self.assertEqual(out["decision"]["note"], "run_L1")
        rows = self.main.read(lambda c: c.execute("SELECT numbers_json, detail_json, decision FROM gate_log").fetchall())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "RUN_NEXT")
        self.assertEqual(json.loads(rows[0]["numbers_json"])["n_judge_positive"], 0)

    def test_no_judge_when_the_rules_agree(self):
        self.build(n_kw=20, n_unmatched=0)              # k_kw = k_any = 20: both in band
        out = gate.g1_fallback(self.s, "L2", k=3)
        self.assertFalse(out["judge_used"])
        self.assertEqual(out["decision"]["decision"], "FREEZE")
        self.assertIsNone(out["gate_log_id"])
        self.assertEqual(self.main.read(lambda c: c.execute("SELECT COUNT(*) FROM gate_log").fetchone()[0]), 0)
        from wtth.judge.db import judge_db_path
        self.assertFalse(judge_db_path(self.s).exists())

    def test_failed_held_out_validation_blocks_the_gate(self):
        self.build()
        from wtth.judge import items as IM
        from wtth.judge.db import open_judge_db

        jdb = open_judge_db(self.s, create=True)
        IM.extract_g1(self.s, self.main, jdb, "L2", seeds=SEEDS)
        g1 = IM.ensure_held_out(self.s, jdb, "L2", gate=True)
        for it in IM.load_items(jdb, tier="G1"):          # every item answered with all-zero labels
            self.rig.rule(it, answer_json(it.item_id, it.text, default_labels(it.item_type)))
        self.rig.write()
        with self.assertRaises(JudgeStop):
            gate.g1_fallback(self.s, "L2", k=3)
        self.assertEqual(self.main.read(lambda c: c.execute("SELECT COUNT(*) FROM gate_log").fetchone()[0]), 0)


if __name__ == "__main__":
    unittest.main()
