"""WP6-C: analysis set, snapshot, seeds loader (plan 47 3.1 WP6-C tests 1)."""
from __future__ import annotations

import time
import unittest
from pathlib import Path

from tests.util import seed_episode, tmp_dir
from tests.wp6_synth import SynthSpec, build_synth
from wtth.analysis import sets
from wtth.analysis.seeds import (AnalysisConfigError, MissingAnalysisConfig, load_analysis_seeds,
                                 parse_analysis_seeds)
from wtth.db import Db

GOOD_YAML = """
bootstrap_B: 10000
seeds: {bootstrap: 11, judge_order: 12, first_batch: 13, h5_sample: 14, qa_sample: 15, blind_sample: 16, export_order: 17}
j2_order: [J2a, J2b, J2c]
kappa_threshold: 0.70
"""


class SeedsTest(unittest.TestCase):
    def test_loads(self) -> None:
        d = tmp_dir("wp6_seeds_ok")
        p = d / "analysis.yaml"
        p.write_text(GOOD_YAML, encoding="utf-8")
        s = load_analysis_seeds(None, path=p)
        self.assertEqual((s.bootstrap_B, s.bootstrap, s.export_order, s.j2_order, s.kappa_threshold),
                         (10000, 11, 17, ("J2a", "J2b", "J2c"), 0.70))

    def test_missing_file_is_a_clear_error(self) -> None:
        d = tmp_dir("wp6_seeds_missing")
        with self.assertRaises(MissingAnalysisConfig) as cm:
            load_analysis_seeds(None, path=d / "analysis.yaml")
        self.assertIn("freeze commit", str(cm.exception))

    def test_malformed(self) -> None:
        for bad in ({}, {"bootstrap_B": 5}, {"bootstrap_B": 0, "seeds": {}, "j2_order": ["a"], "kappa_threshold": .7}):
            with self.assertRaises(AnalysisConfigError):
                parse_analysis_seeds(bad)
        import yaml

        raw = yaml.safe_load(GOOD_YAML)
        raw["seeds"]["bootstrap"] = "x"
        with self.assertRaises(AnalysisConfigError):
            parse_analysis_seeds(raw)
        raw = yaml.safe_load(GOOD_YAML)
        raw["j2_order"] = ["a", "a"]
        with self.assertRaises(AnalysisConfigError):
            parse_analysis_seeds(raw)


class SetsTest(unittest.TestCase):
    def aset(self, gt, phase: str = "core", **kw):
        return sets.analysis_set(gt.settings, Db(gt.path), phase, **kw)

    def test_everything_done(self) -> None:
        gt = build_synth(tmp_dir("wp6_sets_full") / "wtth.sqlite")
        a = self.aset(gt)
        self.assertEqual(a.s_c, 80)
        self.assertEqual(len(a.episodes), 670)
        self.assertEqual(a.above, ())
        self.assertEqual(a.failed_permanent, ())
        self.assertEqual(a.missing, ())
        self.assertEqual(a.planned["NONE"], tuple(range(1, 81)))
        self.assertEqual(a.planned["CONCERN"], tuple(range(1, 51)))
        self.assertEqual(a.model, "claude-haiku-5-5")
        # decoy: the failed first attempt never becomes the final attempt
        e = [x for x in a.episodes if x.cell == "NONE" and x.seed == 2][0]
        self.assertEqual(e.attempt, 2)
        s = self.aset(gt, "sonnet")
        self.assertEqual((s.s_c, len(s.episodes)), (30, 150))
        self.assertEqual(s.model, "claude-sonnet-5-5")
        self.assertEqual(self.aset(gt).fingerprint, a.fingerprint)

    def test_queued_episode_sets_s_c(self) -> None:
        gt = build_synth(tmp_dir("wp6_sets_queued") / "wtth.sqlite", SynthSpec(queued_at=(("NONE", 37),)))
        a = self.aset(gt)
        self.assertEqual(a.s_c, 36)
        self.assertEqual(a.missing, (("NONE", 37),))
        # done episodes above s_c are exploratory, none of them inside
        self.assertTrue(all(e.seed <= 36 for e in a.episodes))
        self.assertTrue(all(e.seed > 36 for e in a.above))
        self.assertEqual(len(a.episodes) + len(a.above), 670 - 1)
        self.assertEqual(a.s_c, gt.s_c["core"])

    def test_failed_permanent_counts_as_complete(self) -> None:
        gt = build_synth(tmp_dir("wp6_sets_failed") / "wtth.sqlite", SynthSpec(failed_permanent_at=(("CLAIM", 10),)))
        a = self.aset(gt)
        self.assertEqual(a.s_c, 80)
        self.assertEqual(a.failed_permanent, (("CLAIM", 10),))
        self.assertEqual(len(a.episodes), 669)
        self.assertFalse(any(e.cell == "CLAIM" and e.seed == 10 for e in a.episodes))

    def test_cut_in_force_and_unplanned_done_go_above(self) -> None:
        n = {c: 30 for c in ("NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG",
                             "POS")}
        n["B-NONE"] = 20
        gt = build_synth(tmp_dir("wp6_sets_cut") / "wtth.sqlite",
                         SynthSpec(haiku_n=n, include_sonnet=False, extra_done={"B-NONE": (21, 22, 23, 24, 25)}))
        a = self.aset(gt)
        self.assertEqual(a.cut["max_seed_by_cell"]["B-NONE"], 20)
        self.assertEqual(a.planned["B-NONE"], tuple(range(1, 21)))
        self.assertEqual(a.s_c, 30)
        self.assertEqual(sorted((e.cell, e.seed) for e in a.above), [("B-NONE", s) for s in range(21, 26)])
        self.assertEqual(len(a.episodes), 10 * 30 + 20)

    def test_mvp_and_core_raise(self) -> None:
        gt = build_synth(tmp_dir("wp6_sets_mvp") / "wtth.sqlite", SynthSpec.small(6, include_sonnet=False))
        gt.db().write(lambda c: seed_episode(c, "mvp/main/claude-haiku-5-5/NONE/L2/s1", phase="mvp", cell="NONE",
                                             seed=1, status="done", queue_pos=1))
        with self.assertRaises(sets.AnalysisSetError):
            self.aset(gt)
        a = self.aset(gt, prefer="core")
        self.assertEqual(len(a.episodes), 11 * 6)

    def test_snapshot(self) -> None:
        gt = build_synth(tmp_dir("wp6_sets_snap") / "wtth.sqlite", SynthSpec.small(4, include_sonnet=False))
        t0 = time.time()
        snap = sets.snapshot_main_db(gt.settings)
        self.assertTrue(snap.path.is_file())
        self.assertEqual(snap.path.parent, Path(gt.settings.paths.runs_dir) / "analysis" / "snapshots")
        self.assertEqual(len(snap.sha256), 64)
        self.assertGreaterEqual(snap.taken_at, t0 - 1)
        a = sets.analysis_set(gt.settings, Db(snap.path), "core")
        self.assertEqual(len(a.episodes), 44)


if __name__ == "__main__":
    unittest.main()
