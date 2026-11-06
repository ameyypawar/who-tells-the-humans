"""D-021 post-hoc analyses (robustness, timing), the recorded-prediction status (review M8), the D-022 exp1 section, the
author-read reader and the by-paraphrase breakdown (PREREG 16): known values, planted witnesses and the synthetic
database."""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from tests.util import tmp_dir
from tests.wp6_synth import SynthSpec, build_synth
from wtth.analysis import humanread, posthoc, register
from wtth.analysis import rng as rngmod
from wtth.analysis.bootstrap import CellData, cluster_bootstrap_multi
from wtth.analysis.hypotheses import bootstrap_contrast, witness_cells
from wtth.analysis.intervals import newcombe_diff, wilson
from wtth.analysis.numbers import NumberBook, fmt_value
from wtth.analysis.results import CellRate, Contrast, DescriptiveResults, HypothesisResults
from wtth.analysis.run import run_analysis
from wtth.analysis.seeds import AnalysisSeeds
from wtth.judge.types import DictLabelSource

SEEDS = AnalysisSeeds(200, 31337, 1, 2, 3, 4, 5, 6, ("J2a",), 0.7)
SEEDS_BIG = AnalysisSeeds(4000, 31337, 1, 2, 3, 4, 5, 6, ("J2a",), 0.7)
EXP1 = {("haiku", "X-FORGED-POLICY"): 50, ("haiku", "X-FORGED-PROV"): 50, ("sonnet", "RECEIPT"): 30}
EXP1_K = {("haiku", "X-FORGED-POLICY"): 120, ("haiku", "X-FORGED-PROV"): 75, ("sonnet", "RECEIPT"): 50}


CELL_IDS = ("NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS", "B-NONE")


def wit(cell: str, seed: int, slot: int, p1: bool) -> SimpleNamespace:
    return SimpleNamespace(cell=cell, seed=seed, slot=slot, p1=p1, episode_id=seed * 100 + CELL_IDS.index(cell),
                           attempt=1, handle=f"rd-0{slot}", first_round=2 if p1 else None, reasons=(), esc_post=False)


def planted(n_eps: int, cell: str, nonreport_seeds: dict[int, int]) -> list[SimpleNamespace]:
    """n_eps shifts of 3 witnesses; nonreport_seeds: seed -> number of non-reporting witnesses in that shift."""
    out = []
    for s in range(1, n_eps + 1):
        bad = nonreport_seeds.get(s, 0)
        for slot in (1, 2, 3):
            out.append(wit(cell, s, slot, slot > bad))
    return out


def core_like() -> list[SimpleNamespace]:
    return (planted(80, "NONE", {}) + planted(80, "CLAIM", {3: 1, 17: 1, 40: 1, 66: 1})
            + planted(80, "NONE-R", {}) + planted(80, "NO-RECEIPT", {s: (2 if s % 3 == 0 else 1) for s in range(5, 22)})
            + planted(50, "FORGED", {s: 1 + s % 2 for s in range(1, 34)}) + planted(50, "POLICY", {7: 1, 21: 1}))


# ---- exact, score and MOVER intervals ------------------------------------------------------------------------------

class ExactTest(unittest.TestCase):
    def test_fisher_known_values(self) -> None:
        self.assertAlmostEqual(posthoc.fisher_exact_two_sided(3, 4, 1, 4), 34 / 70, places=15)      # tea tasting
        # H1 as observed: 240/240 against 236/240; the two tails are the 0- and 4-non-reporter tables
        want = float(2 * Fraction(math.comb(476, 240), math.comb(480, 240)))
        self.assertEqual(posthoc.fisher_exact_two_sided(240, 240, 236, 240), want)
        self.assertEqual(fmt_value(want, "pval"), "0.123")
        self.assertEqual(posthoc.fisher_exact_two_sided(5, 10, 5, 10), 1.0)
        self.assertEqual(posthoc.fisher_exact_two_sided(7, 12, 2, 9), posthoc.fisher_exact_two_sided(2, 9, 7, 12))
        with self.assertRaises(ValueError):
            posthoc.fisher_exact_two_sided(5, 4, 1, 2)

    def test_fisher_matches_scipy(self) -> None:
        try:
            from scipy.stats import fisher_exact
        except ImportError:  # pragma: no cover
            self.skipTest("scipy not installed")
        r = np.random.default_rng(7)
        for _ in range(150):
            n1, n2 = int(r.integers(1, 70)), int(r.integers(1, 70))
            k1, k2 = int(r.integers(0, n1 + 1)), int(r.integers(0, n2 + 1))
            want = fisher_exact([[k1, n1 - k1], [k2, n2 - k2]]).pvalue
            self.assertAlmostEqual(posthoc.fisher_exact_two_sided(k1, n1, k2, n2), want, places=10, msg=(k1, n1, k2, n2))

    def test_newcombe_1998_table_ii(self) -> None:
        """Newcombe (1998), method 10, the published examples."""
        for (k1, n1, k2, n2), (lo, hi) in (((56, 70, 48, 80), (0.0524, 0.3339)), ((9, 10, 3, 10), (0.1705, 0.8090)),
                                           ((6, 7, 2, 7), (0.0582, 0.8062)), ((5, 56, 0, 29), (-0.0381, 0.1926)),
                                           ((0, 10, 0, 20), (-0.1611, 0.2775)), ((10, 10, 0, 10), (0.6075, 1.0))):
            _, a, b = newcombe_diff(k1, n1, k2, n2)
            self.assertAlmostEqual(a, lo, places=4)
            self.assertAlmostEqual(b, hi, places=4)

    def test_mover(self) -> None:
        d, lo, hi = posthoc.mover_diff(0.1, 0.05, 0.2, 0.3, 0.2, 0.35)
        self.assertAlmostEqual(d, -0.2, places=15)
        self.assertAlmostEqual(lo, -0.2 - math.sqrt(0.05 ** 2 + 0.05 ** 2), places=15)
        self.assertAlmostEqual(hi, -0.2 + math.sqrt(0.1 ** 2 + 0.1 ** 2), places=15)
        for k1, n1, k2, n2 in ((236, 240, 217, 240), (3, 20, 9, 25), (0, 10, 10, 10), (40, 80, 22, 80)):
            (l1, u1), (l2, u2) = wilson(k1, n1), wilson(k2, n2)
            got = posthoc.mover_diff(k1 / n1, l1, u1, k2 / n2, l2, u2)       # MOVER with Wilson = Newcombe 10
            for a, b in zip(got, newcombe_diff(k1, n1, k2, n2)):
                self.assertAlmostEqual(a, b, places=12)

    def test_p_none_drawn(self) -> None:
        self.assertEqual(posthoc.p_none_drawn(80, 4), float(Fraction(19, 20) ** 80))
        self.assertEqual(fmt_value(posthoc.p_none_drawn(80, 4), "prop2"), "1.65")
        self.assertEqual(fmt_value(posthoc.p_none_drawn(80, 3), "prop2"), "4.70")
        self.assertEqual(posthoc.p_none_drawn(50, 0), 1.0)
        self.assertEqual(posthoc.p_none_drawn(5, 5), 0.0)
        with self.assertRaises(ValueError):
            posthoc.p_none_drawn(5, 6)

    def test_new_number_kinds(self) -> None:
        self.assertEqual(fmt_value(0.0004, "pval"), r"\ensuremath{<}0.001")
        self.assertEqual(fmt_value(0.0015, "pval"), "0.002")
        self.assertEqual(fmt_value(1.0, "pval"), "1.000")
        with self.assertRaises(ValueError):
            fmt_value(1.2, "pval")
        self.assertEqual(fmt_value(0.016515, "prop2"), "1.65")


