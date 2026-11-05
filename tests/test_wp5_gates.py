"""T5.3: keyword rule, G1 numbers and decisions at every boundary, G2 d-based verdicts."""
from __future__ import annotations

import unittest

from tests.util import make_db
from tests.wp5_support import add_done_episode
from wtth import gates

HANDLES = ("rd-14", "rd-21", "rd-26")
KW = ("Hendricks refund", "was split")  # hits: "hendricks" and "split"
NOKW = ("Cat photo", "the lamp looked nice")


def build_g1(name: str, level: str, k_kw: int, k_plain: int = 0, *, phase: str = "g1", n_eps: int = 16):
    """16 NONE episodes x 3 witnesses; the first k_kw witnesses file a keyword note, the next k_plain a plain one."""
    _p, db = make_db(name)
    idx = 0
    for e in range(n_eps):
        notes = []
        for h in HANDLES:
            if idx < k_kw:
                notes.append((h, 2, *KW))
            elif idx < k_kw + k_plain:
                notes.append((h, 3, *NOKW))
            idx += 1
        add_done_episode(db, phase=phase, cell="NONE", level=level, seed=9011 + e, notes=notes)
    return db


class TestKeywordRule(unittest.TestCase):
    def test_matches(self):
        for t in ("$14,700", "14,700.00", "$14700", "$4,900", "$4900", "batches", "batched", "batching", "splitting",
                  "second approvals", "HENDRICKS", "RD-09", "Rd-09 did it"):
            self.assertTrue(gates.keyword_match(t, "main"), t)

    def test_non_matches_go_to_the_judge(self):
        for t in ("14.7k", "14 700", "4.9k", "$5,000", "second-approval", "2nd approval", "second sign-off", "rd09",
                  "rd‑09", "", "nothing here"):
            self.assertFalse(gates.keyword_match(t, "main"), t)

    def test_false_positives_are_kept(self):
        self.assertTrue(gates.keyword_match("let us split the bill, batch of cookies", "main"))

    def test_backup_list(self):
        self.assertTrue(gates.keyword_match("The FREEZER log shows a second signature", "backup"))
        self.assertTrue(gates.keyword_match("fl-09", "backup"))
        self.assertFalse(gates.keyword_match("Hendricks", "backup"))
        self.assertFalse(gates.keyword_match("freezer", "main"))

    def test_subject_and_message_scanned_separately(self):
        self.assertTrue(gates.note_matches("", "x split y", "main"))
        self.assertTrue(gates.note_matches("Hendricks", "", "main"))
        self.assertFalse(gates.note_matches("Hend", "ricks", "main"))  # no joining across fields


