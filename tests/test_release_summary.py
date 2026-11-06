"""scripts/release_summary.py reproduces the headline numbers in paper/generated/analysis/numbers.json."""
from __future__ import annotations

import importlib.util
import json
import unittest

from tests.util import REPO

_spec = importlib.util.spec_from_file_location("release_summary", REPO / "scripts" / "release_summary.py")
RS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RS)

TOKENS = {"B-NONE": "BNone", "CLAIM": "Claim", "CONCERN": "Concern", "FORGED": "Forged", "NEG": "Neg",
          "NO-RECEIPT": "NoReceipt", "NONE": "None", "NONE-R": "NoneR", "POLICY": "Policy", "POS": "Pos", "RECEIPT": "Receipt"}


class ReleaseSummary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nums = {r["key"]: r["value"] for r in json.loads(
            (REPO / "paper/generated/analysis/numbers.json").read_text(encoding="utf-8"))}
        cls.s = RS.summarize(REPO / "data" / "release")

    def test_p1_per_cell(self):
        self.assertEqual(set(self.s["p1"]), set(TOKENS))
        for cell, tok in TOKENS.items():
            k, n = self.s["p1"][cell]
            self.assertEqual((k, n), (self.nums[f"HaikuPOne{tok}K"], self.nums[f"HaikuPOne{tok}N"]), cell)

    def test_p2_per_cell(self):
        self.assertEqual(self.s["p2"]["FORGED"], [4, 50])
        for cell, (k, n) in self.s["p2"].items():
            tok = TOKENS[cell]
            self.assertEqual((k, n), (self.nums[f"HaikuPTwo{tok}K"], self.nums[f"HaikuPTwo{tok}N"]), cell)

    def test_h3_point_difference(self):
        self.assertEqual(round(self.s["h3_delta_points"], 1), 23.8)
        self.assertAlmostEqual(self.s["h3_delta_points"] / 100.0, self.nums["HaikuHThreeDelta"], places=6)


if __name__ == "__main__":
    unittest.main()
