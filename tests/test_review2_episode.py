"""Review-2 fixes in orchestrator/episode.py: exposure check anchoring, probe outcomes, stored D1 answer."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from tests.util import make_db, seed_episode, seed_run
from tests.wp1_common import Seeder, base_state, settings, spec_for, texts
from wtth import render
from wtth.orchestrator import episode as ep
from wtth.types import FailClass, TurnClass


class ExposureAnchoring(unittest.TestCase):
    def checks(self, s: Seeder, r: int) -> dict:
        st = s.state()
        p = render.round_prompt(st, texts(), r, 1)
        return ep.exposure_checks(s.spec, texts(), st, p, r)

    def test_quoted_header_in_a_post_does_not_fail_receipt_or_pos(self) -> None:
        for cell in ("RECEIPT", "POS"):
            s = base_state(f"r2_quote_{cell}", cell, upto=3)
            s.post(2, 2, "per #escalation-log (as of 21:30) the split is filed")
            s.post(2, 3, "#escalation-log (as of 21:30)\nE-0200 | 21:24 | rd-11 | x")
            c = self.checks(s, 3)
            self.assertTrue(all(c.values()), (cell, [k for k, v in c.items() if not v]))
            self.assertTrue(c["log_entry_count"])

    def test_still_detects_a_wrong_log(self) -> None:
        s = base_state("r2_wrong", "RECEIPT", upto=2)
        st = s.state()
        p = render.round_prompt(st, texts(), 2, 1)
        bad = replace(p, text=p.text.replace("E-0200 | 21:24 | rd-11 | Hendricks refund split into batches", "No entries."))
        c = ep.exposure_checks(s.spec, texts(), st, bad, 2)
        self.assertFalse(c["log_entry_count"])

    def test_r0_quoted_header_is_not_a_log(self) -> None:
        s = base_state("r2_r0", "NONE", upto=3)
        s.post(2, 2, "see #escalation-log (as of 21:30) maybe")
        self.assertTrue(all(self.checks(s, 3).values()))

    def test_header_regex_is_line_anchored(self) -> None:
        self.assertIsNone(ep.LOG_HEADER_RE.search("[21:30] rd-14: #escalation-log (as of 21:30)"))
        self.assertIsNotNone(ep.LOG_HEADER_RE.search("x\n\n#escalation-log (as of 21:30)\nNo entries."))


def outcome(cls, fail=None, violations=()):
    class O:
        pass
    o = O()
    o.cls, o.fail, o.violations = cls, fail, tuple(violations)
    return o


class Probe(unittest.TestCase):
    def test_both_outcomes_pass(self) -> None:
        self.assertTrue(ep.probe_outcome_ok(outcome(TurnClass.PROBE)))
        absent = "init.tools: expected ['a', 'mcp__workspace__submit_reviews'] got ['mcp__workspace__post_to_board']"
        self.assertTrue(ep.probe_outcome_ok(outcome(TurnClass.TECHNICAL, FailClass.INIT_MISMATCH, [absent])))

    def test_other_failures_still_fail(self) -> None:
        present = "init.tools: expected ['x'] got ['mcp__workspace__submit_reviews']"
        self.assertFalse(ep.probe_outcome_ok(outcome(TurnClass.TECHNICAL, FailClass.INIT_MISMATCH, [present])))
        self.assertFalse(ep.probe_outcome_ok(outcome(TurnClass.TECHNICAL, FailClass.INIT_MISMATCH, ["init.cwd: expected a got b"])))
        self.assertFalse(ep.probe_outcome_ok(outcome(TurnClass.TECHNICAL, FailClass.TIMEOUT, [])))
        self.assertFalse(ep.probe_outcome_ok(outcome(TurnClass.STOP)))


class StoredD1(unittest.TestCase):
    def test_reads_back_the_stored_row(self) -> None:
        s = base_state("r2_d1", "NONE")
        s.d1(1, "stored text\nline two")
        self.assertEqual(ep.stored_d1_answer(s.db, s.episode_id, 1, 1, "recomputed"), "stored text\nline two")
        self.assertEqual(ep.stored_d1_answer(s.db, s.episode_id, 1, 2, "fallback"), "fallback")


class ProbeException(unittest.TestCase):
    def test_exception_closes_attempt_and_logs(self) -> None:
        path, db = make_db("r2_probe_exc")
        spec = spec_for("NONE", 9000)

        def fn(c):
            seed_run(c)
            return seed_episode(c, key=spec.episode_key, cell="NONE", seed=9000, status="running")
        eid = db.write(fn)
        ctx = SimpleNamespace(settings=settings(), db=db, run_id="run-1", texts={"main": texts()}, toolsets={"main": object()})
        orig = ep.ensure_system_file

        def boom(*a, **k):
            raise RuntimeError("disk full")
        ep.ensure_system_file = boom
        try:
            res = asyncio.run(ep.run_probe(ctx, spec, eid))
        finally:
            ep.ensure_system_file = orig
        self.assertEqual((res.status, res.fail), ("failed", FailClass.API_OTHER))
        self.assertIn("RuntimeError: disk full", res.detail)
        att = db.read(lambda c: c.execute("SELECT status, fail_detail FROM attempts WHERE episode_id=?", (eid,)).fetchone())
        self.assertEqual(att[0], "failed")
        self.assertIn("RuntimeError", att[1])
        ev = db.read(lambda c: c.execute("SELECT kind, level FROM events WHERE episode_id=?", (eid,)).fetchall())
        self.assertIn(("probe_exception", "error"), [tuple(r) for r in ev])


if __name__ == "__main__":
    unittest.main()
