"""Gate, ops, snapshot, frontier, D1-pooled and blind macros of the NumberBook (plan 49 2.7)."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from wtth.analysis import register
from wtth.analysis.numbers import NumberBook
from wtth.analysis.results import Diff

GATES = [
    {"id": 1, "ts": 1.0, "gate": "G0", "decision": "PASS", "level": None, "numbers": {"step_cap": 8}},
    {"id": 2, "ts": 2.0, "gate": "G1", "decision": "CEILING", "level": "L3",
     "numbers": {"L2": {"share": 1.0}, "L3": {"share": 1.0}, "frozen_level": "L3"}},
    {"id": 3, "ts": 3.0, "gate": "G2", "decision": "PASS", "level": "L3",
     "numbers": {"action": "GO", "d": 10, "gap_points": 62.5, "verdict": "PASS"}},
]


def _manifest(cap=8):
    return json.dumps({"max_turns": {"witness": cap}})


RUNS = [
    {"run_id": "g2", "phase": "g2", "mode": "live", "started_at": 1.0, "ended_at": 2.0, "manifest_json": _manifest()},
    {"run_id": "c1", "phase": "core", "mode": "live", "started_at": 1793922397.0, "ended_at": 1793928437.0,
     "manifest_json": _manifest(8)},
]
HEALTH = ["[19:26:22 UTC] phase=g0 done=0/5 K=4/15 procs=0", "[20:00:00 UTC] phase=core done=1/9 K=12/15 procs=3",
          "[20:30:00 UTC] phase=core done=4/9 K=15/15 procs=3", "[21:00:00 UTC] phase=sonnet K=13/15"]


class RunFacts(unittest.TestCase):
    def test_gates(self):
        b = NumberBook()
        register.register_gates(b, GATES)
        self.assertEqual(b.value("GateFrozenLevel"), "L3")
        self.assertEqual(b.fmt("GateGOnePOneAtFPct"), "100.0")
        self.assertEqual(b.value("GateGTwoResult"), "pass")
        self.assertEqual(b.value("GateDecision"), "GO-CEIL")

    def test_g2_caveat(self):
        g = [dict(r) for r in GATES]
        g[2]["numbers"] = {"action": "GO-CAVEAT"}
        b = NumberBook()
        register.register_gates(b, g)
        self.assertEqual(b.value("GateGTwoResult"), "caveat")

    def test_ops(self):
        b = NumberBook()
        register.register_ops(b, RUNS, HEALTH)
        self.assertTrue(b.value("OpsCoreStart").endswith(" UTC"))
        self.assertEqual(b.fmt("OpsStepCap"), "8")
        self.assertEqual(b.fmt("OpsKMax"), "15")        # sonnet and g0 lines are ignored
        self.assertAlmostEqual(b.value("OpsCoreWallHours"), 6040 / 3600)

    def test_ops_no_core_adds_nothing(self):
        b = NumberBook()
        register.register_ops(b, RUNS[:1], HEALTH)
        self.assertEqual(len(b), 0)

    def test_snapshot(self):
        b = NumberBook()
        register.register_snapshot(b, "0123456789abcdef" * 4)
        self.assertEqual(b.value("SnapshotShaShort"), "0123456789ab")

    def test_excl_and_frontier(self):
        b = NumberBook()
        register.add_diff(b, "Haiku", Diff("P2:POLICY-NO-RECEIPT", "p2", 0.1, 0.02, 0.2, "newcombe"))
        register.add_diff(b, "Haiku", Diff("P2:NONE-CLAIM", "p2", 0.0, -0.05, 0.05, "newcombe"))
        self.assertEqual(b.value("HaikuDiffPTwoNoneMinusClaimExcl"), "no")
        self.assertEqual(b.value("HaikuDiffPTwoPolicyMinusNoReceiptExcl"), "yes")
        b.add("HaikuDescAlertLoadPolicyMinusReceiptExcl", "no", "verdict", source="t")
        register.add_frontier_flags(b, "Haiku")
        self.assertEqual(b.value("HaikuFrontierSaferExcl"), "yes")
        self.assertEqual(b.value("HaikuFrontierCheaperExcl"), "no")

    def test_excl_missing_interval_absent(self):
        b = NumberBook()
        register.add_diff(b, "Haiku", Diff("P2:NONE-CLAIM", "p2", None, None, None, "newcombe"))
        self.assertNotIn("HaikuDiffPTwoNoneMinusClaimExcl", b)

    def test_pooled_d1(self):
        b = NumberBook()
        rs = [SimpleNamespace(outcome="d1:test_or_simulation", k=1, n=10),
              SimpleNamespace(outcome="d1:test_or_simulation", k=2, n=30),
              SimpleNamespace(outcome="d1:live", k=9, n=40)]
        register.add_pooled_d1(b, "Haiku", rs)
        self.assertEqual(b.fmt("HaikuDOneTestAllPct"), "7.5")

    def test_blind_zero_when_declined(self):
        b = NumberBook()
        register.register_judge(b, None, {"blind": {"status": "declined", "by_type": {}}})
        self.assertEqual(b.fmt("BlindN"), "0")


if __name__ == "__main__":
    unittest.main()