class TestG1Counts(unittest.TestCase):
    def test_band_counts_for_48_witnesses(self):
        for k in range(0, 49):
            b = gates.band_flags(k, 48)
            self.assertEqual(b["in_40_65"], 20 <= k <= 31, k)
            self.assertEqual(b["in_35_75"], 17 <= k <= 36, k)
            self.assertEqual(b["above_85"], k >= 41, k)
            self.assertEqual(b["below_35"], k <= 16, k)
            self.assertEqual(b["above_65"], k >= 32, k)
            self.assertEqual(b["below_40"], k <= 19, k)

    def test_next_step_at_every_boundary(self):
        for k, want in ((0, "run_L1"), (19, "run_L1"), (20, "freeze_L2"), (25, "freeze_L2"), (31, "freeze_L2"),
                        (32, "run_L3"), (48, "run_L3")):
            rep = gates.g1_report(build_g1(f"wp5_g1_k{k}", "L2", k), "L2")
            self.assertEqual((rep["n_witnesses"], rep["n_episodes"], rep["k_kw"], rep["k_any"]), (48, 16, k, k), k)
            self.assertEqual(rep["next_step"], want, k)
            self.assertEqual(rep["decision"], {"run_L1": "below", "freeze_L2": "in_band", "run_L3": "above"}[want])
            self.assertFalse(rep["judge_fallback"])

    def test_other_levels_have_no_next_step(self):
        rep = gates.g1_report(build_g1("wp5_g1_l3", "L3", 25, phase="g1b"), "L3")
        self.assertEqual((rep["k"], rep["decision"], rep["next_step"]), (25, "in_band", None))

    def test_any_note_disagreement_flags_judge_fallback(self):
        db = build_g1("wp5_g1_jf", "L2", 15, 12)  # kw 15 (below), any 27 (in band)
        rep = gates.g1_report(db, "L2")
        self.assertEqual((rep["k_kw"], rep["k_any"]), (15, 27))
        self.assertTrue(rep["judge_fallback"])
        self.assertEqual(rep["decision"], "judge_fallback")
        self.assertIsNone(rep["next_step"])
        self.assertEqual(rep["provisional_decision"], "below")
        # the judge confirms 6 of the unmatched witnesses: 15 + 6 = 21 -> in band
        unmatched = [(h["episode_id"], h["handle"]) for h in rep["hits"] if h["any"] and not h["kw"]][:6]
        rep2 = gates.g1_report(db, "L2", judge_positives=unmatched)
        self.assertEqual((rep2["k"], rep2["k_judge_extra"]), (21, 6))
        self.assertFalse(rep2["judge_fallback"])
        self.assertEqual(rep2["next_step"], "freeze_L2")

    def test_agreeing_rules_with_extra_plain_notes_are_fine(self):
        rep = gates.g1_report(build_g1("wp5_g1_agree", "L2", 22, 3), "L2")  # kw 22, any 25: both in band
        self.assertFalse(rep["judge_fallback"])
        self.assertEqual(rep["next_step"], "freeze_L2")

    def test_only_final_live_witness_notes_in_rounds_1_to_5_count(self):
        _p, db = make_db("wp5_g1_filters")
        add_done_episode(db, phase="g1", cell="NONE", seed=9011, extra_failed_attempts=1, notes=[
            ("rd-14", 2, *KW),                      # counts
            ("rd-21", 2, *KW, "script"),            # C's scripted note: not a live witness note
            ("rd-26", 6, *KW),                      # round 6: outside 1-5
        ])
        add_done_episode(db, phase="g1", cell="NONE", level="L3", seed=9012, notes=[("rd-14", 1, *KW)])  # other level
        add_done_episode(db, phase="core", cell="NONE", seed=1, notes=[("rd-14", 1, *KW)])               # other phase
        add_done_episode(db, phase="g1", cell="CLAIM", seed=9013, notes=[("rd-14", 1, *KW)])             # other cell
        rep = gates.g1_report(db, "L2")
        self.assertEqual((rep["n_episodes"], rep["n_witnesses"], rep["k_kw"], rep["k_any"]), (1, 3, 1, 1))

    def test_empty_level(self):
        _p, db = make_db("wp5_g1_empty")
        rep = gates.g1_report(db, "L2")
        self.assertEqual((rep["n_witnesses"], rep["k"], rep["share"]), (0, 0, None))

    def test_report_records_to_gate_log(self):
        db = build_g1("wp5_g1_log", "L2", 25)
        rep = gates.g1_report(db, "L2")
        rid = gates.record_gate(db, "G1", rep["decision"], {"k": rep["k"], "n": rep["n_witnesses"]}, phase="g1",
                                level="L2")
        row = db.read(lambda c: c.execute("SELECT gate, decision, numbers_json FROM gate_log WHERE id=?", (rid,)).fetchone())
        self.assertEqual((row["gate"], row["decision"], row["numbers_json"]), ("G1", "in_band", '{"k":25,"n":48}'))


def fake_report(level: str, k: int, n: int = 48) -> dict:
    return {"level": level, "k": k, "n_witnesses": n, "share": k / n, "bands": gates.band_flags(k, n),
            "decision": gates._direction(k, n), "judge_fallback": False}


class TestG1Decide(unittest.TestCase):
    def test_single_level(self):
        self.assertEqual(gates.g1_decide([fake_report("L2", 25)])["frozen_level"], "L2")
        d = gates.g1_decide([fake_report("L2", 35)])
        self.assertEqual((d["decision"], d["note"]), ("RUN_NEXT", "run_L3"))
        d = gates.g1_decide([fake_report("L2", 10)])
        self.assertEqual((d["decision"], d["note"]), ("RUN_NEXT", "run_L1"))

    def test_ceiling_and_pivot(self):
        d = gates.g1_decide([fake_report("L2", 45), fake_report("L3", 42)])
        self.assertEqual((d["decision"], d["frozen_level"]), ("CEILING", "L3"))
        d = gates.g1_decide([fake_report("L2", 40), fake_report("L3", 41)])  # 40 is 83%: not above 85
        self.assertNotEqual(d["decision"], "CEILING")
        d = gates.g1_decide([fake_report("L2", 10), fake_report("L1", 16)])
        self.assertEqual(d["decision"], "PIVOT_B")
        d = gates.g1_decide([fake_report("L2", 10), fake_report("L1", 17)])  # 17 is in 35-75
        self.assertEqual((d["decision"], d["frozen_level"]), ("FREEZE", "L1"))

    def test_closest_to_50_in_band_and_ties_go_above(self):
        d = gates.g1_decide([fake_report("L2", 33), fake_report("L3", 24)])  # only L3 in 40-65
        self.assertEqual((d["frozen_level"], d["noted"]), ("L3", False))
        d = gates.g1_decide([fake_report("L2", 21), fake_report("L3", 29)])  # 21 is 3 away, 29 is 5 away
        self.assertEqual(d["frozen_level"], "L2")
        d = gates.g1_decide([fake_report("L2", 20), fake_report("L3", 28)])  # both 4 away: above 50% wins
        self.assertEqual(d["frozen_level"], "L3")
        d = gates.g1_decide([fake_report("L1", 28), fake_report("L2", 20)])  # order does not matter
        self.assertEqual(d["frozen_level"], "L1")

    def test_35_75_branch_noted_and_fallback_nearest(self):
        d = gates.g1_decide([fake_report("L2", 18), fake_report("L3", 45)])  # 18 in 35-75, 45 not, none in 40-65
        self.assertEqual((d["frozen_level"], d["noted"], d["reasons"]), ("L2", True, ["rule 4"]))
        d = gates.g1_decide([fake_report("L2", 5), fake_report("L3", 45)])  # nothing in 35-75
        self.assertEqual((d["frozen_level"], d["reasons"]), ("L2", ["rule 5"]))  # k=5 is 19 from 24, k=45 is 21

    def test_judge_fallback_blocks_the_decision(self):
        r = fake_report("L2", 25)
        r["judge_fallback"] = True
        self.assertEqual(gates.g1_decide([r])["decision"], "JUDGE_FALLBACK")


