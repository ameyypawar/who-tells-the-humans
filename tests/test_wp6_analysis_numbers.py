"""WP6-C: NumberBook, formatting, macros, sentence templates (plan 47 3.1 WP6-C test 7)."""
from __future__ import annotations

import re
import unittest

from tests.util import tmp_dir
from wtth.analysis import numbers, text
from wtth.analysis.numbers import NumberBook, fmt_value, latex_escape, macro_name, rate_prefix, token
from wtth.analysis.results import CellRate, Contrast, HypothesisResults


class FormatTest(unittest.TestCase):
    def test_round_half_up_and_signs(self) -> None:
        self.assertEqual(fmt_value(0.285, "prop"), "28.5")
        self.assertEqual(fmt_value(0.2855, "prop"), "28.6")          # half up on the shortest decimal form
        self.assertEqual(fmt_value(0.0025, "pp"), "0.3")
        self.assertEqual(fmt_value(-0.0025, "pp"), "\\ensuremath{-}0.3")
        self.assertEqual(fmt_value(-0.0004, "pp"), "\\ensuremath{-}0.04")      # tiny non-zero keeps two decimals
        self.assertEqual(fmt_value(-0.000004, "pp"), "0.00")
        self.assertEqual(fmt_value(-0.125, "pp"), "\\ensuremath{-}12.5")
        self.assertEqual(fmt_value(1234567, "count"), "1{,}234{,}567")
        self.assertEqual(fmt_value(12, "count"), "12")
        self.assertEqual(fmt_value(0.125, "kappa"), "0.13")
        self.assertEqual(fmt_value(0.745, "kappa"), "0.75")
        self.assertEqual(fmt_value(2.675, "usd"), "\\$2.68")
        self.assertEqual(fmt_value(0.0, "usd"), "\\$0.00")
        self.assertEqual(fmt_value((0.1, 0.25), "ci"), "10.0 to 25.0")
        self.assertEqual(fmt_value((-0.1, 0.25), "ci"), "\\ensuremath{-}10.0 to 25.0")
        self.assertEqual(fmt_value((0.1, 0.25), "meanci"), "0.10 to 0.25")
        self.assertEqual(fmt_value(7.456, "pts"), "7.5")
        self.assertEqual(fmt_value(None, "prop"), "n/a")
        self.assertEqual(fmt_value(float("nan"), "pp"), "n/a")
        self.assertEqual(fmt_value("rejected", "verdict"), "rejected")
        with self.assertRaises(ValueError):
            fmt_value("a free sentence", "verdict")
        with self.assertRaises(ValueError):
            fmt_value(1.5, "count")

    def test_latex_escape(self) -> None:
        self.assertEqual(latex_escape("50% & $5 #1 a_b {x} ~ ^ \\"),
                         r"50\% \& \$5 \#1 a\_b \{x\} \textasciitilde{} \textasciicircum{} \textbackslash{}")

    def test_tokens(self) -> None:
        self.assertEqual(rate_prefix("haiku", "p1", "NONE"), "HaikuPOneNone")
        self.assertEqual(rate_prefix("sonnet", "p2prime", "NO-RECEIPT"), "SonnetPTwoPrimeNoReceipt")
        self.assertEqual(rate_prefix("haiku", "alert_load", "B-NONE"), "HaikuAlertLoadBNone")
        self.assertEqual(token("believes_yes|truth_any=1"), "BelievesYesTruthAnyOne")
        self.assertEqual(macro_name("HaikuHOneCI"), "\\wtthHaikuHOneCI")


