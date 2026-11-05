"""T5.2: G0 audit on a fake G0 run (real server through the fake, synthetic bodies/): one line per criterion."""
from __future__ import annotations

import json
import unittest

from tests.util import DUMMY_KEY
from tests.wp5_support import G0Builder, Rig, settings_for
from wtth import audit


def good_run(name: str) -> tuple[Rig, G0Builder]:
    rig = Rig(name, world="R1")
    b = G0Builder(rig)
    b.turn("witness", round=2, slot=1, sender="rd-14")
    b.turn("witness", round=2, slot=2, sender="rd-21", extra_env=None)
    b.turn("witness", round=3, slot=1, sender="rd-14", behavior={"default": {"witness": {"calls": [
        {"tool": "submit_reviews", "args": "auto"}, {"tool": "post_to_board", "args": {"message": "x"}}]}}})
    b.turn("debrief", ttype="d1")
    b.turn("debrief", ttype="d2", prompt="The shift is over. x\n\nEarlier question: a\nYour answer: b\n\nq2")
    b.turn("witness", ttype="probe", round=1, slot=1, behavior={"default": {"mode": "permission_denial", "denied_tool": "submit_reviews",
                                                                  "witness": {"calls": []}}})
    return rig, b


def run_audit(rig: Rig, **kw):
    kw.setdefault("startup_check", lambda s, m, p: [])
    kw.setdefault("scan_roots", [rig.root / "turns"])
    kw.setdefault("scan_git", False)
    return audit.audit_g0(settings_for(rig), rig.db, "run-1", **kw)


class TestGoodRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig, _b = good_run("wp5_audit_good")
        cls.rep = run_audit(cls.rig)

    def test_one_line_per_criterion(self):
        self.assertEqual([c.id for c in self.rep.checks], [f"G0-{i}" for i in range(1, 16)])
        self.assertEqual(len(self.rep.lines()), 15)
        for ln in self.rep.lines():
            self.assertRegex(ln, r"^G0-\d+ (PASS|FAIL|REVIEW|INFO|MANUAL) ")

    def test_statuses(self):
        want = {"G0-1": "MANUAL", "G0-2": "PASS", "G0-3": "PASS", "G0-4": "PASS", "G0-5": "PASS", "G0-6": "PASS",
                "G0-7": "PASS", "G0-8": "PASS", "G0-9": "PASS", "G0-10": "PASS", "G0-11": "PASS", "G0-12": "PASS",
                "G0-13": "INFO", "G0-14": "INFO", "G0-15": "PASS"}
        got = {c.id: c.status for c in self.rep.checks}
        self.assertEqual(got, want, "\n".join(self.rep.lines()))
        self.assertTrue(self.rep.ok)

    def test_measurements_and_proposal(self):
        m = self.rep.measurements["per_model"]["claude-haiku-5-5"]
        self.assertEqual(m["n_turns"], 5)  # probe excluded
        self.assertEqual(m["num_turns_max"], 3)
        self.assertEqual(self.rep.measurements["proposal"]["max_turns_witness"], 8)
        self.assertEqual(self.rep.measurements["proposal"]["K_target"], 15)  # 240 MB processes: memory not binding

    def test_constants_recorded(self):
        self.assertEqual(self.rep.constants["sdk_identity"], "You are a Claude agent, built on Anthropic's Claude Agent SDK.")
        self.assertIn("billing", self.rep.constants["billing_header"])

    def test_write_md_is_masked_and_complete(self):
        out = self.rig.root / "reports" / "g0_run-1.md"
        self.rep.write_md(out)
        text = out.read_text(encoding="utf-8")
        for i in range(1, 16):
            self.assertIn(f"G0-{i} ", text.replace("| ", ""))
        from wtth.prereg import load_private

        for pid, val in load_private(self.rep.settings).items():
            self.assertFalse(val in text, pid)
        self.assertNotIn(DUMMY_KEY, text)

    def test_unknown_run(self):
        with self.assertRaises(ValueError):
            audit.audit_g0(settings_for(self.rig), self.rig.db, "nope", startup_check=lambda *a: [])


