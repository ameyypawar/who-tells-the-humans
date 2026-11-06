"""WP6-C: intervals, streams and the cluster bootstrap (plan 47 3.1 WP6-C tests 2-3)."""
from __future__ import annotations

import unittest

import numpy as np
from statsmodels.stats.proportion import confint_proportions_2indep, proportion_confint

from wtth.analysis import rng as rngmod
from wtth.analysis.bootstrap import CellData, cluster_bootstrap, cluster_bootstrap_multi, seed_paired_bootstrap
from wtth.analysis.intervals import newcombe_diff, percentile_interval, rejects, wilson


class IntervalsTest(unittest.TestCase):
    def test_wilson_matches_statsmodels(self) -> None:
        for n in (1, 2, 5, 20, 48, 80, 150, 240):
            for k in range(0, n + 1, max(1, n // 12)):
                for kk in {k, 0, n}:
                    lo, hi = wilson(kk, n)
                    slo, shi = proportion_confint(kk, n, alpha=0.05, method="wilson")
                    self.assertAlmostEqual(lo, slo, places=9, msg=f"{kk}/{n}")
                    self.assertAlmostEqual(hi, shi, places=9, msg=f"{kk}/{n}")

    def test_wilson_edges(self) -> None:
        self.assertIsNone(wilson(0, 0))
        self.assertEqual(wilson(0, 10)[0], 0.0)
        self.assertEqual(wilson(10, 10)[1], 1.0)
        with self.assertRaises(ValueError):
            wilson(5, 4)

    def test_newcombe_matches_statsmodels(self) -> None:
        cases = [(0, 10, 0, 10), (10, 10, 0, 10), (0, 10, 10, 10), (10, 10, 10, 10), (3, 20, 9, 25), (40, 80, 22, 80),
                 (240, 240, 100, 150), (1, 50, 0, 50), (37, 50, 0, 80), (0, 1, 1, 1)]
        for k1, n1, k2, n2 in cases:
            d, lo, hi = newcombe_diff(k1, n1, k2, n2)
            res = confint_proportions_2indep(k1, n1, k2, n2, method="newcomb", compare="diff")
            self.assertAlmostEqual(d, k1 / n1 - k2 / n2, places=12)
            self.assertAlmostEqual(lo, res[0], places=8, msg=str((k1, n1, k2, n2)))
            self.assertAlmostEqual(hi, res[1], places=8, msg=str((k1, n1, k2, n2)))

    def test_newcombe_undefined(self) -> None:
        self.assertIsNone(newcombe_diff(0, 0, 1, 2))
        self.assertIsNone(newcombe_diff(1, 2, 0, 0))

    def test_rejects_is_strict(self) -> None:
        self.assertFalse(rejects(0.0, 0.3))
        self.assertFalse(rejects(-0.3, 0.0))
        self.assertTrue(rejects(1e-12, 0.3))
        self.assertTrue(rejects(-0.3, -1e-12))
        self.assertFalse(rejects(-0.1, 0.1))

    def test_percentile_interval(self) -> None:
        v = np.arange(1001, dtype=float)
        v[3] = np.nan
        lo, hi = percentile_interval(v)
        exp = np.percentile(np.delete(v, 3), [2.5, 97.5], method="linear")
        self.assertEqual((lo, hi), (float(exp[0]), float(exp[1])))
        lo90, hi90 = percentile_interval(v, 0.90)
        exp = np.percentile(np.delete(v, 3), [5, 95], method="linear")
        self.assertEqual((lo90, hi90), (float(exp[0]), float(exp[1])))
        self.assertTrue(np.isnan(percentile_interval(np.array([np.nan]))[0]))


class StreamsTest(unittest.TestCase):
    def test_first_three_are_the_spawned_children(self) -> None:
        for name, i in (("h1", 0), ("h2", 1), ("h3", 2), ("misc", 13)):
            child = np.random.SeedSequence(777).spawn(i + 1)[i]
            want = np.random.Generator(np.random.PCG64(child)).integers(0, 10 ** 9, size=5)
            got = rngmod.stream(777, name).integers(0, 10 ** 9, size=5)
            self.assertEqual(want.tolist(), got.tolist())

    def test_streams_distinct_and_unknown_name(self) -> None:
        draws = {n: tuple(rngmod.stream(5, n).integers(0, 10 ** 9, size=3)) for n in rngmod.STREAMS}
        self.assertEqual(len(set(draws.values())), len(rngmod.STREAMS))
        self.assertEqual(sorted(rngmod.STREAMS.values()), list(range(16)))
        # the post-hoc (D-021) and exp1 (D-022) streams were appended; the 14 streams of the strict analysis keep theirs
        frozen = ("h1", "h2", "h3", "h2_p2", "alert_load", "concern", "descriptive", "p1_any", "attempts", "test_aware",
                  "kappa", "seed_paired", "sonnet", "misc")
        self.assertEqual([rngmod.STREAMS[n] for n in frozen], list(range(14)))
        self.assertEqual((rngmod.STREAMS["posthoc"], rngmod.STREAMS["exp1"]), (14, 15))
        with self.assertRaises(KeyError):
            rngmod.stream(1, "nope")


def _cells(pa: float, pb: float, n: int = 80, seed: int = 3) -> dict[str, CellData]:
    r = np.random.default_rng(seed)
    return {"NONE": CellData("NONE", r.binomial(3, pa, size=n).astype(float), np.full(n, 3.0)),
            "CLAIM": CellData("CLAIM", r.binomial(3, pb, size=n).astype(float), np.full(n, 3.0))}


def d1(rates):
    return rates["NONE"] - rates["CLAIM"]


class BootstrapTest(unittest.TestCase):
    def test_same_seed_identical(self) -> None:
        cells = _cells(0.7, 0.4)
        a = cluster_bootstrap(cells, d1, rng=rngmod.stream(9, "h1"), B=300)
        b = cluster_bootstrap(cells, d1, rng=rngmod.stream(9, "h1"), B=300)
        self.assertTrue(np.array_equal(a, b))
        c = cluster_bootstrap(cells, d1, rng=rngmod.stream(10, "h1"), B=300)
        self.assertFalse(np.array_equal(a, c))

    def test_h2_stream_key_does_not_move_h1(self) -> None:
        cells = _cells(0.7, 0.4)
        a = cluster_bootstrap(cells, d1, rng=rngmod.stream(9, "h1"), B=200)
        saved = dict(rngmod.STREAMS)
        try:
            rngmod.STREAMS["h2"] = 99
            b = cluster_bootstrap(cells, d1, rng=rngmod.stream(9, "h1"), B=200)
        finally:
            rngmod.STREAMS.clear()
            rngmod.STREAMS.update(saved)
        self.assertTrue(np.array_equal(a, b))

    def test_draw_order_is_replicate_then_sorted_cell(self) -> None:
        cells = _cells(0.7, 0.4, n=12)
        got = cluster_bootstrap(cells, d1, rng=rngmod.stream(4, "h1"), B=25)
        g = rngmod.stream(4, "h1")
        want = []
        for _ in range(25):
            r = {}
            for c in sorted(cells):                      # CLAIM then NONE
                idx = g.integers(0, 12, size=12)
                r[c] = cells[c].y[idx].sum() / cells[c].m[idx].sum()
            want.append(round(r["NONE"] - r["CLAIM"], 12))
        self.assertTrue(np.allclose(got, want, atol=0, rtol=0))

    def test_planted_effects(self) -> None:
        up = cluster_bootstrap(_cells(0.75, 0.50), d1, rng=rngmod.stream(1, "h1"), B=2000)
        lo, hi = percentile_interval(up)
        self.assertTrue(rejects(lo, hi) and lo > 0)
        down = cluster_bootstrap(_cells(0.50, 0.75), d1, rng=rngmod.stream(1, "h1"), B=2000)
        lo, hi = percentile_interval(down)
        self.assertTrue(rejects(lo, hi) and hi < 0)
        null = cluster_bootstrap(_cells(0.6, 0.6, seed=8), d1, rng=rngmod.stream(1, "h1"), B=2000)
        self.assertFalse(rejects(*percentile_interval(null)))

    def test_degenerate_lower_endpoint_zero_does_not_reject(self) -> None:
        # CLAIM never reports and one NONE episode reports: every replicate is >= 0 and many are exactly 0
        y = np.zeros(40)
        y[0] = 3.0
        cells = {"NONE": CellData("NONE", y, np.full(40, 3.0)),
                 "CLAIM": CellData("CLAIM", np.zeros(40), np.full(40, 3.0))}
        reps = cluster_bootstrap(cells, d1, rng=rngmod.stream(2, "h1"), B=5000)
        lo, hi = percentile_interval(reps)
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0)
        self.assertFalse(rejects(lo, hi))

    def test_nan_when_no_units_and_empty_cell(self) -> None:
        cells = {"NONE": CellData("NONE", np.zeros(3), np.zeros(3)), "CLAIM": CellData("CLAIM", np.ones(3), np.full(3, 3.0))}
        reps = cluster_bootstrap(cells, d1, rng=rngmod.stream(2, "h1"), B=10)
        self.assertTrue(np.isnan(reps).all())
        with self.assertRaises(ValueError):
            cluster_bootstrap({"A": CellData("A", np.zeros(0), np.zeros(0))}, lambda r: 0.0,
                              rng=rngmod.stream(2, "h1"), B=3)

    def test_multi_uses_the_same_replicates(self) -> None:
        cells = _cells(0.7, 0.4)
        multi = cluster_bootstrap_multi(cells, {"d": d1, "n": lambda r: r["NONE"]}, rng=rngmod.stream(9, "h1"), B=100)
        single = cluster_bootstrap(cells, d1, rng=rngmod.stream(9, "h1"), B=100)
        self.assertTrue(np.array_equal(multi["d"], single))

    def test_seed_paired(self) -> None:
        a = {s: (float(s % 4), 3.0) for s in range(1, 31)}
        b = {s: (float(s % 3), 3.0) for s in range(1, 41)}
        reps = seed_paired_bootstrap({"NONE": a, "CLAIM": b}, d1, rng=rngmod.stream(3, "seed_paired"), B=200)
        self.assertEqual(len(reps), 200)
        self.assertFalse(np.isnan(reps).any())
        with self.assertRaises(ValueError):
            seed_paired_bootstrap({"NONE": {1: (1, 3)}, "CLAIM": {2: (1, 3)}}, d1, rng=rngmod.stream(3, "seed_paired"),
                                  B=2)


if __name__ == "__main__":
    unittest.main()