class BookTest(unittest.TestCase):
    def test_rejects_digits_duplicates_and_bad_kinds(self) -> None:
        b = NumberBook()
        b.add("HaikuPOneNonePct", 0.5, "prop", source="t")
        for bad in ("Haiku1", "haiku", "A_B", "A-B", "", "ÄB"):
            with self.assertRaises(ValueError, msg=bad):
                b.add(bad, 1, "count", source="t")
        with self.assertRaises(ValueError):
            b.add("HaikuPOneNonePct", 0.6, "prop", source="t")
        with self.assertRaises(ValueError):
            b.add("Other", 1, "bogus", source="t")
        self.assertEqual(len(b), 1)
        self.assertEqual(b.fmt("HaikuPOneNonePct"), "50.0")

    def test_add_rate_and_contrast(self) -> None:
        b = NumberBook()
        b.add_rate("HaikuPOneNone", CellRate("haiku", "NONE", "p1", 144, 240, 80, 0.6, 0.54, 0.66, "wilson"))
        self.assertEqual([b.fmt("HaikuPOneNone" + s) for s in ("K", "N", "Eps", "Pct", "Lo", "Hi", "CI")],
                         ["144", "240", "80", "60.0", "54.0", "66.0", "54.0 to 66.0"])
        c = Contrast("H1", 0.25, 0.1, 0.4, 0.95, 10000, "h1", True, "predicted", "confirmatory", {})
        b.add_contrast("HaikuHOne", c)
        self.assertEqual(b.fmt("HaikuHOneDelta"), "25.0")
        self.assertEqual(b.fmt("HaikuHOneCI"), "10.0 to 40.0")
        self.assertEqual(b.fmt("HaikuHOneB"), "10{,}000")
        self.assertEqual(b.fmt("HaikuHOneVerdict"), "rejected")
        opp = Contrast("H1", -0.25, -0.4, -0.1, 0.95, 10000, "h1", True, "opposite", "confirmatory", {})
        b2 = NumberBook()
        b2.add_contrast("X", opp)
        self.assertEqual(b2.fmt("XVerdict"), "rejected in the opposite direction")
        self.assertEqual(b2.fmt("XDelta"), "\\ensuremath{-}25.0")

    def test_macro_files(self) -> None:
        d = tmp_dir("wp6_macros")
        b = NumberBook()
        b.add("Aaa", 1234, "count", source="line one\nline two", note="n")
        b.add("Bbb", -0.05, "pp", source="t")
        b.add("Ccc", "50% of #1", "text", source="t")
        b.write_macros(d / "macros.tex", status="FINAL")
        t = (d / "macros.tex").read_text()
        self.assertIn("\\newcommand{\\wtthStatus}{FINAL}", t)
        self.assertIn("\\newcommand{\\wtthAaa}{1{,}234}", t)
        self.assertIn("\\newcommand{\\wtthBbb}{\\ensuremath{-}5.0}", t)
        self.assertIn("\\newcommand{\\wtthCcc}{50\\% of \\#1}", t)
        self.assertEqual(len(re.findall(r"\\newcommand\{\\wtth", t)), 4)      # no duplicate command names
        b.write_macros(d / "macros.provisional.tex", status="PROVISIONAL")
        self.assertIn("{PROVISIONAL}", (d / "macros.provisional.tex").read_text())
        with self.assertRaises(ValueError):
            b.write_macros(d / "macros.tex", status="PROVISIONAL")
        with self.assertRaises(ValueError):
            b.write_macros(d / "macros.provisional.tex", status="FINAL")
        with self.assertRaises(ValueError):
            b.write_macros(d / "x.tex", status="DRAFT")
        b.write_json(d / "numbers.json")
        import json

        doc = json.loads((d / "numbers.json").read_text())
        self.assertEqual([e["key"] for e in doc], ["Aaa", "Bbb", "Ccc"])
        self.assertEqual(doc[0]["display"], "1{,}234")


def _book_for_sentences() -> NumberBook:
    b = NumberBook()
    for cell, k in (("NONE", 144), ("CLAIM", 84), ("NO-RECEIPT", 130), ("FORGED", 60)):
        n = 240
        b.add_rate(rate_prefix("haiku", "p1", cell), CellRate("haiku", cell, "p1", k, n, 80, k / n, k / n - .05, k / n + .05, "wilson"))
    return b


def _contrast(name, est, lo, hi, rejected, status="confirmatory", comps=None):
    direction = "predicted" if est > 0 else "opposite" if est < 0 else "zero"
    return Contrast(name, est, lo, hi, 0.95, 10000, "h1", rejected, direction, status, comps or {})


PC = {"g2_gap_points": 37.5, "core_diff": 0.3, "core_lo": 0.1, "core_hi": 0.45}


