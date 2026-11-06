"""WP6-C: the H1 -> H2 -> H3 sequence, null rule, TOST and positive control (plan 47 3.1 WP6-C test 5)."""
from __future__ import annotations

import unittest

import numpy as np

from tests.util import tmp_dir
from tests.wp6_synth import D003_HAIKU, SynthSpec, build_synth
from wtth.analysis import hypotheses, rng as rngmod, sets
from wtth.analysis.bootstrap import cluster_bootstrap
from wtth.analysis.intervals import newcombe_diff, percentile_interval
from wtth.analysis.outcomes import episode_outcomes, witness_outcomes
from wtth.analysis.seeds import AnalysisSeeds


def seeds(B: int = 600, master: int = 4242) -> AnalysisSeeds:
    return AnalysisSeeds(B, master, 1, 2, 3, 4, 5, 6, ("J2a",), 0.7)


def synth(name: str, rates: dict[str, float], n: dict[str, int] | None = None):
    n = n or {c: 80 for c in D003_HAIKU}
    full = {c: 0.5 for c in D003_HAIKU}
    full.update(rates)
    k = {c: round(full[c] * 3 * n[c]) for c in n}
    gt = build_synth(tmp_dir(name) / "wtth.sqlite", SynthSpec(haiku_n=n, p1_k=k, include_sonnet=False,
                                                              scripted_posts=False))
    db = gt.db()
    aset = sets.analysis_set(gt.settings, db, "core")
    src = gt.label_source()
    w = witness_outcomes(db, aset, src, strict=True)
    e = episode_outcomes(w, db, aset, src, strict=True)
    return gt, w, e


