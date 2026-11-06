"""WP6-A acceptance test 4: one judge call through the fake client; modes, argv, env, ledger."""
from __future__ import annotations

import asyncio
import json
import unittest

from tests.util import DUMMY_KEY
from tests.wp6_fakejudge import Rig, answer_json, default_labels, valid_answer
from wtth.config import CONFIGS_DIR, env_allowlist
from wtth.judge import call as C
from wtth.judge import items as IM
from wtth.judge import runner as R
from wtth.judge.types import BudgetBinds, JudgeStop
from wtth.runner import invocation


def run(coro):
    return asyncio.run(coro)


class CallBase(unittest.TestCase):
    overrides: dict = {}

    def setUp(self):
        self.rig = Rig(self.id().split(".")[-1][:40], dict(self.overrides))
        self.s = self.rig.settings
        self.ctx = C.make_context(self.s, run_id="run-t", prompt_version="v1", k=2)
        self.items = IM.ensure_held_out(self.s, self.ctx.jdb, "L2")
        self.item = self.items[0]

    def batch(self, items=None):
        return C.register_batch(self.ctx, items or [self.item], kind="first", tier=None, subtier=None)

    def calls(self, item=None):
        item = item or self.item
        return self.ctx.jdb.read(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM calls WHERE item_id=? ORDER BY launched_at", (item.item_id,))])

    def label(self, item=None):
        item = item or self.item
        return self.ctx.jdb.read(lambda c: c.execute(
            "SELECT * FROM judge_labels WHERE item_id=?", (item.item_id,)).fetchone())

    def events(self, kind):
        return self.ctx.jdb.read(lambda c: c.execute("SELECT * FROM jevents WHERE kind=?", (kind,)).fetchall())

    def judge(self, item=None):
        item = item or self.item
        bid = self.batch([item])
        return run(R.judge_item(self.ctx, item, batch_id=bid, cancel=asyncio.Event()))

    def rig_rule(self, text=None, **extra):
        self.rig.rule(self.item, text, **extra)
        self.rig.write()


class CallBasics(CallBase):
    def test_request_shape_and_argv(self):
        req = C.build_request(self.ctx, self.item, "ab" * 16)
        self.assertEqual((req.episode_id, req.attempt, req.turn_id), (0, 0, "ab" * 16))
        self.assertEqual(req.turn_type.value, "judge")
        self.assertIsNone(req.toolset)
        self.assertIsNone(req.identity)
        self.assertEqual((req.max_turns, req.max_budget_usd, req.timeout_s), (2, 0.10, self.s.timeouts.turn_s))
        self.assertEqual(req.model, "claude-sonnet-5-5")
        self.assertEqual(str(req.turn_dir), str(self.ctx.calls_dir / "ab" / ("ab" * 16)))
        argv = invocation.build_argv(self.s, req, None)
        self.assertEqual(argv, [
            str(self.s.paths.binary), "-p", req.prompt.text, "--bare", "--model", "claude-sonnet-5-5", "--effort",
            "low", "--max-turns", "2", "--max-budget-usd", "0.10", "--system-prompt-file", str(self.ctx.system_path),
            "--tools", "", "--disable-slash-commands", "--setting-sources", "", "--settings",
            str(self.s.paths.cc_settings), "--strict-mcp-config", "--permission-mode", "dontAsk",
            "--no-session-persistence", "--output-format", "stream-json", "--verbose"])
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--allowedTools", argv)
        self.assertIn(f"id: {self.item.item_id}", req.prompt.text)

    def test_valid_call_persists_argv_env_ledger(self):
        self.rig_rule(valid_answer(self.item))
        lab = self.judge()
        self.assertEqual((lab.status, lab.n_asks), ("valid", 1))
        (row,) = self.calls()
        argv = json.loads(row["argv_redacted_json"])
        self.assertTrue(argv[2].startswith("<prompt sha256="))
        self.assertEqual(argv[3:12], ["--bare", "--model", "claude-sonnet-5-5", "--effort", "low", "--max-turns", "2",
                                      "--max-budget-usd", "0.10"])
        base, _audit = env_allowlist(CONFIGS_DIR / "env_allowlist.json")
        want = sorted(set(base) | {"HOME", "TMPDIR", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"})
        self.assertEqual(json.loads(row["env_names_json"]), want)
        self.assertNotIn(DUMMY_KEY, json.dumps(row, default=str))
        self.assertEqual((row["class"], row["parse_ok"], row["quote_ok"], row["ask_no"], row["try_no"]),
                         ("ok", 1, 1, 1, 1))
        self.assertIsNotNone(row["ended_at"])
        ledger = self.ctx.jdb.read(lambda c: c.execute("SELECT * FROM judge_ledger").fetchall())
        self.assertEqual(len(ledger), 1)
        self.assertEqual((ledger[0]["tier"], ledger[0]["item_type"], ledger[0]["estimated"]), ("HO", "board_post", 0))
        self.assertAlmostEqual(ledger[0]["cost_usd"], 0.006)
        self.assertTrue((self.ctx.calls_dir / row["call_id"][:2] / row["call_id"] / "stream.jsonl").is_file())

    def test_closed_call_rows_are_immutable(self):
        self.rig_rule(valid_answer(self.item))
        self.judge()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.ctx.jdb.write(lambda c: c.execute("UPDATE calls SET exit_code=9"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.ctx.jdb.write(lambda c: c.execute("DELETE FROM judge_ledger"))

    def test_bad_output_then_reask_identical_prompt(self):
        self.rig_rule("not json at all")
        lab = self.judge()
        self.assertEqual(lab.status, "invalid")
        cs = self.calls()
        self.assertEqual([c["ask_no"] for c in cs], [1, 2])
        self.assertEqual(cs[0]["user_sha256"], cs[1]["user_sha256"])
        self.assertEqual(self.label()["n_asks"], 2)
        self.assertIsNone(self.label()["labels_json"])
        self.assertEqual(len(self.events("invalid")), 1)


class FakeModes(CallBase):
    def test_refusal_not_reasked(self):
        self.rig_rule(valid_answer(self.item), mode="refusal")
        lab = self.judge()
        self.assertEqual(lab.status, "refused")
        self.assertEqual(len(self.calls()), 1)
        self.assertIsNone(self.label()["labels_json"])
        self.assertEqual(len(self.events("refusal")), 1)

    def test_max_budget_reasks_then_invalid(self):
        self.rig_rule(valid_answer(self.item), mode="max_budget")
        lab = self.judge()
        self.assertEqual(lab.status, "invalid")
        cs = self.calls()
        self.assertEqual([c["ask_no"] for c in cs], [1, 2])
        self.assertTrue(all(c["class"] == "max_budget" for c in cs))
        self.assertEqual(self.label()["n_asks"], 2)

    def test_api_error_529_three_tries_then_technical_exhausted(self):
        self.rig_rule(valid_answer(self.item), mode="api_error", status=529)
        lab = self.judge()
        self.assertIsNone(lab)
        cs = self.calls()
        self.assertEqual(len(cs), 3)
        self.assertEqual([c["try_no"] for c in cs], [1, 2, 3])
        self.assertTrue(all(c["class"] == "technical" and c["fail_class"] == "api_transient" for c in cs))
        self.assertIsNone(self.label())
        self.assertEqual(len(self.events("technical_exhausted")), 1)
        led = self.ctx.jdb.read(lambda c: c.execute("SELECT COUNT(*), SUM(cost_usd) FROM judge_ledger").fetchone())
        self.assertEqual(led[0], 3)

    def test_billing_error_is_judge_stop(self):
        self.rig_rule(valid_answer(self.item), mode="api_error", status=402)
        with self.assertRaises(JudgeStop):
            self.judge()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(self.events("stop")), 1)
        self.assertIsNone(self.label())


class HangMode(CallBase):
    overrides = {"timeouts.turn_s": 3}

    def test_hang_times_out_technical_and_is_charged_the_cap(self):
        self.s2 = self.s
        self.rig_rule(valid_answer(self.item), mode="hang")
        bid = self.batch()
        out = run(C.run_call(self.ctx, self.item, batch_id=bid, ask_no=1, try_no=1, cancel=asyncio.Event()))
        self.assertEqual((out.cls, out.fail), ("technical", "timeout"))
        self.assertTrue(out.cost_estimated)
        self.assertEqual(out.cost_usd, 0.10)
        (row,) = self.calls()
        self.assertEqual(row["timed_out"], 1)
        led = self.ctx.jdb.read(lambda c: c.execute("SELECT cost_usd, estimated FROM judge_ledger").fetchone())
        self.assertEqual((led[0], led[1]), (0.10, 1))


class LaunchGuard(CallBase):
    def test_guard_raises_before_any_process(self):
        from unittest import mock

        with mock.patch.object(self.ctx.budget, "can_launch", return_value=False):
            bid = self.batch()
            with self.assertRaises(BudgetBinds):
                run(C.run_call(self.ctx, self.item, batch_id=bid, ask_no=1, try_no=1, cancel=asyncio.Event()))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ctx.halt, "budget")


if __name__ == "__main__":
    unittest.main()
