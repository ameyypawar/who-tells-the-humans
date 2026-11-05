"""WP6-A acceptance test 7 (lock, environment) and the CLI commands end to end."""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

import wtth.judge.__main__ as M
from tests.test_wp6_judge_items import World
from tests.wp6_fakejudge import FAST, Rig, SEEDS, clean_env, patch_seeds, valid_answer
from tests.wp6_synth import SynthSpec
from wtth.judge import items as IM
from wtth.judge import prompt as P
from wtth.judge.db import judge_db_path, set_meta
from wtth.orchestrator.lock import RunLock


def common(rig):
    s = rig.settings
    out = ["--fake", str(rig.behavior_path), "--runs-dir", str(s.paths.runs_dir),
           "--set", f"paths.slots_root={s.paths.slots_root}", "--set", f"paths.cwd={s.paths.cwd}"]
    for k, v in FAST.items():
        out += ["--set", f"{k}={v}"]
    return out


def cli(rig, *argv, startup=False):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        if startup:
            code = M.main(list(argv) + common(rig))
        else:
            with mock.patch.object(M, "_startup", return_value=[]):
                code = M.main(list(argv) + common(rig))
    return code, buf.getvalue(), err.getvalue()


class LockAndEnv(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        self.rig = Rig(self.id().split(".")[-1][:36])
        self.s = self.rig.settings

    def test_held_lock_exits_5(self):
        with RunLock(self.s.paths.lock_path, "orchestrator"):
            code, out, _ = cli(self.rig, "recover")
        self.assertEqual(code, 5)
        self.assertIn("lock held", out)
        self.assertFalse(judge_db_path(self.s).exists())          # nothing was written

    def test_free_lock_runs_and_releases(self):
        code, out, _ = cli(self.rig, "recover")
        self.assertEqual(code, 0, out)
        self.assertIn("closed 0 open call rows", out)
        with RunLock(self.s.paths.lock_path, "after"):
            pass

    def test_forbidden_env_exits_2(self):
        for var in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            with mock.patch.dict(os.environ, {var: "1"}):
                code, out, _ = cli(self.rig, "recover", startup=True)
            self.assertEqual(code, 2, var)
            self.assertIn(var, out)
            self.assertFalse(judge_db_path(self.s).exists())

    def test_real_startup_checks_pass_in_fake_mode(self):
        code, out, err = cli(self.rig, "recover", startup=True)
        self.assertEqual(code, 0, out + err)

    def test_startup_problems_exit_2(self):
        buf = io.StringIO()
        with mock.patch.object(M, "_startup", return_value=["something is wrong"]), contextlib.redirect_stdout(buf):
            code = M.main(["recover"] + common(self.rig))
        self.assertEqual(code, 2)
        self.assertIn("something is wrong", buf.getvalue())

    def test_bad_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            M.main(["run", "--tier", "J9"])


class Commands(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        patch_seeds(self, SEEDS)
        self.w = World(self.id().split(".")[-1][:36], SynthSpec.small(4))
        self.rig = self.w.rig

    def test_plan_and_status(self):
        code, out, _ = cli(self.rig, "plan")
        self.assertEqual(code, 0, out)
        self.assertIn("J1a", out)
        self.assertIn("J2i", out)
        self.assertIn("remaining", out)
        code, out, _ = cli(self.rig, "status")
        self.assertEqual(code, 0)
        self.assertIn("validation_passed=0", out)
        self.assertIn("J1a", out)

    def test_plan_without_database(self):
        rig = Rig("cli_nodb")
        code, out, _ = cli(rig, "plan")
        self.assertEqual((code, "no judge database" in out), (0, True))

    def test_validate_run_end_to_end(self):
        exp = {k: e for k, _t, e in P.held_out_specs(self.w.s)}
        ho = IM.ensure_held_out(self.w.s, self.w.jdb, "L2")
        first = IM.first_batch(self.w.jdb, seed=SEEDS.first_batch)
        self.rig.answer_held_out(first, exp)
        for it in IM.load_items(self.w.jdb):
            if it.held_out_key is None:
                self.rig.rule(it, valid_answer(it))
        self.rig.write()
        code, out, err = cli(self.rig, "validate", "--level", "L2", "-k", "4")
        self.assertEqual(code, 0, out + err)
        self.assertIn("PASSED", out)
        code, out, err = cli(self.rig, "run", "--tier", "J1", "--subtier", "J1c", "-k", "4")
        self.assertEqual(code, 0, out + err)
        self.assertIn("valid=", out)
        code, out, _ = cli(self.rig, "status")
        self.assertIn("validation_passed=1", out)

    def test_extract_is_idempotent_through_the_cli(self):
        n = self.w.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        code, out, err = cli(self.rig, "extract", "--phase", "core")
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 new items", out)
        self.assertIn("s_c=", out)
        code, out, err = cli(self.rig, "extract", "--phase", "sonnet")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.w.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM items").fetchone()[0]), n)

    def test_gate_g1_without_disagreement_uses_no_judge(self):
        from tests.wp5_support import add_done_episode

        for ep in range(16):
            add_done_episode(self.w.gt.db(), phase="g1", cell="NONE", level="L2", seed=9011 + ep,
                             notes=[("rd-14", 2, "Hendricks", "Hendricks split")] if ep < 7 else [])
        code, out, err = cli(self.rig, "gate-g1", "--level", "L2")
        self.assertEqual(code, 0, out + err)
        self.assertIn('"judge_used": false', out)

    def test_run_before_validation_exits_3(self):
        code, _, err = cli(self.rig, "run", "--tier", "J1")
        self.assertEqual(code, 3)
        self.assertIn("validation", err)

    def test_budget_exit_4(self):
        set_meta(self.w.jdb, "validation_passed", "1")
        code, _, err = cli(self.rig, "run", "--tier", "J1", "--set", "budget.cap_usd=1.0")
        self.assertEqual(code, 4)
        self.assertIn("BUDGET", err)


if __name__ == "__main__":
    unittest.main()
