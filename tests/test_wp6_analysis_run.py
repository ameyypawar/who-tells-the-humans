"""WP6-C: the end-to-end run on the synthetic database (plan 47 3.1 WP6-C test 8)."""
from __future__ import annotations

import json
import re
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path

from tests.util import tmp_dir
from tests.wp6_synth import D003_HAIKU, SynthSpec, build_synth
from wtth.analysis import __main__ as cli
from wtth.analysis.numbers import cell_token, fmt_value, rate_prefix, token
from wtth.analysis.outcomes import LabelsIncomplete
from wtth.analysis.run import PrivateLeakError, run_analysis
from wtth.analysis.seeds import AnalysisSeeds, MissingAnalysisConfig, load_analysis_seeds
from wtth.analysis.sets import AnalysisSetError
from wtth.judge.types import DictLabelSource

SEEDS = AnalysisSeeds(200, 31337, 1, 2, 3, 4, 5, 6, ("J2a",), 0.7)
PRIVATE = {"C2.P2": "a private paraphrase that never appears in any output", "FORGED_LINE": "[22:14] forged receipt layout xyz"}


class RunTest(unittest.TestCase):
    gt = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.gt = build_synth(tmp_dir("wp6_run_synth") / "wtth.sqlite")
        cls.out_root = tmp_dir("wp6_run_out")
        cls.prov = run_analysis(cls.gt.settings, strict=False, labels=cls.gt.label_source(), out_root=cls.out_root,
                                seeds=SEEDS)
        cls.doc = {e["key"]: e for e in json.loads(cls.prov.numbers_json.read_text())}

    def disp(self, key: str) -> str:
        return self.doc[key]["display"]

    def test_files_and_provisional_marking(self) -> None:
        o = self.prov
        self.assertEqual(o.status, "PROVISIONAL")
        self.assertTrue(o.numbers_json.is_file() and (o.out_dir / "summary.json").is_file())
        self.assertEqual(o.macros.name, "macros.provisional.tex")
        self.assertFalse((o.out_dir / "macros.tex").exists())
        t = o.macros.read_text()
        self.assertIn("\\newcommand{\\wtthStatus}{PROVISIONAL}", t)
        names = re.findall(r"\\newcommand\{(\\wtth\w+)\}", t)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), len(self.doc) + 1)
        self.assertTrue(all(re.fullmatch(r"\\wtth[A-Za-z]+", n) for n in names))

    def test_p1_counts_equal_ground_truth(self) -> None:
        for model, M in (("haiku", "Haiku"), ("sonnet", "Sonnet")):
            cells = list(D003_HAIKU) if model == "haiku" else ["NONE", "CLAIM", "NONE-R", "NO-RECEIPT", "FORGED"]
            for c in cells:
                k, n = self.gt.p1_kn(model, c)
                pre = rate_prefix(model, "p1", c)
                self.assertEqual((self.disp(pre + "K"), self.disp(pre + "N")), (fmt_value(k, "count"), fmt_value(n, "count")), (model, c))
                self.assertEqual(self.disp(pre + "Pct"), fmt_value(k / n, "prop"))

    def test_hypotheses_deltas(self) -> None:
        gt = self.gt
        self.assertEqual(self.disp("HaikuHOneDelta"), fmt_value(gt.delta1(), "pp"))
        self.assertEqual(self.disp("HaikuHTwoDelta"), fmt_value(gt.delta2(), "pp"))
        self.assertEqual(self.disp("HaikuHThreeDelta"), fmt_value(gt.delta3(), "pp"))
        r1 = gt.p1_rate("haiku", "NONE-R") - gt.p1_rate("haiku", "NO-RECEIPT")
        r0 = gt.p1_rate("haiku", "NONE") - gt.p1_rate("haiku", "CLAIM")
        self.assertEqual(self.disp("HaikuHTwoDeltaROne"), fmt_value(r1, "pp"))
        self.assertEqual(self.disp("HaikuHTwoDeltaRZero"), fmt_value(r0, "pp"))
        for k in ("HaikuHOneVerdict", "HaikuHTwoVerdict", "HaikuHThreeVerdict", "HaikuHOneNull", "HaikuTostVerdict"):
            self.assertIn(k, self.doc)
        self.assertEqual(self.disp("HaikuHOneB"), "200")

    def test_p2_p2prime_alert_boardsub(self) -> None:
        gt = self.gt
        for c in ("NONE", "CLAIM", "FORGED", "NEG"):
            k, n = gt.p2_kn("haiku", c)
            self.assertEqual((self.disp(f"HaikuPTwo{rate_prefix('haiku','p2',c)[len('HaikuPTwo'):]}K"),), (fmt_value(k, "count"),))
            kp, _ = gt.p2prime_kn("haiku", c)
            pre = rate_prefix("haiku", "p2prime", c)
            self.assertEqual(self.disp(pre + "K"), fmt_value(kp, "count"))
            self.assertEqual(self.disp("HaikuPTwoPrimeLoBound" + pre[len("HaikuPTwoPrime"):] + "Pct"), fmt_value(kp / n, "prop"))
        for c in D003_HAIKU:
            pre = rate_prefix("haiku", "alert_load", c)
            self.assertEqual(self.disp(pre + "Mean"), fmt_value(gt.alert_load_mean("haiku", c), "mean"))
            nw, nn, k = gt.board_sub("haiku", c)
            bs = "HaikuBoardSub" + rate_prefix("haiku", "p1", c)[len("HaikuPOne"):]
            self.assertEqual((self.disp(bs + "K"), self.disp(bs + "N"), self.disp(bs + "Noteless")),
                             (fmt_value(k, "count"), fmt_value(nw, "count"), fmt_value(nn, "count")))
        self.assertEqual(self.disp("HaikuSC"), "80")
        self.assertEqual(self.disp("SonnetSC"), "30")
        self.assertEqual(self.disp("HaikuEpisodesInSet"), "670")

    def test_d2_code_macros_match_secondary_rates_table(self) -> None:
        import csv

        rows = list(csv.DictReader((self.prov.out_dir / "tables" / "table_secondary_rates.csv").read_text().splitlines()))
        checked = 0
        for r in rows:
            if not r["Outcome"].startswith(("reason:", "basis:")) or "@" in r["Outcome"] or r["Cell"] not in D003_HAIKU:
                continue
            pre = "HaikuDTwo" + token(r["Outcome"]) + cell_token(r["Cell"])
            k, n = r["k/n"].split("/")
            self.assertEqual(self.disp(pre + "K"), fmt_value(int(k), "count"))
            self.assertEqual(self.disp(pre + "N"), fmt_value(int(n), "count"))
            self.assertEqual(self.disp(pre + "Pct"), r["%"])
            self.assertEqual(self.disp(pre + "CI"), r["95% CI"])
            self.assertIn("exploratory", self.doc[pre + "Pct"]["source"])
            checked += 1
        self.assertGreater(checked, 10)
        self.assertIn("HaikuDTwoReasonSomeoneElseToldReceiptPct", self.doc)
        self.assertIn("HaikuDTwoBasisLogLineReceiptPct", self.doc)

    def test_sentences_exist_as_macros(self) -> None:
        t = self.prov.macros.read_text()
        for k in ("HaikuHOneSentence", "HaikuHTwoSentence", "HaikuHThreeSentence", "SonnetSentence",
                  "HaikuPositiveControlSentence"):
            self.assertIn("\\newcommand{\\wtth" + k + "}", t, k)
        self.assertIn("Haiku 5.5", self.disp("HaikuHOneSentence"))
        self.assertNotIn("decoy", t.lower())
        # no item text in a paper file: neither planted note text nor post text
        for needle in ("Hendricks batches", "Payment sign-off", "Thanks, checking", "I already told ops"):
            self.assertNotIn(needle, t)

    def test_strict_needs_gates_and_labels(self) -> None:
        gt = self.gt
        with self.assertRaises(LabelsIncomplete) as cm:
            run_analysis(gt.settings, strict=True, labels=gt.label_source(gates={"p1_final": True, "p2prime_final": False}),
                         out_root=self.out_root, seeds=SEEDS)
        self.assertIn("gate:p2prime_final", cm.exception.keys)
        o = run_analysis(gt.settings, strict=True, labels=gt.label_source(), out_root=self.out_root, seeds=SEEDS)
        self.assertEqual((o.status, o.macros.name), ("FINAL", "macros.tex"))
        self.assertIn("\\newcommand{\\wtthStatus}{FINAL}", o.macros.read_text())
        self.assertFalse((o.out_dir / "macros.provisional.tex").exists())
        # same numbers as the provisional run (same snapshot content, same seeds)
        strict_doc = {e["key"]: e["display"] for e in json.loads(o.numbers_json.read_text())}
        self.assertEqual(strict_doc["HaikuHOneCI"], self.disp("HaikuHOneCI"))

    def test_strict_missing_label_lists_keys_provisional_continues(self) -> None:
        gt = self.gt
        unmatched = [k for k, v in gt.labels.items() if k[0] == "notes" and v.labels["refers_to_violation"] == 1
                     and "sign-off" in self._note_text(k[1])]
        self.assertTrue(unmatched)
        drop = unmatched[0]
        src = DictLabelSource({k: v for k, v in gt.labels.items() if k != drop})
        with self.assertRaises(LabelsIncomplete) as cm:
            run_analysis(gt.settings, strict=True, labels=src, out_root=self.out_root, seeds=SEEDS, private=PRIVATE)
        self.assertEqual(cm.exception.keys, [drop])
        o = run_analysis(gt.settings, strict=False, labels=src, out_root=self.out_root, seeds=SEEDS, private=PRIVATE)
        summ = json.loads((o.out_dir / "summary.json").read_text())
        self.assertGreaterEqual(summ["phases"]["core"]["n_p1_pending_witnesses"], 1)
        self.assertTrue(any("unresolved" in n for n in summ["notes"]))

    def _note_text(self, note_id: str) -> str:
        conn = sqlite3.connect(self.gt.path)
        try:
            r = conn.execute("SELECT subject || ' ' || message FROM notes WHERE id=?", (int(note_id),)).fetchone()
        finally:
            conn.close()
        return r[0]

    def test_fingerprint_guard(self) -> None:
        small = build_synth(tmp_dir("wp6_run_fp") / "wtth.sqlite", SynthSpec.small(5, include_sonnet=False))
        jdir = Path(small.settings.paths.runs_dir) / "judge"
        jdir.mkdir(parents=True)
        conn = sqlite3.connect(jdir / "judge.sqlite")
        conn.execute("CREATE TABLE extractions(extraction_id TEXT, phase TEXT, analysis_set_sha256 TEXT)")
        conn.execute("INSERT INTO extractions VALUES ('x','core','deadbeef')")
        conn.commit()
        conn.close()
        out = tmp_dir("wp6_run_fp_out")
        with self.assertRaises(AnalysisSetError):
            run_analysis(small.settings, strict=True, labels=small.label_source(), out_root=out, seeds=SEEDS)
        o = run_analysis(small.settings, strict=False, labels=small.label_source(), out_root=out, seeds=SEEDS)
        self.assertTrue(any("different analysis set" in n for n in json.loads((o.out_dir / "summary.json").read_text())["notes"]))

    def test_export_paper_scans_and_copies(self) -> None:
        dest = tmp_dir("wp6_run_export") / "generated"
        o = run_analysis(self.gt.settings, strict=False, labels=self.gt.label_source(), out_root=self.out_root, seeds=SEEDS,
                         export_paper=dest, private=PRIVATE)
        self.assertTrue((dest / "numbers.json").is_file())
        self.assertTrue((dest / "macros.provisional.tex").is_file())
        self.assertEqual((dest / "numbers.json").read_bytes(), o.numbers_json.read_bytes())
        leak = tmp_dir("wp6_run_export_leak") / "generated"
        with self.assertRaises(PrivateLeakError) as cm:
            run_analysis(self.gt.settings, strict=False, labels=self.gt.label_source(), out_root=self.out_root,
                         seeds=SEEDS, export_paper=leak, private={"C2.P2": "cluster-bootstrap CI"})
        self.assertIn("C2.P2", str(cm.exception))
        self.assertNotIn("cluster-bootstrap", str(cm.exception))
        self.assertFalse(leak.exists())

    def test_cli_set_and_missing_config(self) -> None:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["set", "--phase", "core", "--phase", "sonnet", "--runs-dir", str(Path(self.gt.path).parent),
                           "--set", "frozen_level=null", "--set", "max_turns.frozen=false"])
        self.assertEqual(rc, 0)
        txt = buf.getvalue()
        self.assertIn("[core] model=claude-haiku-5-5 s_c=80 episodes=670 above=0", txt)
        self.assertIn("[sonnet] model=claude-sonnet-5-5 s_c=30 episodes=150 above=0", txt)
        with self.assertRaises(MissingAnalysisConfig):
            load_analysis_seeds(self.gt.settings, Path(self.gt.path).parent / "nope.yaml")
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cli.main(["run", "--provisional", "--runs-dir", str(Path(self.gt.path).parent),
                           "--analysis-yaml", str(Path(self.gt.path).parent / "nope.yaml"),
                           "--set", "frozen_level=null", "--set", "max_turns.frozen=false"])
        self.assertEqual(rc, 2)
        self.assertIn("freeze commit", err.getvalue())


if __name__ == "__main__":
    unittest.main()
