"""WP6-B1: kappa, bootstrap CI, confusion (plan 47 B acceptance test 8)."""
from __future__ import annotations

import unittest

import numpy as np
from statsmodels.stats.inter_rater import cohens_kappa

from wtth.annotate import agreement as ag


def table(a, b, cats):
    k = len(cats)
    t = np.zeros((k, k))
    for x, y in zip(a, b):
        t[cats.index(x), cats.index(y)] += 1
    return t


class TestKappa(unittest.TestCase):
    def test_matches_statsmodels_unweighted_and_linear(self):
        cats = ["none", "suspected", "asserted"]
        rng = np.random.default_rng(3)
        for _ in range(20):
            a = list(rng.choice(cats, 60, p=[.5, .3, .2]))
            b = [x if rng.random() < .7 else str(rng.choice(cats)) for x in a]
            t = table(a, b, cats)
            sm_u = cohens_kappa(t, return_results=False)
            sm_l = cohens_kappa(t, wt="linear", return_results=False)
            self.assertAlmostEqual(ag.cohen_kappa(a, b, categories=cats), float(sm_u), places=10)
            self.assertAlmostEqual(ag.cohen_kappa(a, b, categories=cats, weights="linear"), float(sm_l), places=10)

    def test_binary_fixed_table(self):
        a = [1] * 40 + [1] * 10 + [0] * 5 + [0] * 45
        b = [1] * 40 + [0] * 10 + [1] * 5 + [0] * 45
        self.assertAlmostEqual(ag.cohen_kappa(a, b, categories=[0, 1]),
                               float(cohens_kappa(table(a, b, [0, 1]), return_results=False)), places=10)
        self.assertAlmostEqual(ag.raw_agreement(a, b), 0.85)
        self.assertEqual(ag.confusion(a, b, categories=[0, 1]), [[45, 5], [10, 40]])

    def test_degenerate_is_none(self):
        self.assertIsNone(ag.cohen_kappa([1, 1, 1], [1, 1, 1], categories=[0, 1]))
        self.assertIsNone(ag.cohen_kappa([], [], categories=[0, 1]))
        self.assertIsNone(ag.cohen_kappa(["none", "none"], ["none", "none"], categories=["none", "suspected", "asserted"],
                                         weights="linear"))
        self.assertEqual(ag.kappa_ci([1, 1], [1, 1], categories=[0, 1], weights=None,
                                     rng=ag.kappa_rng(1), B=50), (None, None))

    def test_perfect_and_chance(self):
        a = [0, 1] * 10
        self.assertEqual(ag.cohen_kappa(a, a, categories=[0, 1]), 1.0)
        self.assertAlmostEqual(ag.cohen_kappa([0, 0, 1, 1], [0, 1, 0, 1], categories=[0, 1]), 0.0)

    def test_bootstrap_ci_reproducible_and_brackets(self):
        a = [1] * 40 + [1] * 10 + [0] * 5 + [0] * 45
        b = [1] * 40 + [0] * 10 + [1] * 5 + [0] * 45
        k = ag.cohen_kappa(a, b, categories=[0, 1])
        c1 = ag.kappa_ci(a, b, categories=[0, 1], weights=None, rng=ag.kappa_rng(7), B=500)
        c2 = ag.kappa_ci(a, b, categories=[0, 1], weights=None, rng=ag.kappa_rng(7), B=500)
        c3 = ag.kappa_ci(a, b, categories=[0, 1], weights=None, rng=ag.kappa_rng(8), B=500)
        self.assertEqual(c1, c2)
        self.assertNotEqual(c1, c3)
        self.assertLess(c1[0], k)
        self.assertGreater(c1[1], k)

    def test_set_label_gets_one_binary_kappa_per_code(self):
        pairs = [({"reason_codes": ["other", "trusted_claim"]}, {"reason_codes": ["other"]}),
                 ({"reason_codes": ["trusted_claim"]}, {"reason_codes": ["trusted_claim"]}),
                 ({"reason_codes": []}, {"reason_codes": ["other"]}),
                 ({"reason_codes": ["other"]}, {"reason_codes": ["other"]})]
        r = ag.compare_label("reason_codes:other", pairs)
        self.assertEqual(r["categories"], [0, 1])
        self.assertEqual(r["confusion"], [[1, 1], [0, 2]])
        names = ag.label_names("d2_answer", pairs)
        self.assertIn("reason_codes:could_not_verify_reported_anyway", names)
        self.assertNotIn("reason_codes", names)

    def test_score_pairs_recomputes_escalation_and_uses_linear_for_awareness(self):
        def post(addr, about, esc, ta="none"):
            return {"addressed_to_humans": addr, "about_violation": about, "escalation_to_humans": esc,
                    "concern": 0, "concern_subtype": None, "claim_present": 0, "claim_subject": None,
                    "test_awareness": ta}
        # the judge returned a wrong escalation value; recomputed value must win
        pairs = [(post(1, 1, 0), post(1, 1, 1)), (post(0, 1, 1), post(0, 1, 0)), (post(1, 0, 0, "suspected"),
                                                                                 post(1, 0, 0, "none"))]
        res = ag.score_pairs("board_post", pairs, rng=ag.kappa_rng(1), B=20)
        self.assertEqual(res["escalation_to_humans"]["confusion"], [[2, 0], [0, 1]])
        self.assertEqual(res["test_awareness"]["weights"], "linear")
        self.assertEqual(res["concern_subtype"]["categories"][0], "none")

    def test_bad_weights(self):
        with self.assertRaises(ValueError):
            ag.cohen_kappa([0, 1], [0, 1], categories=[0, 1], weights="quadratic")
        with self.assertRaises(ValueError):
            ag.cohen_kappa([0], [0, 1], categories=[0, 1])


if __name__ == "__main__":
    unittest.main()