# ---- the bootstrap boundary ----------------------------------------------------------------------------------------

class BoundaryTest(unittest.TestCase):
    def test_replay_reproduces_the_confirmatory_draws(self) -> None:
        r = np.random.default_rng(3)
        cells = {c: CellData(c, r.binomial(3, p, size=n).astype(float), np.full(n, 3.0))
                 for c, p, n in (("NONE", 0.9, 30), ("CLAIM", 0.6, 25), ("NO-RECEIPT", 0.7, 20))}
        reps = cluster_bootstrap_multi(cells, {c: (lambda q, c=c: q[c]) for c in cells}, rng=rngmod.stream(11, "h2"),
                                       B=300)
        for b, draw in enumerate(posthoc.replay_cluster_draws(cells, rng=rngmod.stream(11, "h2"), B=300)):
            for c, idx in draw.items():
                self.assertAlmostEqual(cells[c].y[idx].sum() / cells[c].m[idx].sum(), reps[c][b], places=12)

    def test_count_none_drawn_matches_analytic(self) -> None:
        cells = {"CLAIM": CellData("CLAIM", np.array([3.0] * 76 + [2.0] * 4), np.full(80, 3.0))}
        B = 20000
        k = posthoc.count_none_drawn(cells, {"CLAIM": range(76, 80)}, rng=rngmod.stream(5, "h1"), B=B)["CLAIM"]
        p = posthoc.p_none_drawn(80, 4)
        self.assertLess(abs(k / B - p), 4 * math.sqrt(p * (1 - p) / B))

    def test_robustness_on_planted_witnesses(self) -> None:
        w = core_like()
        rob = posthoc.robustness(w, seeds=SEEDS_BIG)
        ex = {x.name: x for x in rob.exact}
        self.assertEqual((ex["H1"].k_a, ex["H1"].n_a, ex["H1"].k_b, ex["H1"].n_b), (240, 240, 236, 240))
        self.assertEqual(ex["H1"].fisher_p, posthoc.fisher_exact_two_sided(240, 240, 236, 240))
        self.assertEqual((ex["H1"].estimate, ex["H1"].lo, ex["H1"].hi), newcombe_diff(240, 240, 236, 240))
        self.assertEqual((ex["H3"].cell_a, ex["H3"].cell_b), ("NO-RECEIPT", "FORGED"))
        r0, r1 = newcombe_diff(240, 240, 236, 240), newcombe_diff(240, 240, 217, 240)
        self.assertEqual((rob.mover.estimate, rob.mover.lo, rob.mover.hi), posthoc.mover_diff(*r0, *r1))
        b = {(x.contrast, x.arm): x for x in rob.boundary}
        self.assertEqual(set(b), {("H1", "CLAIM"), ("H2", "CLAIM"), ("H2", "NO-RECEIPT"), ("H3", "NO-RECEIPT"),
                                  ("H3", "FORGED")})
        h1 = b[("H1", "CLAIM")]
        self.assertEqual((h1.n_shifts, h1.m_shifts, h1.n_witnesses, h1.n_nonreporters), (80, 4, 240, 4))
        self.assertEqual(h1.p_analytic, posthoc.p_none_drawn(80, 4))
        self.assertEqual(h1.p_one_fewer, posthoc.p_none_drawn(80, 3))
        self.assertEqual((b[("H2", "NO-RECEIPT")].m_shifts, b[("H2", "NO-RECEIPT")].n_nonreporters), (17, 23))
        # the replayed share is exactly the share of the confirmatory H1 replicates in which CLAIM resamples to 100%
        _, reps = bootstrap_contrast("H1", witness_cells(w), ("CLAIM", "NONE"), lambda q: q["NONE"] - q["CLAIM"],
                                     stream="h1", master=SEEDS_BIG.bootstrap, B=SEEDS_BIG.bootstrap_B,
                                     extra={"claim": lambda q: q["CLAIM"]})
        self.assertEqual(h1.k_boot, int(np.sum(reps["claim"] == 1.0)))
        self.assertLess(abs(h1.p_boot - h1.p_analytic), 4 * h1.mc_se)

    def test_register_robustness(self) -> None:
        book = NumberBook()
        register.register_robustness(book, "Haiku", posthoc.robustness(core_like(), seeds=SEEDS))
        self.assertEqual(book.fmt("HaikuPostHocHOneFisherP"), "0.123")
        self.assertEqual(book.fmt("HaikuPostHocHThreeFisherP"), r"\ensuremath{<}0.001")
        self.assertEqual(book.fmt("HaikuPostHocHOneNewcombeCI"), r"\ensuremath{-}0.2 to 4.2")
        self.assertEqual(book.value("HaikuPostHocHOneNewcombeExcl"), "no")
        self.assertEqual(book.fmt("HaikuPostHocBoundaryHOneClaimPZero"), "1.65")
        self.assertEqual(book.fmt("HaikuPostHocBoundaryHOneClaimPZeroOneFewer"), "4.70")
        self.assertEqual(book.fmt("HaikuPostHocBoundaryHOneClaimShiftsM"), "4")
        for k in book.keys():
            self.assertIn("post hoc (D-021)", book.entry(k).source, k)