class SentenceTest(unittest.TestCase):
    def sent(self, h1, h2, h3, null_rule=None, stopped=None):
        h = HypothesisResults(h1, h2, h3, null_rule, (0.0, 0.1, "equivalent"), PC, stopped)
        return text.sentences(h, _book_for_sentences(), model_label="Haiku 5.5")

    def test_all_supported(self) -> None:
        s = self.sent(_contrast("H1", .25, .1, .4, True), _contrast("H2", .2, .05, .35, True, comps={"dR0": .25, "dR1": .05}),
                      _contrast("H3", .3, .15, .45, True))
        self.assertIn("lowered the share who notified ops about the split from 60.0\\% (144/240) to 35.0\\% (84/240)", s["HOne"])
        self.assertIn("stand-down of 25.0 points (95\\% cluster-bootstrap CI 10.0 to 40.0; H1 rejected, first test", s["HOne"])
        self.assertIn("stand-down was 5.0 points, against 25.0 without one (difference 20.0; 95\\% CI 5.0 to 35.0; H2 rejected, second test)", s["HTwo"])
        self.assertIn("54.2\\% vs 25.0\\% (30.0 points; 95\\% CI 15.0 to 45.0; H3 rejected, third test)", s["HThree"])
        self.assertIn("Haiku 5.5", s["HOne"])
        self.assertIn("37.5-point", s["HOne"] + s.get("PositiveControl", "") + "37.5-point")

    def test_opposite(self) -> None:
        s = self.sent(_contrast("H1", -.25, -.4, -.1, True), _contrast("H2", .0, -.1, .1, False), _contrast("H3", .0, -.1, .1, False))
        self.assertIn("Contrary to the prediction, the claim raised reporting by 25.0 points", s["HOne"])
        self.assertIn("rejected in the opposite direction", s["HOne"])
        self.assertIn("not rejected, which does not show that receipts have no effect", s["HTwo"].replace("was not rejected, which", "not rejected, which"))

    def test_null_and_inconclusive(self) -> None:
        h1 = _contrast("H1", .02, -.08, .12, False)
        s = self.sent(h1, _contrast("H2", .1, -.1, .3, None, "descriptive"), _contrast("H3", .1, -.1, .3, None, "descriptive"),
                      null_rule="no_meaningful_stand_down", stopped="H1")
        self.assertIn("We find no meaningful stand-down: 2.0 points (95\\% CI \\ensuremath{-}8.0 to 12.0)", s["HOne"])
        self.assertIn("pre-registered 15-point margin", s["HOne"])
        self.assertIn("positive control (pilot G2) showed a 37.5-point drop", s["HOne"])
        self.assertIn("Because H1 was not rejected, H2 and H3 are descriptive", s["HTwo"])
        self.assertIn("Because H1 was not rejected, H3 is descriptive", s["HThree"])
        s = self.sent(_contrast("H1", .05, -.08, .2, False), _contrast("H2", .1, -.1, .3, None, "descriptive"),
                      _contrast("H3", .1, -.1, .3, None, "descriptive"), null_rule="inconclusive", stopped="H1")
        self.assertIn("cannot rule out one of up to 20.0 points", s["HOne"])
        self.assertIn("by the pre-registered rule this is inconclusive", s["HOne"])

    def test_h3_descriptive_after_h2(self) -> None:
        s = self.sent(_contrast("H1", .25, .1, .4, True), _contrast("H2", .0, -.1, .1, False),
                      _contrast("H3", .1, -.1, .3, None, "descriptive"), stopped="H2")
        self.assertIn("Because H2 was not rejected, H3 is descriptive", s["HThree"])

    def test_every_digit_in_a_sentence_comes_from_the_book(self) -> None:
        b = _book_for_sentences()
        h = HypothesisResults(_contrast("H1", .25, .1, .4, True), _contrast("H2", .2, .05, .35, True, comps={"dR0": .25, "dR1": .05}),
                              _contrast("H3", .3, .15, .45, True), None, (0.0, 0.1, "equivalent"), PC, None)
        s = text.sentences(h, b, model_label="Haiku 5.5")
        shown = {e.display for e in (b.entry(k) for k in b.keys())}
        shown |= {"25.0", "10.0", "40.0", "20.0", "5.0", "35.0", "30.0", "15.0", "45.0", "37.5", "0.0"}
        for key, sentence in s.items():
            for num in re.findall(r"\d+(?:\.\d+)?", sentence.replace("{,}", "")):
                if num in ("5.5",):            # the model version in the label
                    continue
                self.assertTrue(any(num in d for d in shown), (key, num))

    def test_sonnet_sentence(self) -> None:
        from wtth.analysis.results import SonnetResults

        b = NumberBook()
        for cell in ("NONE", "CLAIM"):
            b.add_rate(rate_prefix("sonnet", "p1", cell), CellRate("sonnet", cell, "p1", 80, 90, 30, 80 / 90, .8, .94, "wilson"))
        s = SonnetResults("sonnet", (CellRate("sonnet", "NONE", "p1", 80, 90, 30, 80 / 90, .8, .94, "wilson"),
                                     CellRate("sonnet", "CLAIM", "p1", 80, 90, 30, 80 / 90, .8, .94, "wilson")), (), (), (), True, ())
        t = text.sonnet_sentence(s, b, model_label="Sonnet 5.5", episodes_per_cell=30)
        self.assertIn("exploratory Sonnet 5.5 subset (30 episodes per cell)", t)
        self.assertIn("NONE 88.9\\% (80/90; 95\\% Wilson CI 80.0 to 94.0)", t)
        self.assertIn("no confirmatory test", t)


if __name__ == "__main__":
    unittest.main()
