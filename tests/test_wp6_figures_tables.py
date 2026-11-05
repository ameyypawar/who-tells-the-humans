"""WP6-B4: figures and tables against synthetic result objects (plan 47 B acceptance test 9)."""
from __future__ import annotations

import csv
import os
import re
import unittest
from pathlib import Path

_MPL = Path(__file__).resolve().parent / "tmp" / "mplconfig"
_MPL.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPL))

from wtth.analysis import figures, style, tables  # noqa: E402
from wtth.analysis.results import (BoardSubRate, CellRate, Contrast, CountRow, Diff, EpisodeRate,  # noqa: E402
                                   FrontierPoint, HypothesisResults, KMCurve, MeanRate, SecondaryResults)
from tests.util import tmp_dir  # noqa: E402

CELLS8 = ("NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "NO-RECEIPT", "FORGED", "NEG")


def contrast(name, est, lo, hi, rejected, status="confirmatory", direction="predicted", **kw):
    return Contrast(name, est, lo, hi, 0.95, 10000, "h1", rejected, direction, status, {}, **kw)


def p2_rates():
    out, outp = [], []
    for i, c in enumerate(CELLS8):
        k, n = 5 + 3 * i, 40
        p = k / n
        out.append(EpisodeRate("haiku", c, "P2", k, n, p, max(p - 0.12, 0), min(p + 0.12, 1)))
        if c == "CLAIM":
            outp.append(EpisodeRate("haiku", c, "P2prime", None, n, None, None, None, bound_lo=0.2, bound_hi=0.45))
        else:
            outp.append(EpisodeRate("haiku", c, "P2prime", k, n, p - 0.02, max(p - 0.14, 0), min(p + 0.1, 1)))
    return out, outp


def p1_rates():
    spec = {"NONE": (30, 120), "CLAIM": (12, 120), "NONE-R": (28, 120), "NO-RECEIPT": (20, 120), "FORGED": (9, 120)}
    return [CellRate("haiku", c, "P1", k, n, 40, k / n, k / n - 0.08, k / n + 0.08, "cluster_bootstrap")
            for c, (k, n) in spec.items()]


def hyp():
    return HypothesisResults(
        contrast("H1", 0.15, 0.05, 0.25, True), contrast("H2", 0.05, -0.04, 0.14, False),
        contrast("H3", 0.0, 0.0, 0.0, None, status="descriptive"), None, (0.0, 0.2, "inconclusive"), {}, "H2")


def km():
    out = []
    for cell in ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT", "FORGED", "CONCERN"):
        out.append(KMCurve(cell, "episode", (1, 2, 3, 4, 5), (0.9, 0.8, 0.7, 0.6, 0.55), (0.8, 0.7, 0.6, 0.5, 0.4),
                           (0.95, 0.9, 0.8, 0.7, 0.65), (40, 36, 32, 28, 24), (4, 4, 4, 4, 2)))
    out.append(KMCurve("NONE", "witness", (1, 2), (0.9, 0.8), (0.8, 0.7), (0.95, 0.9), (120, 100), (12, 12)))
    return out


def frontier():
    return [FrontierPoint("CLAIM", 0.8, 0.6, 1.0, 0.4, 0.3, 0.5, "CLAIM", "CLAIM", False),
            FrontierPoint("POLICY", 1.1, 0.9, 1.3, 0.3, 0.2, 0.4, "POLICY", "POLICY", False),
            FrontierPoint("receipts arm", 1.0, 0.8, 1.2, 0.35, 0.25, 0.45, "RECEIPT", "NO-RECEIPT", False),
            FrontierPoint("FORGED", 0.5, 0.3, 0.7, 0.6, 0.5, 0.7, "FORGED", "FORGED", False),
            FrontierPoint("NONE", 1.6, 1.4, 1.8, 0.15, 0.08, 0.22, "NONE", "NONE", True)]


class Capture:
    """Wraps style.save so a test can inspect the figure's colours before it is closed."""

    def __enter__(self):
        self.colours: set[str] = set()
        self.orig = figures.save

        def wrapper(fig, stem, **kw):
            self.colours |= style.used_colours(fig)
            return self.orig(fig, stem, **kw)
        figures.save = wrapper
        return self

    def __exit__(self, *a):
        figures.save = self.orig


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class TestFigures(unittest.TestCase):
    def build(self, d: Path):
        p2, p2p = p2_rates()
        return {"fig1a": figures.fig1a(p2, p2p, d, model_label="Haiku"),
                "fig1b": figures.fig1b(frontier(), d, model_label="Haiku"),
                "fig_p1_by_cell": figures.fig_p1_by_cell(p1_rates(), hyp(), d),
                "fig_km": figures.fig_km(km(), d)}

    def test_four_files_each_and_alt_text_names_values(self):
        d = tmp_dir("wp6_fig_files")
        res = self.build(d)
        for name, f in res.items():
            for p in (f.pdf, f.png, f.alt_txt, f.data_csv):
                self.assertTrue(p.is_file() and p.stat().st_size > 0, (name, p))
            self.assertTrue(f.pdf.read_bytes().startswith(b"%PDF"))
            self.assertTrue(f.png.read_bytes().startswith(b"\x89PNG"))
        alt = res["fig1a"].alt_txt.read_text()
        self.assertIn("lower is safer", alt)
        for c in CELLS8:
            self.assertIn(c + " ", alt)
        self.assertIn("bracketed 20.0% to 45.0%", alt)
        self.assertIn("CLAIM", res["fig1b"].alt_txt.read_text())
        self.assertIn("grey reference star", res["fig1b"].alt_txt.read_text())

    def test_pdf_byte_identical_across_runs(self):
        a, b = tmp_dir("wp6_fig_det_a"), tmp_dir("wp6_fig_det_b")
        ra, rb = self.build(a), self.build(b)
        for name in ra:
            self.assertEqual(ra[name].pdf.read_bytes(), rb[name].pdf.read_bytes(), name)
            self.assertEqual(ra[name].data_csv.read_bytes(), rb[name].data_csv.read_bytes(), name)
            self.assertEqual(ra[name].alt_txt.read_bytes(), rb[name].alt_txt.read_bytes(), name)
        self.assertNotIn(b"/CreationDate", ra["fig1a"].pdf.read_bytes())

    def test_only_palette_colours(self):
        d = tmp_dir("wp6_fig_colours")
        with Capture() as cap:
            self.build(d)
        self.assertTrue(cap.colours)
        bad = cap.colours - style.ALLOWED_COLOURS
        self.assertEqual(bad, set())
        self.assertNotIn("#F0E442", cap.colours)

    def test_csv_equals_input(self):
        d = tmp_dir("wp6_fig_csv")
        res = self.build(d)
        p2, p2p = p2_rates()
        rows = read_csv(res["fig1a"].data_csv)
        self.assertEqual(len(rows), 16)
        got = {(r["cell"], r["outcome"]): r for r in rows}
        for r in p2:
            g = got[(r.cell, "P2")]
            self.assertEqual((int(g["k"]), int(g["n"]), float(g["p"]), float(g["lo"]), float(g["hi"])), (r.k, r.n, r.p, r.lo, r.hi))
        claim = got[("CLAIM", "P2prime")]
        self.assertEqual((claim["p"], float(claim["bound_lo"]), float(claim["bound_hi"])), ("", 0.2, 0.45))
        fr = read_csv(res["fig1b"].data_csv)
        self.assertEqual([r["label"] for r in fr], [p.label for p in frontier()])
        self.assertEqual([float(r["x"]) for r in fr], [p.x for p in frontier()])
        self.assertEqual([r["reference"] for r in fr], ["False"] * 4 + ["True"])
        p1 = read_csv(res["fig_p1_by_cell"].data_csv)
        self.assertEqual({(r["group"], r["cell"]) for r in p1}, {("H1", "NONE"), ("H1", "CLAIM"), ("H2", "NONE"),
                         ("H2", "CLAIM"), ("H2", "NONE-R"), ("H2", "NO-RECEIPT"), ("H3", "NO-RECEIPT"), ("H3", "FORGED")})
        k = read_csv(res["fig_km"].data_csv)
        self.assertEqual(len(k), 6 * 5)            # episode-level curves only; the witness-level one is not drawn
        self.assertEqual({r["unit"] for r in k}, {"episode"})
        self.assertEqual({r["panel"] for r in k if r["cell"] == "CONCERN"}, {"R0"})
        self.assertEqual({r["panel"] for r in k if r["cell"] == "FORGED"}, {"R1"})

    def test_series_are_distinguishable_without_colour(self):
        pairs = list(style.CELL_STYLE.values())
        self.assertEqual(len({(c, m) for c, m, _ in pairs}), len(pairs))
        self.assertEqual(len({(c, ls) for c, _, ls in pairs}), len(pairs))
        for c, _, _ in pairs:
            self.assertIn(c, style.PALETTE.values())
        self.assertNotIn("#F0E442", style.PALETTE.values())

    def test_missing_cells_do_not_crash(self):
        d = tmp_dir("wp6_fig_sparse")
        p2, p2p = p2_rates()
        f = figures.fig1a(p2[:3], [], d, model_label="Sonnet")
        self.assertEqual(len(read_csv(f.data_csv)), 3)
        f = figures.fig1b(frontier()[:1], d, model_label="Sonnet")
        self.assertTrue(f.pdf.exists())


class TestFormatting(unittest.TestCase):
    def test_round_half_up_and_signs(self):
        self.assertEqual(tables._dec(0.125, 2), "0.13")
        self.assertEqual(tables._dec(-0.125, 2), "-0.13")
        self.assertEqual(tables._dec(2.5, 0), "3")
        self.assertEqual(tables._dec(-0.0001, 1), "0.0")
        self.assertEqual(tables.pct(0.0625).csv, "6.3")
        self.assertEqual(tables.pct(-0.15).tex, r"\ensuremath{-}15.0")
        self.assertEqual(tables.pp(-0.0449).csv, "-4.5")
        self.assertEqual(tables.ci(-0.04, 0.14).tex, r"\ensuremath{-}4.0 to 14.0")
        self.assertEqual(tables.integer(1234567).tex, "1{,}234{,}567")
        self.assertEqual(tables.integer(12).tex, "12")
        self.assertEqual(tables.num(0.7).csv, "0.70")
        self.assertEqual(tables.usd(3.456).tex, r"\$3.46")
        self.assertEqual(tables.pct(None).tex, "--")

    def test_latex_escape(self):
        s = tables.latex_escape("a_b & 50% #1 {x} $y ~ ^ \\ < > |")
        for frag in (r"a\_b", r"\&", r"50\%", r"\#1", r"\{x\}", r"\$y", r"\textasciitilde{}", r"\textasciicircum{}",
                     r"\textbackslash{}", r"\textless{}", r"\textgreater{}", r"\textbar{}"):
            self.assertIn(frag, s)
        self.assertNotRegex(re.sub(r"\\.", "", re.sub(r"\\[a-z]+\{\}", "", s)), r"[&%#_~^$]")


class TestTables(unittest.TestCase):
    def check(self, files, n_body_rows, n_cols):
        t = files.tex.read_text(encoding="utf-8")
        self.assertIn(r"\toprule", t)
        self.assertIn(r"\midrule", t)
        self.assertIn(r"\bottomrule", t)
        self.assertEqual(t.count(r"\begin{tabular}"), 1)
        self.assertNotIn(r"\hline", t)
        body = [ln for ln in t.splitlines() if ln.endswith(r"\\")]
        self.assertEqual(len(body), n_body_rows + 1)         # + header
        for ln in body:
            self.assertEqual(len(re.findall(r"(?<!\\)&", ln)), n_cols - 1, ln)
        with open(files.csv, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(len(rows), n_body_rows + 1)
        return t, rows

    def test_hypotheses(self):
        d = tmp_dir("wp6_tab_h")
        f = tables.table_hypotheses(hyp(), p1_rates(), None, d)
        t, rows = self.check(f, 5, 7)
        self.assertIn("rejected, predicted direction", t)
        self.assertIn("not rejected", t)
        self.assertIn("not tested (sequence stopped earlier)", t)
        self.assertIn(r"NONE 30/120 (25.0%)", t.replace(r"\%", "%"))
        self.assertEqual(rows[1][3], "15.0")
        self.assertIn("95\\% CI", t)
        self.assertEqual(rows[4][0], "H1 null rule")
        self.assertEqual(rows[5][0], "TOST")

    def test_p2_with_bracket_and_diffs(self):
        d = tmp_dir("wp6_tab_p2")
        p2, p2p = p2_rates()
        diffs = [Diff("NONE \u2212 CLAIM", "P2", -0.1, -0.25, 0.04, "newcombe", 8, 40, 14, 40),
                 Diff("A_B & C", "P2", 0.2, 0.1, 0.3, "newcombe")]
        f = tables.table_p2(p2, p2p, diffs, None, d)
        t, rows = self.check(f, 16 + 2, 5)
        self.assertIn(r"A\_B \& C", t)
        self.assertIn(r"\ensuremath{-}10.0", t)
        self.assertIn("P2\u2032".replace("\u2032", r"\ensuremath{'}") + r" (bracketed)", t)
        br = next(r for r in rows if "bracketed" in r[1] and r[0] == "CLAIM")
        self.assertEqual(br[4], "20.0 to 45.0")

    def test_counts(self):
        d = tmp_dir("wp6_tab_counts")
        counts = [CountRow("haiku", "NONE", "refusal", 2), CountRow("haiku", "CLAIM", "refusal", 1234),
                  CountRow("haiku", "NONE", "max_turns", 0), CountRow("sonnet", "NONE", "refusal", 0)]
        f = tables.table_counts(counts, None, d)
        t, rows = self.check(f, 3, 4)
        self.assertIn("1{,}234", t)
        self.assertEqual(rows[0], ["Model", "Count", "NONE", "CLAIM"])

    def test_secondary(self):
        d = tmp_dir("wp6_tab_sec")
        rates = (CellRate("haiku", "NONE", "concern_w", 30, 120, 40, 0.25, 0.18, 0.33, "wilson", 1.0),
                 CellRate("haiku", "CLAIM", "concern_w", 20, 120, 40, None, None, None, "wilson", 0.5))
        sec = SecondaryResults(
            "haiku", (), (), (), (), (MeanRate("haiku", "NONE", "alert_load", 1.5, 1.1, 1.9, 40, "bootstrap"),),
            rates, contrast("d1_no_ta", 0.12, 0.02, 0.22, True, status="exploratory"), None,
            (), (BoardSubRate("haiku", "NONE", "judge", 120, 60, 40, 5, 0.125, 0.05, 0.28, True, 0.67, "x"),))
        files = tables.table_secondary(sec, None, d)
        self.assertEqual(len(files), 5)
        self.check(files[0], 2, 8)
        self.check(files[1], 1, 6)
        t, rows = self.check(files[2], 1, 10)
        self.assertIn("conditional", t)
        self.check(files[3], 1, 5)
        self.check(files[4], 0, 6)
        self.assertEqual(self.check(files[0], 2, 8)[1][2][4], "")           # None rate -> empty csv cell

    def test_judge_gate_cost(self):
        d = tmp_dir("wp6_tab_judge")
        agr = {"model_model": {"note": {"refers_to_violation": {
                   "label": "refers_to_violation", "n": 12, "categories": [0, 1], "weights": None, "raw_agreement": 10 / 12,
                   "kappa": 2 / 3, "confusion": [[5, 1], [1, 5]], "kappa_lo": 0.2, "kappa_hi": 0.9}}},
               "model_model_h5": {}, "correction": {"reviewed": 3, "corrected": 2, "rate": 2 / 3, "share_reviewed": 0.5},
               "precision_escalation_h3": {"n_judge_positive_reviewed": 3, "n_confirmed": 2, "precision": 2 / 3},
               "blind": {"status": "not run", "by_type": {}}}
        f = tables.table_judge({"prompt_version": "v1", "n_pass": 11, "n_items": 12, "passed": True}, agr, None, d)
        t, rows = self.check(f, 1 + 1 + 3 + 1, 6)
        self.assertIn("model--model", t)
        self.assertIn("not run", t)
        c = tables.table_confusion(agr, None, d)
        self.check(c, 4, 5)
        g = tables.table_gate_log([{"gate": "G1", "phase": "g1", "level": "L2", "decision": "pass",
                                    "numbers_json": '{"k": 3, "n_x": 10}'}], None, d)
        t, rows = self.check(g, 1, 6)
        self.assertIn(r"k=3; n\_x=10", t)
        self.assertEqual(rows[1][-1], "pilot")
        co = tables.table_cost({"phase_model": [{"phase": "core", "model": "haiku", "n_calls": 5000, "usd": 12.345}],
                                "judge_tier": [{"tier": "J1", "n_calls": 900, "usd": 6.0, "n_estimated": 2}]}, None, d)
        t, rows = self.check(co, 3, 5)
        self.assertIn(r"5{,}000", t)
        self.assertEqual(rows[-1][3], "$18.35")


if __name__ == "__main__":
    unittest.main()
