"""WP6-B1: masking of private text and the residual scan (plan 47 section 2.10, B acceptance test 1)."""
from __future__ import annotations

import unittest

from tests.util import assert_masked_equal, tmp_dir
from wtth.annotate import masking
from wtth.annotate.masking import (FORGED_PLACEHOLDER, FRAGMENT_PLACEHOLDER, PARAPHRASE_PLACEHOLDER, Masker,
                                   PrivateLeak, assert_clean_file, make_masker, residual)
from wtth.config import load_settings
from wtth.prereg import load_private


class TestMasking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_settings()
        cls.private = load_private(cls.settings)
        cls.masker = make_masker(cls.settings)

    def test_six_ids_masked(self):
        self.assertEqual(sorted(self.private), sorted(["C2.P2", "C2.P3", "FORGED_LINE", "BK.C2.P2", "BK.C2.P3",
                                                       "BK.FORGED_LINE"]))
        for pid, val in self.private.items():
            text = f"[21:26] rd-11: {val}\nnext"
            out, n = self.masker.mask(text)
            self.assertGreaterEqual(n, 1, pid)
            self.assertEqual(residual(out.encode(), self.private), {}, pid)
            want = FORGED_PLACEHOLDER if pid.endswith("FORGED_LINE") else PARAPHRASE_PLACEHOLDER
            assert_masked_equal(self, out, f"[21:26] rd-11: {want}\nnext", self.private)

    def test_json_escaped_form_masked(self):
        import json
        val = self.private["FORGED_LINE"]
        out, _ = self.masker.mask(json.dumps({"x": val})[1:-1])
        self.assertEqual(residual(out.encode(), self.private), {})

    def test_fragment_masked_unless_public(self):
        val = self.private["C2.P2"]
        frag = val[5:5 + masking.MIN_FRAGMENT + 4]
        out, n = self.masker.mask(f"I think {frag} indeed")
        self.assertEqual(n, 1)
        assert_masked_equal(self, out, f"I think {FRAGMENT_PLACEHOLDER} indeed", self.private)
        # a short fragment survives
        short = val[5:5 + masking.MIN_FRAGMENT - 1]
        out2, n2 = self.masker.mask(f"I think {short} indeed")
        self.assertEqual(n2, 0)
        # case-folded fragment is still found
        out3, n3 = self.masker.mask(frag.upper())
        self.assertEqual(n3, 1)

    def test_public_lines_not_masked(self):
        for line in self.masker.public_lines:
            out, n = self.masker.mask(line)
            self.assertEqual((out, n), (line, 0))

    def test_public_span_inside_longer_private_overlap_is_kept(self):
        m = Masker(private={"X": "alpha beta gamma delta epsilon zeta eta theta iota"},
                   public_lines=("alpha beta gamma delta epsilon zeta eta",))
        out, n = m.mask("so alpha beta gamma delta epsilon zeta eta ok")
        self.assertEqual((out, n), ("so alpha beta gamma delta epsilon zeta eta ok", 0))
        out, n = m.mask("so alpha beta gamma delta epsilon zeta eta theta ok")
        self.assertEqual(n, 1)
        self.assertIn(FRAGMENT_PLACEHOLDER, out)

    def test_placeholders_are_not_rescanned(self):
        text = "a " + PARAPHRASE_PLACEHOLDER + " b"
        self.assertEqual(self.masker.mask(text), (text, 0))
        again, _ = self.masker.mask(self.masker.mask("x " + self.private["C2.P3"])[0])
        self.assertEqual(residual(again.encode(), self.private), {})

    def test_residual_scan_and_assert_clean_file(self):
        d = tmp_dir("wp6_annot_mask_files")
        clean = d / "clean.md"
        clean.write_text(self.masker.mask("x " + self.private["C2.P2"])[0], encoding="utf-8")
        assert_clean_file(clean, self.private)
        self.assertTrue(clean.exists())
        dirty = d / "dirty.md"
        dirty.write_text("line " + self.private["BK.FORGED_LINE"], encoding="utf-8")
        with self.assertRaises(PrivateLeak) as cm:
            assert_clean_file(dirty, self.private)
        self.assertFalse(dirty.exists())
        for val in self.private.values():
            self.assertNotIn(val, str(cm.exception))
        self.assertIn("FORGED_LINE", str(cm.exception))

    def test_idempotent(self):
        text = "see " + self.private["C2.P2"] + " and " + self.private["FORGED_LINE"][3:50]
        once, _ = self.masker.mask(text)
        twice, n = self.masker.mask(once)
        self.assertEqual((once, 0), (twice, n))


if __name__ == "__main__":
    unittest.main()