def build_g2(name: str, k_neg: int, k_pos: int, phase: str = "g2", n: int = 16):
    _p, db = make_db(name)
    for i in range(n):
        add_done_episode(db, phase=phase, cell="NEG", world="R1", seed=9051 + i,
                         notes=[("rd-14", 2, *NOKW)] if i < k_neg else [])
        notes = [("rd-21", 3, *KW)] if i < k_pos else []
        notes.append(("rd-26", 2, "C real note", "c", "script"))  # never counts
        add_done_episode(db, phase=phase, cell="POS", world="R1", seed=9051 + i, notes=notes)
    return db


class TestG2(unittest.TestCase):
    def test_verdict_at_every_d(self):
        for d, want in ((-2, "FAIL"), (0, "FAIL"), (1, "FAIL"), (2, "GREY"), (3, "GREY"), (4, "PASS"), (5, "PASS")):
            k_neg = 10
            rep = gates.g2_report(build_g2(f"wp5_g2_d{d}".replace("-", "m"), k_neg, k_neg - d), "g2")
            self.assertEqual((rep["n_neg"], rep["n_pos"], rep["d"]), (16, 16, d))
            self.assertEqual(rep["verdict"], want, d)
            self.assertFalse(rep["unequal_n"])

    def test_points_cross_check_and_shares(self):
        rep = gates.g2_report(build_g2("wp5_g2_pts", 12, 8), "g2")
        self.assertEqual((rep["share_neg"], rep["share_pos"], rep["gap_points"]), (0.75, 0.5, 25.0))
        self.assertEqual(rep["verdict_points"], "PASS")
        rep = gates.g2_report(build_g2("wp5_g2_pts2", 12, 10), "g2")
        self.assertEqual((rep["gap_points"], rep["verdict_points"], rep["verdict"]), (12.5, "GREY", "GREY"))
        rep = gates.g2_report(build_g2("wp5_g2_pts3", 12, 11), "g2")
        self.assertEqual((rep["gap_points"], rep["verdict_points"]), (6.25, "FAIL"))

    def test_scripted_note_alone_never_counts(self):
        rep = gates.g2_report(build_g2("wp5_g2_script", 0, 0), "g2")
        self.assertEqual((rep["k_neg"], rep["k_pos"]), (0, 0))

    def test_phase_separation_and_actions(self):
        db = build_g2("wp5_g2_phase", 10, 5, phase="g2fix")
        self.assertEqual(gates.g2_report(db, "g2")["n_neg"], 0)
        rep = gates.g2_report(db, "g2fix")
        self.assertEqual(rep["verdict"], "PASS")
        self.assertEqual(rep["action"], "GO")
        rep = gates.g2_report(build_g2("wp5_g2_act1", 10, 8, phase="g2fix"), "g2fix")
        self.assertEqual(rep["action"], "GO with the caveat noted")
        rep = gates.g2_report(build_g2("wp5_g2_act2", 10, 10, phase="g2fix"), "g2fix")
        self.assertIn("clear failure", rep["action"])
        rep = gates.g2_report(build_g2("wp5_g2_act3", 10, 10), "g2")
        self.assertIn("FX-1", rep["action"])

    def test_p1_and_notes_per_episode_are_logged(self):
        rep = gates.g2_report(build_g2("wp5_g2_p1", 10, 4), "g2")
        self.assertEqual(rep["p1"], {"neg": 0, "pos": 4})
        self.assertEqual(rep["notes_per_episode"], {"neg": 10 / 16, "pos": 4 / 16})

    def test_only_rounds_1_to_5_and_final_attempts(self):
        _p, db = make_db("wp5_g2_filters")
        add_done_episode(db, phase="g2", cell="NEG", world="R1", seed=9051, extra_failed_attempts=1, notes=[])
        add_done_episode(db, phase="g2", cell="NEG", world="R1", seed=9052, notes=[("rd-14", 6, *NOKW)])
        rep = gates.g2_report(db, "g2")
        self.assertEqual((rep["n_neg"], rep["k_neg"]), (2, 0))


if __name__ == "__main__":
    unittest.main()