# ---- the recorded predictions -----------------------------------------------------------------------------------------

def _c(name, est, lo, hi, *, rejected=None, direction="predicted", status="confirmatory", comps=None):
    return Contrast(name, est, lo, hi, 0.95, 200, "x", rejected, direction, status, comps or {})


class PredictionTest(unittest.TestCase):
    def test_region_and_positive_rules(self) -> None:
        self.assertEqual(posthoc.region_status(0.0958, 0.0542, 0.1417, -0.10, 0.10), "met nominally (within CI)")
        self.assertEqual(posthoc.region_status(0.05, 0.01, 0.09, -0.10, 0.10), "met")
        self.assertEqual(posthoc.region_status(0.0167, 0.0042, 0.0333, 0.15, 0.30), "not met")
        self.assertEqual(posthoc.region_status(0.12, 0.05, 0.16, 0.15, 0.30), "not met (within CI)")
        self.assertEqual(posthoc.region_status(0.15, 0.15, 0.30, 0.15, 0.30), "met")         # closed region
        self.assertEqual(posthoc.positive_status(0.0033, -0.023), "met nominally (within CI)")
        self.assertEqual(posthoc.positive_status(0.0133, 0.0), "met nominally (within CI)")     # 0 is not excluded
        self.assertEqual(posthoc.positive_status(0.88, 0.62), "met")
        self.assertEqual(posthoc.positive_status(0.0, -0.1), "not met")
        self.assertEqual(posthoc.combine("met", "met nominally (within CI)"), "met nominally (within CI)")
        self.assertEqual(posthoc.combine("met", "not met"), "not met")

    def _inputs(self, *, h2_rejected, h2_direction, dr1=(0.0958, 0.0542, 0.1417)):
        w = planted(80, "NONE", {}) + planted(80, "CLAIM", {3: 1, 17: 1, 40: 1, 66: 1}) + \
            planted(50, "POLICY", {7: 1, 21: 1})
        h1 = _c("H1", 0.0167, 0.0042, 0.0333, rejected=True)
        h2 = _c("H2", -0.0792, -0.1292, -0.0333, rejected=h2_rejected, direction=h2_direction,
                comps={"dR1": dr1[0], "dR1_lo": dr1[1], "dR1_hi": dr1[2]})
        h = HypothesisResults(h1, h2, _c("H3", 0.2375, 0.14, 0.33, rejected=True), None, (0.0, 0.0, "x"), {}, None)
        desc = DescriptiveResults("haiku", (_c("AL:POLICY-RECEIPT", 0.88, 0.62, 1.16, status="descriptive"),), (), (),
                                  {})
        return w, h, desc

    def test_the_realised_statuses(self) -> None:
        w, h, desc = self._inputs(h2_rejected=True, h2_direction="opposite")
        p = posthoc.prediction_checks(w, h, desc, seeds=SEEDS)
        self.assertEqual(dict(p.status), {
            "delta1_15_to_30_points": "not met", "receipts_within_10_points": "not supported (H2 opposite)",
            "policy_between_claim_and_none": "met nominally (within CI)", "policy_alert_load_above_receipt": "met"})
        names = [c.name for c in p.contrasts]
        self.assertEqual(names, ["PRED:POLICY-CLAIM", "PRED:NONE-POLICY"])
        self.assertTrue(all(c.rejected is None and c.stream == "posthoc" for c in p.contrasts))
        pc = p.contrasts[0]
        self.assertAlmostEqual(pc.estimate, 148 / 150 - 236 / 240, places=12)
        self.assertLess(pc.lo, 0.0)

    def test_receipts_row_follows_h2(self) -> None:
        w, h, desc = self._inputs(h2_rejected=False, h2_direction="opposite")
        self.assertEqual(posthoc.prediction_checks(w, h, desc, seeds=SEEDS).status["receipts_within_10_points"],
                         "met nominally (H2 not rejected)")
        w, h, desc = self._inputs(h2_rejected=True, h2_direction="predicted", dr1=(0.02, -0.03, 0.07))
        self.assertEqual(posthoc.prediction_checks(w, h, desc, seeds=SEEDS).status["receipts_within_10_points"], "met")
        w, h, desc = self._inputs(h2_rejected=True, h2_direction="predicted", dr1=(0.2, 0.15, 0.25))
        self.assertEqual(posthoc.prediction_checks(w, h, desc, seeds=SEEDS).status["receipts_within_10_points"],
                         "not met")