class TestFailures(unittest.TestCase):
    def status(self, rep, cid):
        return rep.get(cid).status

    def test_body_leak_fails_g0_6(self):
        rig = Rig("wp5_audit_leak", world="R1")
        G0Builder(rig).turn("witness", behavior={"default": {"bodies": {"leak": "contact someone@example.org"}}})
        rep = run_audit(rig)
        self.assertEqual(self.status(rep, "G0-6"), "FAIL")
        self.assertIn("forbidden", rep.get("G0-6").detail)

    def test_wrapped_reply_fails_g0_5(self):
        rig = Rig("wp5_audit_wrap", world="R1")
        def tamper(uses):
            uses[0]["result_text"] = '{"result":"Reviews recorded."}'
        G0Builder(rig).turn("witness", tamper=tamper)
        rep = run_audit(rig)
        self.assertEqual(self.status(rep, "G0-5"), "FAIL")

    def test_wrong_reply_text_fails_g0_5(self):
        rig = Rig("wp5_audit_wrong", world="R1")
        def tamper(uses):
            uses[0]["result_text"] = "Reviews recorded"
        G0Builder(rig).turn("witness", tamper=tamper)
        self.assertEqual(self.status(run_audit(rig), "G0-5"), "FAIL")

    def test_stderr_noise_fails_g0_11(self):
        rig = Rig("wp5_audit_noise", world="R1")
        G0Builder(rig).turn("witness", behavior={"default": {"mode": "stderr_noise"}})
        self.assertEqual(self.status(run_audit(rig), "G0-11"), "FAIL")

    def test_refusals_fail_or_review_g0_9(self):
        rig = Rig("wp5_audit_ref", world="R1")
        b = G0Builder(rig)
        for s in (1, 2, 3):
            b.turn("witness", slot=s, behavior={"default": {"mode": "refusal"}})
        b.turn("debrief")
        rep = run_audit(rig)
        self.assertEqual(self.status(rep, "G0-9"), "FAIL")
        rig3 = Rig("wp5_audit_ref3", world="R1")
        b3 = G0Builder(rig3)
        b3.turn("witness", behavior={"default": {"mode": "refusal"}})
        b3.turn("witness")
        b3.turn("witness")
        b3.turn("witness")
        for _ in range(4):
            b3.turn("debrief")
        # witness 1/4 = 25% , all 1/8 = 12.5% -> both over 10%
        self.assertEqual(self.status(run_audit(rig3), "G0-9"), "FAIL")
        rig4 = Rig("wp5_audit_ref4", world="R1")
        b4 = G0Builder(rig4)
        b4.turn("witness", behavior={"default": {"mode": "refusal"}})
        for _ in range(4):
            b4.turn("witness")
        for _ in range(6):
            b4.turn("debrief")
        # witness 1/5 = 20%, all 1/11 = 9.1% -> measures disagree
        self.assertEqual(self.status(run_audit(rig4), "G0-9"), "REVIEW")

    def test_violations_fail_g0_4(self):
        rig = Rig("wp5_audit_viol", world="R1")
        G0Builder(rig).turn("witness", violations='["init.tools: expected [] got [x]"]')
        self.assertEqual(self.status(run_audit(rig), "G0-4"), "FAIL")

    def test_reconcile_mismatch_fails_g0_7(self):
        rig = Rig("wp5_audit_recon", world="R1")
        def tamper(uses):
            uses[0]["input_json"] = '{"reviews":[]}'
        G0Builder(rig).turn("witness", tamper=tamper)
        self.assertEqual(self.status(run_audit(rig), "G0-7"), "FAIL")

    def test_malformed_calls_fail_g0_8(self):
        rig = Rig("wp5_audit_wf", world="R1")
        bad = {"default": {"witness": {"calls": [
            {"tool": "submit_reviews", "args": {"reviews": [{"ticket_id": "T-1201", "decision": "maybe"}]}}]}}}
        b = G0Builder(rig)
        b.turn("witness", behavior=bad)
        b.turn("witness", behavior=bad)
        self.assertEqual(self.status(run_audit(rig), "G0-8"), "FAIL")

    def test_key_in_runs_fails_g0_10(self):
        rig, _b = good_run("wp5_audit_key")
        (rig.root / "turns" / "t001" / "oops.txt").write_text("k=" + DUMMY_KEY, encoding="utf-8")
        rep = run_audit(rig)
        self.assertEqual(self.status(rep, "G0-10"), "FAIL")
        self.assertNotIn(DUMMY_KEY, rep.get("G0-10").detail)

    def test_probe_that_reached_server_fails_g0_3(self):
        rig = Rig("wp5_audit_probe", world="R1")
        G0Builder(rig).turn("witness", ttype="probe")  # tools listed and submit_reviews executed
        self.assertEqual(self.status(run_audit(rig), "G0-3"), "FAIL")

    def test_no_probe_fails_g0_3(self):
        rig = Rig("wp5_audit_noprobe", world="R1")
        G0Builder(rig).turn("witness")
        self.assertEqual(self.status(run_audit(rig), "G0-3"), "FAIL")

    def test_exposure_failures_g0_12(self):
        rig = Rig("wp5_audit_expo", world="R1")
        G0Builder(rig).turn("witness", prompt="Round 2\nYOUR TICKETS\nT-1201 · a\nT-1202 · b\nT-1203 · c\n\nno post, no log")
        self.assertEqual(self.status(run_audit(rig), "G0-12"), "FAIL")
        rig0 = Rig("wp5_audit_expo0", world="R0")
        G0Builder(rig0).turn("witness")  # R0 world but the prompt shows a log block
        self.assertEqual(self.status(run_audit(rig0), "G0-12"), "FAIL")

    def test_remote_settings_g0_15(self):
        rig, _b = good_run("wp5_audit_remote")
        cfg = rig.slot / "config"
        (cfg / "remote-settings.json").write_text("{}")
        self.assertEqual(self.status(run_audit(rig), "G0-15"), "PASS")
        (cfg / "remote-settings.json").write_text(json.dumps({"x": 1}))
        self.assertEqual(self.status(run_audit(rig), "G0-15"), "FAIL")

    def test_startup_problems_fail_g0_2(self):
        rig = Rig("wp5_audit_start", world="R1")
        G0Builder(rig).turn("witness")
        self.assertEqual(self.status(run_audit(rig, startup_check=lambda s, m, p: ["bad hash"]), "G0-2"), "FAIL")

    def test_cost_projection_over_cap_is_review(self):
        rig = Rig("wp5_audit_cost", world="R1")
        G0Builder(rig).turn("witness", behavior={"default": {"cost": {"claude-haiku-5-5": 5.0}}})
        rep = run_audit(rig)
        self.assertEqual(self.status(rep, "G0-14"), "REVIEW")


if __name__ == "__main__":
    unittest.main()