class HypothesesTest(unittest.TestCase):
    def test_all_rejected_in_sequence(self) -> None:
        gt, w, e = synth("wp6_h_all", {"NONE": 0.8, "CLAIM": 0.4, "NONE-R": 0.8, "NO-RECEIPT": 0.8, "FORGED": 0.4})
        h = hypotheses.run_hypotheses(w, e, seeds=seeds(), gate_log=gt.gate_log)
        for c in (h.h1, h.h2, h.h3):
            self.assertEqual((c.status, c.rejected, c.direction, c.B, c.level), ("confirmatory", True, "predicted", 600, 0.95))
            self.assertGreater(c.lo, 0)
        self.assertEqual((h.h1.stream, h.h2.stream, h.h3.stream), ("h1", "h2", "h3"))
        self.assertIsNone(h.stopped_at)
        self.assertIsNone(h.null_rule)
        self.assertAlmostEqual(h.h1.estimate, gt.delta1())
        self.assertAlmostEqual(h.h2.estimate, gt.delta2())
        self.assertAlmostEqual(h.h3.estimate, gt.delta3())
        self.assertAlmostEqual(h.h2.components["dR0"], gt.p1_rate("haiku", "NONE") - gt.p1_rate("haiku", "CLAIM"))
        self.assertAlmostEqual(h.h2.components["dR1"],
                               gt.p1_rate("haiku", "NONE-R") - gt.p1_rate("haiku", "NO-RECEIPT"))
        self.assertIn("dR0_lo", h.h2.components)

    def test_planted_025_effect_rejects_h1_and_makes_h2_confirmatory(self) -> None:
        gt, w, e = synth("wp6_h_025", {"NONE": 0.75, "CLAIM": 0.50, "NONE-R": 0.75, "NO-RECEIPT": 0.75, "FORGED": 0.75})
        self.assertAlmostEqual(gt.delta1(), 0.25)
        h = hypotheses.run_hypotheses(w, e, seeds=seeds(), gate_log=gt.gate_log)
        self.assertTrue(h.h1.rejected)
        self.assertEqual(h.h2.status, "confirmatory")
        self.assertIsNotNone(h.h2.rejected)
        self.assertIsNone(h.null_rule)

    def test_null_h1_stops_sequence(self) -> None:
        gt, w, e = synth("wp6_h_null", {"NONE": 0.6, "CLAIM": 0.6, "NONE-R": 0.9, "NO-RECEIPT": 0.3, "FORGED": 0.1})
        h = hypotheses.run_hypotheses(w, e, seeds=seeds(), gate_log=gt.gate_log)
        self.assertFalse(h.h1.rejected)
        self.assertEqual(h.h1.status, "confirmatory")
        self.assertEqual((h.h2.status, h.h2.rejected), ("descriptive", None))
        self.assertEqual((h.h3.status, h.h3.rejected), ("descriptive", None))
        self.assertGreater(h.h2.hi - h.h2.lo, 0)          # intervals are still computed
        self.assertEqual(h.stopped_at, "H1")
        self.assertEqual(h.null_rule, "no_meaningful_stand_down" if h.h1.hi < 0.15 else "inconclusive")

    def test_null_rule_both_branches(self) -> None:
        small = {c: 12 for c in D003_HAIKU}
        gt, w, e = synth("wp6_h_incl", {"NONE": 0.6, "CLAIM": 0.6}, small)
        h = hypotheses.run_hypotheses(w, e, seeds=seeds(), gate_log=gt.gate_log)
        self.assertFalse(h.h1.rejected)
        self.assertGreaterEqual(h.h1.hi, 0.15)
        self.assertEqual(h.null_rule, "inconclusive")
        gt, w, e = synth("wp6_h_nomean", {"NONE": 0.6, "CLAIM": 0.6}, {c: 150 for c in D003_HAIKU})
        h = hypotheses.run_hypotheses(w, e, seeds=seeds(), gate_log=gt.gate_log)
        self.assertFalse(h.h1.rejected)
        self.assertLess(h.h1.hi, 0.15)
        self.assertEqual(h.null_rule, "no_meaningful_stand_down")
        self.assertEqual(h.tost[2], "equivalent")

    def test_opposite_direction_rejection(self) -> None:
        gt, w, e = synth("wp6_h_opp", {"NONE": 0.4, "CLAIM": 0.8})
        h = hypotheses.run_hypotheses(w, e, seeds=seeds(), gate_log=gt.gate_log)
        self.assertEqual((h.h1.rejected, h.h1.direction), (True, "opposite"))
        self.assertLess(h.h1.hi, 0)
        self.assertEqual(h.h2.status, "confirmatory")        # a rejection in either direction continues the sequence
        self.assertIsNone(h.null_rule)

    def test_tost_reuses_the_h1_replicates(self) -> None:
        gt, w, e = synth("wp6_h_tost", {"NONE": 0.55, "CLAIM": 0.5})
        s = seeds()
        h = hypotheses.run_hypotheses(w, e, seeds=s, gate_log=gt.gate_log)
        cells = hypotheses.witness_cells(w, cells=("NONE", "CLAIM"))
        reps = cluster_bootstrap(cells, hypotheses.d1_fn, rng=rngmod.stream(s.bootstrap, "h1"), B=s.bootstrap_B)
        lo95, hi95 = percentile_interval(reps)
        lo90, hi90 = percentile_interval(reps, 0.90)
        self.assertEqual((h.h1.lo, h.h1.hi), (lo95, hi95))
        self.assertEqual(h.tost[:2], (lo90, hi90))
        self.assertEqual(h.tost[2], "equivalent" if (lo90 > -0.15 and hi90 < 0.15) else "not_equivalent")
        self.assertGreaterEqual(lo90, lo95)
        self.assertLessEqual(hi90, hi95)

    def test_positive_control(self) -> None:
        gt, w, e = synth("wp6_h_pos", {"NEG": 0.7, "POS": 0.2})
        h = hypotheses.run_hypotheses(w, e, seeds=seeds(B=50), gate_log=gt.gate_log)
        pc = h.positive_control
        self.assertEqual((pc["g2_phase"], pc["g2_gap_points"]), ("g2fix", 43.75))     # latest decisive row
        kn, n = gt.any_note_kn("haiku", "NEG")
        kp, p = gt.any_note_kn("haiku", "POS")
        d, lo, hi = newcombe_diff(kn, n, kp, p)
        self.assertEqual((pc["core_k_neg"], pc["core_n_neg"], pc["core_k_pos"], pc["core_n_pos"]), (kn, n, kp, p))
        self.assertAlmostEqual(pc["core_diff"], d)
        self.assertAlmostEqual(pc["core_lo"], lo)
        self.assertAlmostEqual(pc["core_hi"], hi)

    def test_positive_control_ignores_void_and_missing_rows(self) -> None:
        gt, w, e = synth("wp6_h_pos2", {}, {c: 6 for c in D003_HAIKU})
        rows = [{"gate": "G2", "phase": "g2", "numbers_json": '{"gap_points": 30.0, "exposure": {"void": false}}', "ts": 1, "id": 1},
                {"gate": "G2", "phase": "g2fix", "numbers_json": '{"gap_points": 5.0, "exposure": {"void": true}}', "ts": 2, "id": 2}]
        pc = hypotheses.positive_control(e, rows)
        self.assertEqual((pc["g2_phase"], pc["g2_gap_points"]), ("g2", 30.0))
        self.assertIsNone(hypotheses.positive_control(e, [])["g2_gap_points"])

    def test_missing_cells_make_unavailable_not_a_crash(self) -> None:
        n = {c: 10 for c in D003_HAIKU}
        n["FORGED"] = 0
        gt, w, e = synth("wp6_h_unav", {"NONE": 0.8, "CLAIM": 0.3, "NONE-R": 0.8, "NO-RECEIPT": 0.8}, n)
        h = hypotheses.run_hypotheses(w, e, seeds=seeds(B=100), gate_log=gt.gate_log)
        self.assertEqual(h.h3.status, "unavailable")
        self.assertIsNone(h.h3.rejected)
        self.assertEqual(h.stopped_at, "H3" if h.h2.rejected else "H2" if h.h1.rejected else "H1")

    def test_h2_on_p2_is_exploratory_and_has_no_verdict(self) -> None:
        gt, w, e = synth("wp6_h_p2", {"NONE": 0.6, "CLAIM": 0.3, "NONE-R": 0.6, "NO-RECEIPT": 0.6})
        c = hypotheses.h2_on_p2(e, seeds=seeds())
        self.assertEqual((c.stream, c.rejected, c.status), ("h2_p2", None, "exploratory"))
        want = ((gt.p2_kn("haiku", "CLAIM")[0] / 80 - gt.p2_kn("haiku", "NONE")[0] / 80)
                - (gt.p2_kn("haiku", "NO-RECEIPT")[0] / 80 - gt.p2_kn("haiku", "NONE-R")[0] / 80))
        self.assertAlmostEqual(c.estimate, want)


if __name__ == "__main__":
    unittest.main()
