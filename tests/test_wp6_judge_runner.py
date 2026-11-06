"""WP6-A acceptance tests 5 (validation, re-ask) and 8 (recover), plus tier runs (test 6, partial)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from tests.test_wp6_judge_items import World
from tests.wp6_fakejudge import Rig, SEEDS, answer_json, default_labels, patch_seeds, valid_answer, write_seq_shim
from tests.wp6_synth import SynthSpec
from wtth.judge import call as C
from wtth.judge import items as IM
from wtth.judge import prompt as P
from wtth.judge import runner as R
from wtth.judge.db import active_version, get_meta
from wtth.judge.types import BudgetBinds, JudgeStop


def expected_map(settings):
    return {k: e for k, _t, e in P.held_out_specs(settings)}


class ValidationBase(unittest.TestCase):
    def setUp(self):
        patch_seeds(self, SEEDS)
        self.w = World(self.id().split(".")[-1][:36], SynthSpec.small(5))
        self.rig = self.w.rig
        self.s = self.w.s
        self.exp = expected_map(self.s)
        self.jdb = self.w.jdb
        self.ho = IM.ensure_held_out(self.s, self.jdb, "L2")
        self.first = IM.first_batch(self.jdb, seed=SEEDS.first_batch)

    def answer(self, *, wrong=frozenset()):
        self.rig.answer_held_out(self.first, self.exp, wrong=set(wrong))
        for it in self.first:
            if it.held_out_key is None:
                self.rig.rule(it, valid_answer(it, **({"refers_to_violation": 1} if it.item_type.value == "note" else {})))
        self.rig.write()

    def validate(self, **kw):
        return R.run_first_batch(self.s, level="L2", k=3, **kw)


class FirstBatch(ValidationBase):
    def test_twelve_of_twelve_passes(self):
        self.answer()
        vr = self.validate()
        self.assertTrue(vr.passed)
        self.assertEqual((vr.n_pass, vr.n_items, vr.prompt_version), (12, 12, "v1"))
        self.assertEqual(get_meta(self.jdb, "validation_passed"), "1")
        self.assertEqual(get_meta(self.jdb, "validation_level"), "L2")
        self.assertEqual(active_version(self.jdb), "v1")
        row = self.jdb.read(lambda c: c.execute("SELECT * FROM validations").fetchone())
        self.assertEqual((row["n_items"], row["n_pass"], row["passed"], row["context_level"]), (12, 12, 1, "L2"))
        self.assertGreater(row["c_bar_usd"], 0)
        n_calls = self.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
        self.assertEqual(n_calls, len(self.first))
        batch = self.jdb.read(lambda c: c.execute("SELECT * FROM batches").fetchone())
        self.assertEqual((batch["kind"], batch["n_items"], batch["seed"]), ("first", len(self.first), SEEDS.first_batch))
        self.assertEqual(self.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM judge_runs WHERE ended_at IS NOT NULL")
                                       .fetchone()[0]), 1)

    def test_second_validate_is_a_noop(self):
        self.answer()
        self.validate()
        n = self.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
        vr = self.validate()
        self.assertTrue(vr.passed)
        self.assertEqual(self.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]), n)

    def test_eleven_of_twelve_still_passes(self):
        self.answer(wrong={"H6"})
        vr = self.validate()
        self.assertTrue(vr.passed)
        self.assertEqual((vr.n_pass, vr.per_item["H6"]), (11, False))

    def test_ten_of_twelve_fails_and_v1_stays_active(self):
        self.answer(wrong={"H1", "H6"})
        vr = self.validate()
        self.assertFalse(vr.passed)
        self.assertEqual(vr.n_pass, 10)
        self.assertEqual(get_meta(self.jdb, "validation_passed"), "0")
        self.assertEqual(active_version(self.jdb), "v1")

    def test_cli_exit_3_on_failed_validation(self):
        from unittest import mock as m
        from tests.wp6_fakejudge import clean_env
        import wtth.judge.__main__ as M

        clean_env(self)
        self.answer(wrong={"H1", "H6"})
        argv = ["validate", "--fake", str(self.rig.behavior_path), "--runs-dir", str(self.s.paths.runs_dir),
                "--set", f"paths.slots_root={self.s.paths.slots_root}", "--set", f"paths.cwd={self.s.paths.cwd}",
                "--level", "L2", "-k", "3"]
        for k in ("scheduler.debrief_backoff_s", "scheduler.api_backoff_base_s", "scheduler.breaker_pause_s"):
            pass
        with m.patch.object(M, "_startup", return_value=[]):
            self.assertEqual(M.main(argv), 3)

    def test_v2_path_activates_v2(self):
        self.answer(wrong={"H1", "H6"})
        self.assertFalse(self.validate().passed)
        v2dir = Path(self.rig.root) / "rev"
        v2dir.mkdir()
        (v2dir / "system_v2.txt").write_text("You code messages. Be careful.\n", encoding="utf-8")
        # the v2 prompt answers everything correctly
        self.rig.rules.clear()
        self.answer()
        with mock.patch.object(P, "REVISIONS_DIR", v2dir):
            vr = self.validate(prompt_version="v2", deviation="D-099")
        self.assertTrue(vr.passed)
        self.assertEqual(vr.prompt_version, "v2")
        self.assertEqual(active_version(self.jdb), "v2")
        self.assertEqual(get_meta(self.jdb, "validation_passed"), "1")
        pv = self.jdb.read(lambda c: c.execute("SELECT * FROM prompt_versions WHERE prompt_version='v2'").fetchone())
        self.assertEqual(pv["deviation_ref"], "D-099")
        self.assertTrue(Path(pv["system_path"]).read_text().startswith("You code messages."))
        # the same real items were judged again, under v2
        n_v2 = self.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM judge_labels WHERE prompt_version='v2'")
                             .fetchone()[0])
        self.assertEqual(n_v2, len(self.first))

    def test_v2_failure_is_judge_stop(self):
        self.answer(wrong={"H1", "H6"})
        self.assertFalse(self.validate().passed)
        v2dir = Path(self.rig.root) / "rev"
        v2dir.mkdir()
        (v2dir / "system_v2.txt").write_text("v2 text\n", encoding="utf-8")
        with mock.patch.object(P, "REVISIONS_DIR", v2dir):
            with self.assertRaises(JudgeStop):
                self.validate(prompt_version="v2", deviation="D-099")
            self.assertEqual(active_version(self.jdb), "v1")
            with self.assertRaises(JudgeStop):          # no third attempt
                self.validate(prompt_version="v2", deviation="D-099")
        with self.assertRaises(sqlite3.IntegrityError):  # the schema makes a third version impossible
            self.jdb.write(lambda c: c.execute(
                "INSERT INTO prompt_versions VALUES ('v3','p','s','u','r',NULL,0)"))

    def test_v2_needs_deviation_and_a_failed_v1(self):
        self.answer()
        with self.assertRaises(ValueError):
            self.validate(prompt_version="v2")
        with self.assertRaises(JudgeStop):
            self.validate(prompt_version="v2", deviation="D-099")


class QuoteReask(unittest.TestCase):
    """A quote failure then success is valid; two quote failures are invalid. Needs a stateful fake: K = 1."""

    def setUp(self):
        self.rig = Rig(self.id().split(".")[-1][:36])
        self.s = self.rig.settings
        self.ctx = C.make_context(self.s, run_id="rq", prompt_version="v1", k=1)
        self.item = IM.ensure_held_out(self.s, self.ctx.jdb, "L2")[3]

    def shim(self, texts):
        from tests import fake_claude
        from tests.util import DUMMY_KEY

        behaviors = [{"default": {"d1": {"text": t}}} for t in texts]
        contract = fake_claude.contract_for(self.s, DUMMY_KEY)
        write_seq_shim(self.rig.root, behaviors, contract)

    def run_item(self):
        bid = C.register_batch(self.ctx, [self.item], kind="first", tier=None, subtier=None)
        return asyncio.run(R.judge_item(self.ctx, self.item, batch_id=bid, cancel=asyncio.Event()))

    def bad_quote(self):
        return answer_json(self.item.item_id, self.item.text, default_labels(self.item.item_type),
                           quote="this text is not in the item")

    def test_quote_fail_then_success(self):
        self.shim([self.bad_quote(), valid_answer(self.item)])
        lab = self.run_item()
        self.assertEqual((lab.status, lab.n_asks), ("valid", 2))
        rows = self.ctx.jdb.read(lambda c: c.execute("SELECT ask_no, parse_ok, quote_ok FROM calls ORDER BY launched_at")
                                 .fetchall())
        self.assertEqual([tuple(r) for r in rows], [(1, 0, 0), (2, 1, 1)])
        self.assertEqual(self.ctx.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM judge_ledger").fetchone()[0]), 2)

    def test_two_quote_failures_invalid(self):
        self.shim([self.bad_quote(), self.bad_quote()])
        lab = self.run_item()
        self.assertEqual((lab.status, lab.n_asks), ("invalid", 2))
        row = self.ctx.jdb.read(lambda c: c.execute("SELECT * FROM judge_labels").fetchone())
        self.assertIsNone(row["labels_json"])
        errs = self.ctx.jdb.read(lambda c: c.execute("SELECT parse_errors_json FROM calls").fetchall())
        self.assertTrue(all("quote:not_substring" in e[0] for e in errs))


class ReviewFixes(ValidationBase):
    def test_technical_held_out_failure_is_not_a_validation_failure(self):
        self.answer()
        bad = next(i for i in self.first if i.held_out_key == "H3")
        self.rig.rules.insert(0, {"match": {"prompt_contains": f"id: {bad.item_id}"}, "mode": "api_error",
                                  "status": 529})
        self.rig.write()
        vr = self.validate()
        self.assertTrue(vr.technical)
        self.assertFalse(self.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM validations").fetchone()[0]))
        self.assertEqual(get_meta(self.jdb, "validation_passed"), "0")
        with self.assertRaises(JudgeStop):          # v1 never failed, so v2 stays unavailable
            self.validate(prompt_version="v2", deviation="D-099")
        self.rig.rules.pop(0)
        self.rig.write()
        vr = self.validate()
        self.assertTrue(vr.passed and not vr.technical)

    def test_gate_row_does_not_count_as_failed_v1(self):
        ctx = C.make_context(self.s, run_id="gr", prompt_version="v1", k=1)
        bid = C.register_batch(ctx, [], kind="gate", tier="G1", subtier="G1")
        R._record_validation(ctx, bid, "L2", {f"H{n}": False for n in range(1, 13)})
        self.assertEqual(self.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM validations").fetchone()[0]), 1)
        self.assertIsNone(R._last_validation(self.jdb, "v1"))

    def test_resume_at_ask_two(self):
        item = self.ho[0]
        ctx = C.make_context(self.s, run_id="rs", prompt_version="v1", k=1)
        bid = C.register_batch(ctx, [item], kind="first", tier=None, subtier=None)
        self.rig.rule(item, "not json", mode="normal")
        self.rig.write()
        asyncio.run(C.run_call(ctx, item, batch_id=bid, ask_no=1, try_no=1, cancel=asyncio.Event()))
        self.assertIsNone(R._read_label(ctx, item.item_id))
        lab = asyncio.run(R.judge_item(ctx, item, batch_id=bid, cancel=asyncio.Event()))
        self.assertEqual(lab.status, "invalid")
        asks = ctx.jdb.read(lambda c: [r[0] for r in c.execute("SELECT ask_no FROM calls ORDER BY launched_at")])
        self.assertEqual(asks, [1, 2])


class Recover(unittest.TestCase):
    def test_open_call_rows_are_charged_the_cap_and_closed(self):
        rig = Rig("recover1")
        s = rig.settings
        ctx = C.make_context(s, run_id="dead-run", prompt_version="v1", k=1)
        item = IM.ensure_held_out(s, ctx.jdb, "L2")[0]
        bid = C.register_batch(ctx, [item], kind="first", tier=None, subtier=None)
        ctx.jdb.write(lambda c: c.execute(
            "INSERT INTO calls(call_id, run_id, batch_id, item_id, prompt_version, ask_no, try_no, model, user_sha256,"
            " turn_dir, slot_index, argv_redacted_json, env_names_json, launched_at) VALUES "
            "('c1','dead-run',?,?,'v1',1,1,'claude-sonnet-5-5','u','d',0,'[]','[]',1.0)", (bid, item.item_id)))
        self.assertEqual(R.recover(s), 1)
        row = ctx.jdb.read(lambda c: c.execute("SELECT * FROM calls WHERE call_id='c1'").fetchone())
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual((row["class"], row["fail_class"]), ("technical", "orch_restart"))
        led = ctx.jdb.read(lambda c: c.execute("SELECT cost_usd, estimated, tier, item_type FROM judge_ledger").fetchone())
        self.assertEqual(tuple(led), (0.10, 1, "HO", "board_post"))
        self.assertEqual(R.recover(s), 0)
        self.assertEqual(ctx.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM judge_runs WHERE ended_at IS NULL")
                                      .fetchone()[0]), 0)
        self.assertEqual(ctx.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM jevents WHERE kind='recover'")
                                      .fetchone()[0]), 2)


if __name__ == "__main__":
    unittest.main()
