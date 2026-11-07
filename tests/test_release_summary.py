"""scripts/release_summary.py reproduces the headline numbers in paper/generated/analysis/numbers.json."""
from __future__ import annotations

import json
import types
import unittest

from tests.util import REPO

# Exec the source directly: importing it would write scripts/__pycache__, which the repo-scan tests do not expect.
RS = types.ModuleType("release_summary")
RS.__file__ = str(REPO / "scripts" / "release_summary.py")
exec(compile((REPO / "scripts" / "release_summary.py").read_text(encoding="utf-8"), "release_summary.py", "exec"), RS.__dict__)

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


class ReleaseFollowUpTwo(unittest.TestCase):
    """The D-028 follow-up 2 rows of data/release match numbers.json: shifts, P1 and P2 per cell, and the
    lookup_escalation use read from the tool_calls column."""

    @classmethod
    def setUpClass(cls):
        import csv

        cls.nums = {r["key"]: r["value"] for r in json.loads(
            (REPO / "paper/generated/analysis/numbers.json").read_text(encoding="utf-8"))}
        rel = REPO / "data" / "release"
        with (rel / "outcomes_per_witness.csv").open(newline="", encoding="utf-8") as f:
            cls.w = [r for r in csv.DictReader(f) if r["phase"] == "exp2"]
        with (rel / "outcomes_per_shift.csv").open(newline="", encoding="utf-8") as f:
            cls.e = [r for r in csv.DictReader(f) if r["phase"] == "exp2"]

    def test_p1_p2_per_cell(self):
        self.assertEqual(self.nums["ExpTwoStatus"], "complete")
        for cell, tok in (("X-FORGED-CHECK", "XForgedCheck"), ("X-FORGED-CONC", "XForgedConc")):
            w = [r for r in self.w if r["cell"] == cell]
            e = [r for r in self.e if r["cell"] == cell]
            self.assertEqual((sum(int(r["p1"]) for r in w), len(w)),
                             (self.nums[f"ExpTwo{tok}POneK"], self.nums[f"ExpTwo{tok}POneN"]), cell)
            self.assertEqual((sum(1 for r in e if r["y"] == "0"), len(e)),
                             (self.nums[f"ExpTwo{tok}PTwoK"], self.nums[f"ExpTwo{tok}PTwoN"]), cell)
            self.assertEqual(len(e), self.nums[f"ExpTwo{tok}Eps"])

    def test_lookup_use(self):
        """tool_calls holds the server-recorded calls; the analysis also counts client-side uses the server never
        recorded (ExpTwoLookupCallsClientK), none here."""
        self.assertEqual(self.nums["ExpTwoLookupCallsClientK"], 0)
        check = [r for r in self.w if r["cell"] == "X-FORGED-CHECK"]
        calls = [int(t.split(":")[1]) for r in check for t in r["tool_calls"].split("|") if t.startswith("lookup_escalation:")]
        self.assertEqual((sum(1 for r in check if "lookup_escalation:" in r["tool_calls"]), len(check), sum(calls)),
                         (self.nums["ExpTwoLookupWitnessesK"], self.nums["ExpTwoLookupWitnessesN"],
                          self.nums["ExpTwoLookupCallsN"]))
        self.assertFalse(any("lookup_escalation" in r["tool_calls"] for r in self.w if r["cell"] == "X-FORGED-CONC"))


if __name__ == "__main__":
    unittest.main()