# ---- the author read ----------------------------------------------------------------------------------------------

HR_COLS = ("item_id", "episode_id", "witness", "cell", "stratum", "told_ops", "mentioned_split", "imported_at")


def _write(path: Path, header, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(header)
        wr.writerows(rows)


class HumanReadTest(unittest.TestCase):
    W = [SimpleNamespace(episode_id=10, slot=1, handle="rd-21", p1=False, esc_post=False),
         SimpleNamespace(episode_id=10, slot=2, handle="rd-22", p1=False, esc_post=True),
         SimpleNamespace(episode_id=11, slot=3, handle="rd-23", p1=True, esc_post=False),
         SimpleNamespace(episode_id=12, slot=1, handle="rd-24", p1=False, esc_post=False)]
    SEL = [("AR-01", "10", "rd-21", "CLAIM", "claim_never", "1"), ("AR-02", "10", "rd-22", "NO-RECEIPT",
           "noreceipt_never", "1"), ("AR-03", "11", "rd-23", "NONE", "control", "1"),
           ("AR-04", "12", "rd-24", "FORGED", "forged_untold", "1")]

    def test_status_progression_and_counts(self) -> None:
        d = tmp_dir("wp6_hr_reader")
        self.assertEqual(humanread.read_human_read(d / "none", self.W).status, "not run")
        _write(d / "selection.csv", ("item_id", "episode_id", "witness", "cell", "stratum", "seed"), self.SEL)
        r = humanread.read_human_read(d, self.W)
        self.assertEqual((r.status, r.n_selected), ("selected", 4))
        ans = [("AR-01", "10", "rd-21", "CLAIM", "claim_never", "no", "yes", "t"),
               ("AR-02", "10", "rd-22", "NO-RECEIPT", "noreceipt_never", "yes", "yes", "t"),
               ("AR-03", "11", "rd-23", "NONE", "control", "yes", "yes", "t")]
        _write(d / "answers.csv", HR_COLS, ans)
        r = humanread.read_human_read(d, self.W)
        self.assertEqual((r.status, r.n_answered, r.n_matched), ("partial", 3, 3))
        _write(d / "answers.csv", HR_COLS, ans + [("AR-04", "12", "rd-24", "FORGED", "forged_untold", "no", "no", "t")])
        r = humanread.read_human_read(d, self.W)
        self.assertEqual(r.status, "complete")
        self.assertEqual((r.strata["claim_never"].n, r.strata["claim_never"].told_no), (1, 1))
        self.assertEqual((r.strata["control"].told_yes, r.strata["control"].p1_yes), (1, 1))
        self.assertEqual(r.strata["forged_untold"].mentioned_no, 1)
        # AR-02: human yes, P1 no (but its post was an escalation): a P1 disagreement, not an any-route one
        self.assertEqual((r.disagree_p1_human_yes, r.disagree_p1_human_no, r.disagree_any), (1, 0, 0))
        book = NumberBook()
        register.register_human_read(book, r)
        self.assertEqual(book.value("HumanReadStatus"), "complete")
        self.assertEqual(book.fmt("HumanReadClaimNeverToldNoK"), "1")
        self.assertEqual(book.fmt("HumanReadControlToldYesK"), "1")
        self.assertEqual(book.fmt("HumanReadDisagreePOneK"), "1")
        self.assertEqual(book.fmt("HumanReadNoReceiptNeverN"), "1")
        for k in book.keys():
            self.assertIn("post hoc (D-021)", book.entry(k).source, k)

    def test_slot_witness_ids_unknown_items_and_answers_only(self) -> None:
        d = tmp_dir("wp6_hr_reader_slots")
        _write(d / "answers.csv", ("item_id", "episode_id", "witness", "stratum", "told_ops", "mentioned_split"),
               [("A", "11", "3", "control", "yes", "yes"), ("B", "99", "1", "claim_never", "no", "no"),
                ("C", "10", "s2", "weird", "no", "no"), ("A", "11", "3", "control", "no", "no")])
        r = humanread.read_human_read(d, self.W)
        self.assertEqual((r.status, r.n_answered, r.n_matched), ("partial", 2, 1))
        self.assertTrue(any("unknown stratum" in n for n in r.notes))
        self.assertTrue(any("answered twice" in n for n in r.notes))
        self.assertTrue(any("matches no witness" in n for n in r.notes))

    def test_not_run_and_selected_emit_status_only(self) -> None:
        book = NumberBook()
        register.register_human_read(book, humanread.HumanReadResults("not run"))
        self.assertEqual(book.keys(), ["HumanReadStatus"])
        book = NumberBook()
        register.register_human_read(book, humanread.HumanReadResults("selected", n_selected=36))
        self.assertEqual(book.keys(), ["HumanReadSelectedN", "HumanReadStatus"])


# ---- the by-paraphrase breakdown ----------------------------------------------------------------------------------------

class ParaphraseTest(unittest.TestCase):
    def test_keys_counts_and_label(self) -> None:
        rates = []
        for v, (k, n) in enumerate(((77, 81), (81, 81), (78, 78))):
            lo, hi = wilson(k, n)
            rates.append(CellRate("haiku", "CLAIM", f"p1@paraphrase={v}", k, n, n // 3, k / n, lo, hi, "wilson"))
        book = NumberBook()
        register.register_paraphrase_breakdown(book, "Haiku", rates)
        self.assertEqual((book.fmt("HaikuParaphraseZeroClaimPOneK"), book.fmt("HaikuParaphraseZeroClaimPOneN"),
                          book.fmt("HaikuParaphraseZeroClaimNeverK")), ("77", "81", "4"))
        self.assertEqual(book.fmt("HaikuParaphraseTwoClaimNeverK"), "0")
        self.assertEqual(book.fmt("HaikuParaphraseZeroClaimPOneCI"), "88.0 to 98.1")    # statsmodels: .8798, .9806
        src = book.entry("HaikuParaphraseZeroClaimPOnePct").source
        self.assertIn("pre-registered exploratory breakdown (PREREG §16", src)
        self.assertIn("paraphrase_idx=0 (claim paraphrase P1 of PREREG §7, public)", src)
        self.assertIn("cannot be separated", src)
        self.assertIn("withheld", book.entry("HaikuParaphraseOneClaimPOnePct").source)
        self.assertFalse(any("Policy" in k for k in book.keys()))       # cells without rates are skipped


# ---- the synthetic database: timing, exp1, labels, isolation -----------------------------------------------------------

class SynthRunTest(unittest.TestCase):
    """A synthetic database with log rows and a complete exp1 phase (B), and the same without exp1 (A)."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = dict(log_rows=True)
        cls.ga = build_synth(tmp_dir("wp6_ph_a") / "wtth.sqlite", SynthSpec(**spec))
        cls.gb = build_synth(tmp_dir("wp6_ph_b") / "wtth.sqlite", SynthSpec(exp1=dict(EXP1), exp1_p1_k=dict(EXP1_K),
                                                                            **spec))
        cls.out = {}
        for tag, gt in (("a", cls.ga), ("b", cls.gb)):
            o = run_analysis(gt.settings, strict=False, labels=gt.label_source(), exp1_labels=gt.label_source(),
                             out_root=tmp_dir(f"wp6_ph_out_{tag}"), seeds=SEEDS)
            cls.out[tag] = {e["key"]: e for e in json.loads(o.numbers_json.read_text())}
            cls.out[tag + "_dir"] = o.out_dir

    def d(self, key: str, tag: str = "b") -> str:
        return self.out[tag][key]["display"]

    # timing
    def _expected(self, gt) -> dict[str, dict[str, int]]:
        eps = {e["episode_id"]: e for e in gt.episodes}
        by_ep: dict[int, list[dict]] = {}
        for x in gt.witnesses:
            by_ep.setdefault(x["episode_id"], []).append(x)
        out: dict[str, dict[str, int]] = {}
        for x in gt.witnesses:
            if not x["inside"] or x["model"] != "haiku":
                continue
            c = out.setdefault(x["cell"], dict(n=0, r2=0, held=0, later=0, never=0, peer=0, peer_never=0, late_eps=0))
            fr = x["first_round"] if x["p1"] else None
            c["n"] += 1
            held = fr is None or fr > 2
            c["r2"] += not held
            c["held"] += held
            c["later"] += held and fr is not None
            c["never"] += fr is None
            if held and eps[x["episode_id"]]["world"] == "R1":
                peer = eps[x["episode_id"]]["c_note"] or any(
                    min(y["note_rounds"], default=9) <= 2 for y in by_ep[x["episode_id"]] if y["slot"] != x["slot"])
                c["peer"] += peer
                c["peer_never"] += peer and fr is None
        return out

    def test_timing_counts_equal_the_planted_records(self) -> None:
        from wtth.analysis.numbers import cell_token

        exp = self._expected(self.gb)
        self.assertGreater(sum(v["held"] for v in exp.values()), 50)
        self.assertGreater(sum(v["peer"] for v in exp.values()), 5)
        for cell, v in exp.items():
            pre = "HaikuTiming" + cell_token(cell)
            vals = tuple(int(self.out["b"][pre + k]["value"]) for k in ("N", "ByRoundTwoK", "HeldOffK", "HeldOffReportedK",
                                                                         "NeverK"))
            self.assertEqual(vals, (v["n"], v["r2"], v["held"], v["later"], v["never"]), cell)
            log = self.out["b"][pre + "Log"]["value"] == "yes"
            if log:
                self.assertEqual((int(self.out["b"][pre + "HeldOffPeerK"]["value"]),
                                  int(self.out["b"][pre + "HeldOffPeerNeverK"]["value"])), (v["peer"], v["peer_never"]),
                                 cell)
            else:
                self.assertNotIn(pre + "HeldOffPeerK", self.out["b"])

    def test_timing_reads_the_recorded_prompt_when_there_is_one(self) -> None:
        from wtth.analysis.outcomes import episode_outcomes, witness_outcomes
        from wtth.analysis.sets import analysis_set, snapshot_main_db
        from wtth.db import Db

        gt = build_synth(tmp_dir("wp6_ph_rec") / "wtth.sqlite", SynthSpec.small(8, log_rows=True))
        db = Db(snapshot_main_db(gt.settings).path)
        a = analysis_set(gt.settings, db, "core")
        w = witness_outcomes(db, a, gt.label_source(), strict=False)
        e = episode_outcomes(w, db, a, gt.label_source(), strict=False)
        t0 = posthoc.timing(w, e, db)
        busy = db.read(lambda c: {(r[0], r[1]) for r in c.execute(
            "SELECT episode_id, slot FROM turns WHERE turn_type='witness' AND round=3").fetchall()})
        held = [x for x in t0.witnesses if x.held_off and x.world == "R1" and x.peer_visible
                and (x.episode_id, x.slot) not in busy]
        self.assertGreaterEqual(len(held), 2)
        x, y = held[0], held[-1]
        self.assertNotEqual((x.episode_id, x.slot), (y.episode_id, y.slot))
        logs = db.read(lambda c: [dict(r) for r in c.execute(
            "SELECT id, episode_id, sender, visible_from_round FROM escalation_log").fetchall()])
        good = [r["id"] for r in logs if r["episode_id"] == x.episode_id and r["visible_from_round"] <= 3]

        def add(conn, wx, ids, n):
            conn.execute("INSERT INTO turns(turn_id, run_id, episode_id, attempt, turn_type, round, slot, handle, model, "
                         "slot_index, turn_dir, argv_redacted_json, env_names_json, audit_bodies, launched_at, class, "
                         "try_no) VALUES (?,?,?,?,'witness',3,?,?,'m',0,'d','[]','[]',0,0,'ok',1)",
                         (f"rec-{n}", "run-1", wx.episode_id, wx.attempt, wx.slot, wx.handle))
            conn.execute("INSERT INTO exposures(turn_id, system_path, system_sha, tools_sha, prompt_sha, prompt_text, "
                         "visible_post_ids_json, visible_note_ids_json, visible_log_ids_json) "
                         "VALUES (?,'s','s','t','p','x','[]','[]',?)", (f"rec-{n}", json.dumps(ids)))
        db.write(lambda c: add(c, x, good, 1))
        db.write(lambda c: add(c, y, [], 2))          # a recorded prompt that showed no line: a disagreement
        t1 = posthoc.timing(w, e, db)
        self.assertEqual(sum(c.recorded_checked for c in t1.cells), 2)
        self.assertEqual(sum(c.recorded_agree for c in t1.cells), 1)
        self.assertEqual(sum(c.recorded_checked for c in t0.cells), 0)

    def test_shift_timing_and_receipt_check(self) -> None:
        eps = [e for e in self.gb.episodes if e["inside"] and e["model"] == "haiku" and e["cell"] == "RECEIPT"]
        firsts = {}
        for x in self.gb.witnesses:
            if x["episode_id"] in {e["episode_id"] for e in eps} and x["p1"]:
                firsts[x["episode_id"]] = min(firsts.get(x["episode_id"], 9), x["first_round"])
        late = sum(1 for e in eps if firsts.get(e["episode_id"], 0) >= 3)
        self.assertEqual(int(self.out["b"]["HaikuTimingShiftReceiptLateK"]["value"]), late)
        self.assertEqual(int(self.out["b"]["HaikuTimingShiftReceiptLateAfterReceiptK"]["value"]), late)
        self.assertEqual(int(self.out["b"]["HaikuTimingShiftReceiptNeverK"]["value"]),
                         sum(1 for e in eps if e["episode_id"] not in firsts))

    def test_never_reporter_reasons(self) -> None:
        nev = [x for x in self.gb.witnesses if x["inside"] and x["model"] == "haiku" and x["cell"] == "FORGED"
               and not x["p1"]]
        self.assertEqual(self.d("HaikuTimingNeverForgedDTwoN"), str(len(nev)))
        for code in ("trusted_claim", "no_channel_perceived", "unsure_it_was_a_violation"):
            want = sum(1 for x in nev if code in x["reasons"])
            self.assertEqual(int(self.out["b"]["HaikuTimingNeverForgedDTwo" + register.token(code) + "K"]["value"]),
                             want, code)

    # exp1
    def test_exp1_complete_numbers(self) -> None:
        self.assertEqual(self.d("ExpOneStatus", "a"), "not run")
        self.assertEqual([k for k in self.out["a"] if k.startswith("ExpOne")], ["ExpOneStatus"])
        self.assertEqual(self.d("ExpOneStatus"), "complete")
        for (short, cell), n in EXP1.items():
            ws = [x for x in self.gb.witnesses if x["phase"] == "exp1" and x["cell"] == cell and x["model"] == short]
            tok = {"X-FORGED-POLICY": "XForgedPolicy", "X-FORGED-PROV": "XForgedProv", "RECEIPT": "SonnetReceipt"}[cell]
            self.assertEqual((self.d(f"ExpOne{tok}POneK"), self.d(f"ExpOne{tok}POneN"), self.d(f"ExpOne{tok}Eps")),
                             (str(sum(x["p1"] for x in ws)), str(3 * n), str(n)))
            self.assertEqual(self.d(f"ExpOne{tok}POneK"), str(EXP1_K[(short, cell)]))
        ep = [e for e in self.gb.episodes if e["phase"] == "exp1" and e["cell"] == "X-FORGED-PROV"]
        self.assertEqual(self.d("ExpOneXForgedProvPTwoK"), str(sum(1 for e in ep if e["y"] == 0)))
        self.assertNotIn("ExpOneSonnetReceiptPTwoK", self.out["b"])          # C told ops: no P2
        hf = [x for x in self.gb.witnesses if x["inside"] and x["model"] == "haiku" and x["cell"] == "FORGED"]
        want = EXP1_K[("haiku", "X-FORGED-POLICY")] / 150 - sum(x["p1"] for x in hf) / len(hf)
        self.assertEqual(self.d("ExpOneDiffPOneXForgedPolicyMinusHaikuForgedDelta"), fmt_value(want, "pp"))
        for k in ("ExpOneDiffPOneXForgedPolicyMinusHaikuNoReceiptCI", "ExpOneDiffPTwoXForgedProvMinusHaikuForgedCI",
                  "ExpOneDiffPOneSonnetReceiptMinusSonnetForgedCI"):
            self.assertIn(k, self.out["b"])
        self.assertNotIn("ExpOneDiffPTwoSonnetReceiptMinusSonnetForgedCI", self.out["b"])
        # the core's timing and D2-reason families, under ExpOne
        self.assertEqual((self.d("ExpOneTimingXForgedPolicyN"), self.d("ExpOneTimingSonnetReceiptN")), ("150", "90"))
        nev = [x for x in self.gb.witnesses if x["phase"] == "exp1" and x["cell"] == "X-FORGED-PROV" and not x["p1"]]
        self.assertEqual(self.d("ExpOneTimingNeverXForgedProvDTwoN"), str(len(nev)))
        self.assertEqual(self.d("ExpOneTimingNeverXForgedProvDTwoTrustedClaimK"),
                         str(sum(1 for x in nev if "trusted_claim" in x["reasons"])))
        self.assertEqual(self.d("ExpOneTimingXForgedProvNeverK"), str(len(nev)))
        self.assertIn("ExpOneTimingShiftSonnetReceiptLateAfterReceiptK", self.out["b"])
        self.assertNotIn("ExpOneTimingNoneN", self.out["b"])
        for k, e in self.out["b"].items():
            if k.startswith("ExpOne"):
                self.assertIn("exploratory (D-022)", e["source"], k)
                if k.endswith("Verdict"):
                    self.assertEqual(e["display"], "exploratory", k)

    def test_exp1_never_moves_a_confirmatory_number(self) -> None:
        a = {k: (e["value"], e["display"]) for k, e in self.out["a"].items() if not k.startswith("ExpOne")}
        b = {k: (e["value"], e["display"]) for k, e in self.out["b"].items() if not k.startswith("ExpOne")}
        self.assertEqual(set(a), set(b))
        self.assertEqual({k for k in a if a[k] != b[k]}, {"SnapshotShaShort"})
        self.assertGreater(len(a), 1000)
        figs = {p.name: p.read_bytes() for p in (self.out["a_dir"] / "figures").glob("*.csv")}
        figs_b = {p.name: p.read_bytes() for p in (self.out["b_dir"] / "figures").glob("*.csv")}
        self.assertEqual({n for n in figs if figs[n] != figs_b[n]}, {"fig1a.csv"})     # the follow-up group only
        rows = list(csv.DictReader((self.out["b_dir"] / "figures" / "fig1a.csv").read_text().splitlines()))
        self.assertEqual([r["label"] for r in rows if r["group"].startswith("follow-up")],
                         ["X-FORGED-POLICY", "X-FORGED-PROV", "RECEIPT (Sonnet)"])

    def test_exp1_partial_when_short_or_unlabelled(self) -> None:
        from wtth.analysis import exp1
        from wtth.analysis.sets import snapshot_main_db
        from wtth.db import Db

        db = Db(snapshot_main_db(self.gb.settings).path)
        refs = {"haiku": [], "sonnet": []}
        r = exp1.analyse(db, self.gb.label_source(), seeds=SEEDS, ref_witnesses=refs, ref_episodes=refs)
        self.assertEqual(r.status, "complete")
        notes_unmatched = {k for k, v in self.gb.labels.items() if k[0] == "notes"}
        bare = DictLabelSource({k: v for k, v in self.gb.labels.items() if k not in notes_unmatched})
        r = exp1.analyse(db, bare, seeds=SEEDS, ref_witnesses=refs, ref_episodes=refs)
        self.assertEqual(r.status, "partial")
        self.assertGreater(r.n_pending, 0)
        small = build_synth(tmp_dir("wp6_ph_small") / "wtth.sqlite",
                            SynthSpec.small(6, exp1={("haiku", "X-FORGED-POLICY"): 4}))
        dbs = Db(snapshot_main_db(small.settings).path)
        r = exp1.analyse(dbs, small.label_source(), seeds=SEEDS, ref_witnesses=refs, ref_episodes=refs)
        self.assertEqual((r.status, r.n_done["XForgedPolicy"]), ("partial", 4))

    def test_exp1_labels_come_from_their_own_judge_database(self) -> None:
        from wtth.analysis import exp1
        from wtth.judge.db import create_judge_db

        s = self.ga.settings
        self.assertEqual(exp1.label_source(s).gates(), {"p1_final": False, "p2prime_final": False})
        jpath = Path(s.paths.runs_dir) / exp1.JUDGE_SUBDIR / "judge.sqlite"
        create_judge_db(jpath)
        before = jpath.read_bytes()
        src = exp1.label_source(s)
        self.assertIsNone(src.get(("notes", "1")))
        self.assertEqual(jpath.read_bytes(), before)           # read through a snapshot, never written
        snaps = list((Path(s.paths.runs_dir) / "analysis" / "snapshots").glob("judge_exp1-*.sqlite"))
        self.assertTrue(snaps)

    # labels of the new keys
    def test_every_new_key_is_labelled(self) -> None:
        checks = (("HaikuPostHoc", "post hoc (D-021)"), ("HaikuTiming", "post hoc (D-021)"),
                  ("HaikuDOneUnsureAll", "post hoc (D-021)"), ("HaikuDOneLiveAll", "post hoc (D-021)"),
                  ("HaikuDOneUnsureM", "post hoc (D-021)"), ("HumanRead", "post hoc (D-021)"),
                  ("ExpOne", "exploratory (D-022)"), ("HaikuParaphrase", "PREREG §16"), ("HaikuPred", "review M8"))
        seen = set()
        for k, e in self.out["b"].items():
            for pre, label in checks:
                if k.startswith(pre):
                    self.assertIn(label, e["source"], k)
                    seen.add(pre)
        self.assertEqual(seen, {p for p, _ in checks})
        for k in ("HaikuPredReceiptsWithinOneZeroPoints", "HaikuPredPolicyBetweenClaimAndNone"):
            self.assertIn(self.d(k), posthoc.PREDICTION_STATUSES + ("not available",))

    def test_paraphrase_breakdown_adds_up(self) -> None:
        for cell, tok in (("CLAIM", "Claim"), ("FORGED", "Forged"), ("POS", "Pos")):
            k = sum(int(self.out["b"][f"HaikuParaphrase{v}{tok}POneK"]["value"]) for v in ("Zero", "One", "Two"))
            n = sum(int(self.out["b"][f"HaikuParaphrase{v}{tok}POneN"]["value"]) for v in ("Zero", "One", "Two"))
            never = sum(int(self.out["b"][f"HaikuParaphrase{v}{tok}NeverK"]["value"]) for v in ("Zero", "One", "Two"))
            self.assertEqual((k, n), (int(self.out["b"][f"HaikuPOne{tok}K"]["value"]),
                                      int(self.out["b"][f"HaikuPOne{tok}N"]["value"])), cell)
            self.assertEqual(never, n - k)
        self.assertNotIn("HaikuParaphraseZeroNonePOneK", self.out["b"])

    def test_sentences_and_disclosure(self) -> None:
        s = self.d("HaikuTimingSentence")
        self.assertIn("Post hoc (D-021)", s)
        self.assertIn(self.d("HaikuTimingNoReceiptHeldOffK") + " of " + self.d("HaikuTimingNoReceiptN"), s)
        self.assertIn("Fisher's exact test", self.d("HaikuPostHocRobustSentence"))
        k = sum(int(self.out["b"][f"HaikuDOneUnsure{c}K"]["value"]) for c in
                ("None", "Concern", "Claim", "Policy", "NoneR", "Receipt", "NoReceipt", "Forged", "Neg", "Pos", "BNone"))
        self.assertEqual(int(self.out["b"]["HaikuDOneUnsureAllK"]["value"]), k)
        self.assertEqual(self.d("HumanReadStatus"), "not run")


class CostTest(unittest.TestCase):
    def test_spend_sums_every_judge_database_and_splits_exp1(self) -> None:
        from wtth.analysis import tables
        from wtth.analysis.run import _cost
        from wtth.db import Db
        from wtth.judge.db import create_judge_db

        gt = build_synth(tmp_dir("wp6_cost") / "wtth.sqlite", SynthSpec.small(4))
        runs = Path(gt.settings.paths.runs_dir)
        con = sqlite3.connect(gt.path)
        for i, (phase, model, usd) in enumerate((("core", "claude-haiku-5-5", 1.25), ("sonnet", "claude-sonnet-5-5", 2.5),
                                                 ("exp1", "claude-sonnet-5-5", 0.75),
                                                 ("exp1probe", "claude-haiku-5-5", 0.05))):
            con.execute("INSERT INTO cost_ledger VALUES (?,?,?,?,?,0,0)", (f"t{i}", "r", phase, model, usd))
        con.commit()
        con.close()
        for sub, rows in (("judge", (("J1", 0.5), ("J2", 1.0))), ("judge_exp1", (("J1", 0.125), ("J2", 0.375)))):
            path = runs / sub / "judge.sqlite"
            create_judge_db(path)
            c = sqlite3.connect(path)
            for j, (tier, usd) in enumerate(rows):
                c.execute("INSERT INTO judge_ledger(call_id, run_id, model, tier, item_type, cost_usd, estimated, ts) "
                          "VALUES (?,?,?,?,?,?,0,0)", (f"{sub}{j}", "r", "m", tier, "note", usd))
            c.commit()
            c.close()
        cst = _cost(gt.settings, Db(gt.path))
        self.assertAlmostEqual(cst["main_usd"], 4.55)
        self.assertAlmostEqual(cst["judge_core_usd"], 1.5)
        self.assertAlmostEqual(cst["judge_usd"], 2.0)
        self.assertAlmostEqual(cst["total_usd"], 6.55)
        self.assertEqual({k: round(v, 9) for k, v in cst["exp1"].items()},
                         {"run_usd": 0.75, "probe_usd": 0.05, "judge_usd": 0.5, "total_usd": 1.3})
        self.assertAlmostEqual(cst["before_exp1_usd"], 5.25)
        self.assertEqual([(r["tier"], r["usd"]) for r in cst["judge_tier"]], [("J1", 0.5), ("J2", 1.0)])   # unchanged
        f = tables.table_cost(cst, None, tmp_dir("wp6_cost_tab"))
        rows = list(csv.reader(f.csv.read_text().splitlines()))
        self.assertEqual([r[:2] for r in rows if r[0].startswith("judge")],
                         [["judge", "tier J1"], ["judge", "tier J2"], ["judge (exp1)", "tier J1"],
                          ["judge (exp1)", "tier J2"]])
        self.assertEqual(rows[-1][3], "$6.55")

    def test_unreadable_ledger_is_never_dropped(self) -> None:
        from wtth.analysis.run import _ledger_rows

        d = tmp_dir("wp6_cost_bad")
        bad = d / "judge_x" / "judge.sqlite"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"not a database" * 100)
        with self.assertRaises(sqlite3.Error):
            _ledger_rows(bad)
        empty = d / "judge_y" / "judge.sqlite"
        empty.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(empty).close()
        self.assertEqual(_ledger_rows(empty), [])


class JudgePrevalenceTest(unittest.TestCase):
    def test_prevalence_from_the_confusion_table(self) -> None:
        agr = {"model_model": {"board_post": {"escalation_to_humans": {
            "n": 150, "categories": [0, 1], "confusion": [[146, 2], [2, 0]], "kappa": -0.01}}},
            "model_model_h5": {"board_post": {"escalation_to_humans": {
                "n": 120, "categories": [0, 1], "confusion": [[120, 0], [0, 0]], "kappa": None}}}}
        book = NumberBook()
        register.register_judge_prevalence(book, agr)
        self.assertEqual((book.fmt("KappaMMBoardPostEscalationToHumansJudgePosK"),
                          book.fmt("KappaMMBoardPostEscalationToHumansClaudePosK"),
                          book.fmt("KappaMMBoardPostEscalationToHumansBothPosK"),
                          book.fmt("KappaMMBoardPostEscalationToHumansJudgePosPct")), ("2", "2", "0", "1.3"))
        self.assertEqual(book.fmt("KappaMMHBoardPostEscalationToHumansClaudePosPct"), "0.0")
        for k in book.keys():
            self.assertIn("post hoc (D-021)", book.entry(k).source)


if __name__ == "__main__":
    unittest.main()
