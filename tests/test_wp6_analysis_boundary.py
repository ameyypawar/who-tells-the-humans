"""Regression tests for the exact pre-registered boundaries (review 48 P0-1, P0-2): float noise must not decide a
rejection at lo = 0 or the null rule at hi = 15 points."""
from __future__ import annotations

import dataclasses
import unittest

import numpy as np

from wtth.analysis import text as T
from wtth.analysis.bootstrap import CellData
from wtth.analysis.hypotheses import H_CELLS, bootstrap_contrast, d2_fn, run_hypotheses
from wtth.analysis.intervals import q, rejects
from wtth.analysis.numbers import NumberBook, rate_prefix
from wtth.analysis.outcomes import WitnessOutcome
from wtth.analysis.secondary import p1_rates
from wtth.analysis.seeds import AnalysisSeeds

SEEDS = AnalysisSeeds(10000, 3703925950, 1, 1, 1, 1, 1, 1, ("J2a",), 0.7)
F = [f.name for f in dataclasses.fields(WitnessOutcome)]


def W(cell: str, seed: int, slot: int, p1: bool) -> WitnessOutcome:
    d = {f: None for f in F}
    d.update(episode_id=hash((cell, seed)) & 0xffffff, attempt=1, slot=slot, handle=f"rd-{slot}", phase="core",
             model="haiku", cell=cell, world="R0", seed=seed, paraphrase_idx=0, handle_idx=0, ticket_idx=0,
             any_note=p1, n_notes=int(p1), p1=p1, p1_pending=False, p1_no_round1=p1, first_round=2 if p1 else None,
             n_qual_notes=int(p1), att_lo=p1, att_hi=p1, notes=(), posts=(), concern_subtypes=(), claims_self=0,
             claims_other=0, claims_self_backed=0, claims_known=True, noteless=not p1, conc_esc_judge=False,
             conc_esc_final=False, all_posts_judged=True, all_posts_final=True, d1_known=False)
    return WitnessOutcome(**d)


def witnesses(ys: dict) -> list[WitnessOutcome]:
    return [W(cell, s, slot, slot <= int(y)) for cell, arr in ys.items() for s, y in enumerate(arr, 1) for slot in (1, 2, 3)]


class BoundaryTest(unittest.TestCase):
    def test_quantizer_removes_float_noise(self) -> None:
        self.assertEqual(q(5.55e-17), 0.0)
        self.assertEqual(q(0.14999999999999997), 0.15)
        self.assertFalse(rejects(5.55e-17, 0.2))
        self.assertFalse(rejects(-0.2, -1.1e-16))
        self.assertTrue(rejects(1e-9, 0.2))

    def test_every_k_over_240_boundary(self) -> None:
        for k in range(36, 241):
            self.assertEqual(q(k / 240 - (k - 36) / 240), 0.15)
        for a in range(0, 241, 3):
            for b in range(0, 241, 5):
                for c in range(0, 241, 7):
                    d = c - (a - b)
                    if 0 <= d <= 240:
                        self.assertEqual(q((a / 240 - b / 240) - (c / 240 - d / 240)), 0.0)

    def test_degenerate_did_is_exactly_zero_and_not_rejected(self) -> None:
        cells = {"NONE": CellData("NONE", np.full(80, 3.0), np.full(80, 3.0)),
                 "CLAIM": CellData("CLAIM", np.full(80, 2.0), np.full(80, 3.0)),
                 "NONE-R": CellData("NONE-R", np.full(80, 1.0), np.full(80, 3.0)),
                 "NO-RECEIPT": CellData("NO-RECEIPT", np.zeros(80), np.full(80, 3.0))}
        c, _ = bootstrap_contrast("H2", cells, H_CELLS["H2"], d2_fn, stream="h2", master=SEEDS.bootstrap, B=2000)
        self.assertEqual((c.estimate, c.lo, c.hi, c.rejected, c.direction), (0.0, 0.0, 0.0, False, "zero"))

    def test_realistic_h2_case_not_rejected_and_h3_descriptive(self) -> None:
        names = sorted(H_CELLS["H2"])
        gen = np.random.default_rng(12345)
        for _ in range(359):
            p = {"NONE": 0.55, "CLAIM": 0.55 - gen.uniform(0.05, 0.25), "NONE-R": 0.55,
                 "NO-RECEIPT": 0.55 - gen.uniform(0.0, 0.05)}
            arrs = {c: gen.binomial(3, p[c], size=80) for c in names}
        self.assertEqual({k: int(v.sum()) for k, v in arrs.items()},
                         {"CLAIM": 102, "NO-RECEIPT": 133, "NONE": 133, "NONE-R": 136})
        arrs["FORGED"] = np.full(50, 1)
        h = run_hypotheses(witnesses(arrs), [], seeds=SEEDS, gate_log=[])
        self.assertEqual(h.h2.lo, 0.0)                      # the exact lower bound is 0/240
        self.assertFalse(h.h2.rejected)
        self.assertEqual(h.h3.status, "descriptive")
        self.assertIsNone(h.h3.rejected)
        self.assertEqual(h.stopped_at, "H2")

    def test_h1_exactly_15_points_is_not_a_null_claim(self) -> None:
        gen = np.random.default_rng(99)
        for _ in range(266):
            pc = 0.5 - gen.uniform(-0.04, 0.06)
            yc = gen.binomial(3, pc, size=80)
            yn = gen.binomial(3, 0.5, size=80)
        self.assertEqual((int(yn.sum()), int(yc.sum())), (121, 108))
        ys = {"CLAIM": yc, "NONE": yn, "NONE-R": np.full(80, 1), "NO-RECEIPT": np.full(80, 1), "FORGED": np.full(50, 1)}
        w = witnesses(ys)
        h = run_hypotheses(w, [], seeds=SEEDS, gate_log=[])
        self.assertFalse(h.h1.rejected)
        self.assertEqual(h.h1.hi, 0.15)
        self.assertEqual(h.null_rule, "inconclusive")
        book = NumberBook()
        for r in p1_rates(w, seeds=dataclasses.replace(SEEDS, bootstrap_B=100)):
            book.add_rate(rate_prefix("haiku", "p1", r.cell), r)
        sentence = T.sentences(h, book, model_label="Haiku 5.5")["HOne"]
        self.assertNotIn("no meaningful stand-down", sentence)
        self.assertIn("cannot rule out", sentence)


if __name__ == "__main__":
    unittest.main()
