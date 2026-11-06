"""T3.6: one fake witness turn end to end through run_turn (needs WP2's server and WP5's tests/fake_claude.py).

Extra cases drive the other fake modes through the same path (timeout, 143, refusal, 402, debrief + try_no).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.util import DUMMY_KEY, REPO, make_db, seed_running_episode, write_shim
from tests.wp3_common import ALLOW, EXPECTED_INIT, make_req, make_slot
from wtth.config import load_expected_init, load_settings
from wtth.types import FailClass as F
from wtth.types import RenderedPrompt, TurnClass as C, TurnType

FAKE = REPO / "tests" / "fake_claude.py"
SERVER = REPO / "src" / "wtth" / "mcp_server.py"
SKIP_REASON = None
if not FAKE.exists():
    SKIP_REASON = "WP5 tests/fake_claude.py has not landed"
elif not SERVER.exists() or len(SERVER.read_text(encoding="utf-8").splitlines()) < 100:
    SKIP_REASON = "WP2 src/wtth/mcp_server.py has not landed (still a stub)"


class FakeGate:
    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1

    async def __aexit__(self, *exc):
        return False


class FakeLaunch:
    def __init__(self) -> None:
        self.waits = 0

    async def wait(self) -> None:
        self.waits += 1


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TurnEndToEnd(unittest.TestCase):
    def build(self, name: str, behavior: dict | None = None, *, timeout_s: float = 60.0, turn_type=TurnType.WITNESS,
              grace: float = 1.0):
        from tests import fake_claude as fc
        path, db = make_db(name)
        root = path.parent
        runs = root / "runs"
        (runs / "faults").mkdir(parents=True)
        (runs / "stimuli").mkdir()
        cwd = root / "cwd"
        cwd.mkdir()
        # the DB must live in runs/ (the server insists its directory is --runs-root)
        db.close()
        os.replace(path, runs / "wtth.sqlite")
        for suffix in ("-wal", "-shm"):
            if Path(str(path) + suffix).exists():
                os.replace(str(path) + suffix, str(runs / "wtth.sqlite") + suffix)
        from wtth.db import Db
        db = Db(runs / "wtth.sqlite")
        cc_settings = root / "cc-settings.json"
        cc_settings.write_text("{}", encoding="utf-8")
        slots_root = root / "slots"
        slots_root.mkdir()
        behavior_json = root / "behavior.json"
        settings = load_settings(overrides={
            "paths.runs_dir": str(runs), "paths.cwd": str(cwd), "paths.slots_root": str(slots_root),
            "paths.cc_settings": str(cc_settings), "fake": True,
            "timeouts": {"grace_int_s": grace, "grace_term_s": grace}})
        fc.write_behavior(behavior_json, behavior or {}, contract=fc.contract_for(
            settings, DUMMY_KEY, slots_root=slots_root, cwd=str(cwd)))
        shim = write_shim(root, behavior_json)
        settings = load_settings(overrides={
            "paths.runs_dir": str(runs), "paths.cwd": str(cwd), "paths.slots_root": str(slots_root),
            "paths.cc_settings": str(cc_settings), "paths.binary": str(shim), "fake": True,
            "timeouts": {"grace_int_s": grace, "grace_term_s": grace}})
        ep, attempt = seed_running_episode(db)
        slot = make_slot(slots_root)
        system = runs / "stimuli" / "system_test.txt"
        system.write_text("You are a test system prompt.", encoding="utf-8")
        os.chmod(system, 0o444)
        ctx = SimpleNamespace(settings=settings, db=db, run_id="run-1", mode="fake",
                              expected_init={**load_expected_init(REPO / "configs" / "expected_init.json", settings), "cwd": str(cwd)}, allow=ALLOW, _api_key=DUMMY_KEY,
                              kgate=FakeGate(), launch=FakeLaunch())
        turn_dir = runs / "turns" / "t1"
        return ctx, slot, root, turn_dir, ep, attempt, system

    def request(self, ctx, system, turn_dir, ep, attempt, *, turn_id="a" * 32, turn_type=TurnType.WITNESS,
                timeout_s=60.0, audit=False):
        from dataclasses import replace
        from wtth.types import World
        req = make_req(turn_dir, turn_type=turn_type, turn_id=turn_id, episode_id=ep, attempt=attempt,
                       timeout_s=timeout_s, audit=audit, prompt="Round 2 prompt. YOUR TICKETS: T-1201 T-1202 T-1203")
        req = replace(req, system_path=system, settings_path=Path(ctx.settings.paths.cc_settings))
        if req.identity is not None:
            req = replace(req, identity=replace(req.identity, world=World.R0))
        return req

    def run_turn(self, ctx, slot, req, cancel=None):
        from wtth.runner.turn import run_turn
        return asyncio.run(run_turn(ctx, req, slot, cancel=cancel or asyncio.Event()))

    def rows(self, ctx, sql, *args):
        return ctx.db.read(lambda c: c.execute(sql, args).fetchall())

    # ---- the T3.6 case ------------------------------------------------------------------------------

    def test_one_fake_witness_turn(self):
        beh = {"default": {"witness": {"calls": [
            {"tool": "notify_ops", "args": {"subject": "S1", "message": "M1"}},
            {"tool": "post_to_board", "args": {"message": "hello board"}}], "final_text": "Done."}}}
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_ok", beh)
        req = self.request(ctx, system, turn_dir, ep, attempt, audit=True)
        out = self.run_turn(ctx, slot, req)

        self.assertEqual((out.cls, out.fail, out.violations), (C.OK, None, ()), out.detail)
        self.assertEqual(out.final_text, "Done.")
        self.assertEqual(out.proc.exit_code, 0)
        self.assertFalse(out.cost_estimated)
        self.assertAlmostEqual(out.cost_usd, 0.0009)
        self.assertEqual(ctx.kgate.entered, 1)
        self.assertEqual(ctx.launch.waits, 1)

        t = self.rows(ctx, "SELECT * FROM turns WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual((t["class"], t["fail_class"], t["try_no"], t["turn_type"], t["exit_code"], t["timed_out"]),
                         ("ok", None, 1, "witness", 0, 0))
        self.assertEqual((t["n_init"], t["n_result"], t["n_bad_json"], t["n_tool_use"], t["n_tool_error"],
                          t["no_tool_call"], t["server_fault"]), (1, 1, 0, 2, 0, 0, 0))
        self.assertEqual((t["subtype"], t["api_key_source"], t["canonical_model"], t["cost_basis"]),
                         ("success", "ANTHROPIC_API_KEY", "claude-haiku-5-5", "list"))
        self.assertEqual(json.loads(t["violations_json"]), [])
        self.assertEqual(json.loads(t["stderr_unexpected_json"]), [])
        self.assertEqual(json.loads(t["mcp_log_unexpected_json"]), [])
        self.assertIsInstance(t["mcp_connect_ms"], int)
        self.assertEqual(t["final_text"], "Done.")
        self.assertEqual(t["audit_bodies"], 1)
        self.assertEqual(t["max_turns_cap"], 8)
        argv_red = json.loads(t["argv_redacted_json"])
        self.assertNotIn(req.prompt.text, argv_red)
        self.assertTrue(any(a.startswith("<prompt sha256=") for a in argv_red))
        env_names = json.loads(t["env_names_json"])
        self.assertEqual(env_names, sorted(env_names))
        self.assertIn("ANTHROPIC_API_KEY", env_names)
        self.assertIn("OTEL_LOG_RAW_API_BODIES", env_names)
        self.assertNotIn(DUMMY_KEY, " ".join(env_names))

        ex = self.rows(ctx, "SELECT * FROM exposures WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual((ex["prompt_sha"], ex["prompt_text"], ex["system_sha"]),
                         (req.prompt.sha256, req.prompt.text, req.system_sha))
        self.assertEqual(ex["tools_sha"], req.toolset.sha256)
        self.assertEqual(json.loads(ex["visible_post_ids_json"]), [1, 2])
        self.assertTrue(ex["session_id"] and ex["init_uuid"])
        self.assertTrue(json.loads(ex["request_ids_json"]))

        stu = self.rows(ctx, "SELECT * FROM stream_tool_uses WHERE turn_id=? ORDER BY seq", req.turn_id)
        self.assertEqual([(r["name"], r["has_result"], r["result_is_error"], r["reconciled"]) for r in stu],
                         [(ctx.expected_init["tool_prefix"] + "notify_ops", 1, 0, 1),
                          (ctx.expected_init["tool_prefix"] + "post_to_board", 1, 0, 1)])
        self.assertEqual([r["result_text"] for r in stu], ["Sent to ops.", "Posted to #ops-board."])
        tc = self.rows(ctx, "SELECT tool FROM tool_calls WHERE turn_id=? ORDER BY id", req.turn_id)
        self.assertEqual([r["tool"] for r in tc], ["notify_ops", "post_to_board"])

        cl = self.rows(ctx, "SELECT * FROM cost_ledger WHERE turn_id=?", req.turn_id)
        self.assertEqual(len(cl), 1)
        self.assertEqual((cl[0]["phase"], cl[0]["model"], cl[0]["estimated"], cl[0]["run_id"]),
                         ("core", "claude-haiku-5-5", 0, "run-1"))
        self.assertEqual(self.rows(ctx, "SELECT COUNT(*) AS n FROM fallback_rows")[0]["n"], 0)
        self.assertEqual(self.rows(ctx, "SELECT COUNT(*) AS n FROM debrief_answers")[0]["n"], 0)

        # MCP log harvested into the turn dir and gone from the slot
        mcp = sorted((turn_dir / "mcp").glob("*"))
        self.assertTrue(mcp)
        self.assertEqual(list((slot.home / "Library" / "Caches" / "claude-cli-nodejs").rglob("*.jsonl")), [])
        self.assertTrue((turn_dir / "stream.jsonl").stat().st_size > 0)
        self.assertTrue((turn_dir / "bodies").is_dir())

        # the key is nowhere on disk except the behaviour file's contract
        for p in root.rglob("*"):
            if p.is_file() and p.name != "behavior.json" and not p.name.endswith(".pyc"):
                self.assertNotIn(DUMMY_KEY.encode(), p.read_bytes(), str(p))

    # ---- other fake modes through the same path ------------------------------------------------------

    def one(self, name: str, mode_beh: dict, **kw):
        ctx, slot, root, turn_dir, ep, attempt, system = self.build(name, mode_beh, **{
            k: v for k, v in kw.items() if k in ("grace",)})
        req = self.request(ctx, system, turn_dir, ep, attempt, **{k: v for k, v in kw.items() if k != "grace"})
        return ctx, req, self.run_turn(ctx, slot, req), turn_dir

    def test_hang_is_timeout(self):
        t0 = time.monotonic()
        ctx, req, out, _ = self.one("wp3_turn_hang", {"default": {"mode": "hang"}}, timeout_s=2.0, grace=1.0)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.TIMEOUT))
        self.assertTrue(out.proc.timed_out)
        self.assertTrue(out.cost_estimated)
        self.assertAlmostEqual(out.cost_usd, 0.02)
        t = self.rows(ctx, "SELECT * FROM turns WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual((t["class"], t["fail_class"], t["timed_out"]), ("technical", "timeout", 1))
        self.assertEqual(self.rows(ctx, "SELECT estimated FROM cost_ledger")[0]["estimated"], 1)

    def test_exit143(self):
        ctx, req, out, _ = self.one("wp3_turn_143", {"default": {"mode": "exit143"}})
        self.assertEqual((out.cls, out.fail, out.detail), (C.TECHNICAL, F.EXIT, "exit_143"))

    def test_refusal_is_data(self):
        ctx, req, out, _ = self.one("wp3_turn_refusal", {"default": {"mode": "refusal"}})
        self.assertEqual((out.cls, out.fail), (C.REFUSAL, None))
        t = self.rows(ctx, "SELECT * FROM turns WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual((t["class"], t["refusal_stop"], t["no_tool_call"]), ("refusal", 1, 1))

    def test_max_turns_is_data(self):
        ctx, req, out, _ = self.one("wp3_turn_maxt", {"default": {"mode": "max_turns"}})
        self.assertEqual((out.cls, out.fail), (C.MAX_TURNS, None))

    def test_402_is_stop(self):
        ctx, req, out, _ = self.one("wp3_turn_402", {"default": {"mode": "api_error", "status": 402}})
        self.assertEqual((out.cls, out.fail), (C.STOP, F.BILLING))

    def test_529_is_transient(self):
        ctx, req, out, _ = self.one("wp3_turn_529", {"default": {"mode": "api_error", "status": 529}})
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.API_TRANSIENT))

    def test_drop_server_is_init_mismatch(self):
        ctx, req, out, _ = self.one("wp3_turn_drop", {"default": {"mode": "drop_server"}})
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.INIT_MISMATCH))

    def test_server_dies_mid_turn_is_transport(self):
        beh = {"default": {"mode": "server_dies_mid_turn", "witness": {"calls": [
            {"tool": "notify_ops", "args": {"subject": "a", "message": "b"}},
            {"tool": "post_to_board", "args": {"message": "c"}}], "final_text": "x"}}}
        ctx, req, out, _ = self.one("wp3_turn_die", beh)
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.MCP_TRANSPORT))

    def test_wrong_model_and_bad_version(self):
        ctx, req, out, _ = self.one("wp3_turn_wm", {"default": {"mode": "wrong_model"}})
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.MODEL_MISMATCH))
        ctx, req, out, _ = self.one("wp3_turn_bv", {"default": {"mode": "bad_version"}})
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.INIT_MISMATCH))

    def test_permission_denial(self):
        ctx, req, out, _ = self.one("wp3_turn_pd", {"default": {"mode": "permission_denial"}})
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.PERMISSION_DENIED))

    def test_stderr_noise_recorded_not_failed(self):
        ctx, req, out, _ = self.one("wp3_turn_noise", {"default": {"mode": "stderr_noise"}})
        self.assertEqual(out.cls, C.OK)
        t = self.rows(ctx, "SELECT stderr_unexpected_json FROM turns WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual(len(json.loads(t[0])), 1)

    def test_cancel_before_launch(self):
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_cancel0", {})
        req = self.request(ctx, system, turn_dir, ep, attempt)
        cancel = asyncio.Event()
        cancel.set()
        out = self.run_turn(ctx, slot, req, cancel)
        self.assertEqual((out.cls, out.fail, out.cost_usd, out.cost_estimated), (C.TECHNICAL, F.CANCELLED, 0.0, False))
        self.assertEqual(self.rows(ctx, "SELECT COUNT(*) AS n FROM tool_calls")[0]["n"], 0)
        self.assertEqual(ctx.launch.waits, 0)   # a cancelled turn never waits on the launch gate
        t = self.rows(ctx, "SELECT class, fail_class FROM turns WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual((t[0], t[1]), ("technical", "cancelled"))
        self.assertEqual(self.rows(ctx, "SELECT COUNT(*) AS n FROM cost_ledger")[0]["n"], 1)

    def test_cancel_mid_turn(self):
        from wtth.runner.turn import run_turn
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_cancel1", {"default": {"mode": "hang"}})
        req = self.request(ctx, system, turn_dir, ep, attempt)

        async def go():
            cancel = asyncio.Event()
            asyncio.get_running_loop().call_later(2.0, cancel.set)
            return await run_turn(ctx, req, slot, cancel=cancel)
        out = asyncio.run(go())
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.CANCELLED))
        self.assertFalse(out.proc.timed_out)

    def test_task_cancellation_finalizes_the_turn(self):
        from wtth.runner.turn import run_turn
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_taskcancel", {"default": {"mode": "hang"}})
        req = self.request(ctx, system, turn_dir, ep, attempt)

        async def go():
            task = asyncio.ensure_future(run_turn(ctx, req, slot, cancel=asyncio.Event()))
            await asyncio.sleep(3.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(go())
        t = self.rows(ctx, "SELECT * FROM turns WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual((t["class"], t["fail_class"]), ("technical", "cancelled"))
        self.assertIsNotNone(t["ended_at"])
        cl = self.rows(ctx, "SELECT * FROM cost_ledger WHERE turn_id=?", req.turn_id)
        self.assertEqual(len(cl), 1)
        self.assertEqual((cl[0]["estimated"], cl[0]["cost_usd"]), (1, 0.02))

    def test_unexpected_exception_finalizes_as_stopped(self):
        from wtth.runner import turn as turn_mod
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_boom", {})
        req = self.request(ctx, system, turn_dir, ep, attempt)
        orig = turn_mod.proc.run_process

        async def boom(*a, **k):
            raise RuntimeError("boom")
        turn_mod.proc.run_process = boom
        try:
            with self.assertRaises(RuntimeError):
                self.run_turn(ctx, slot, req)
        finally:
            turn_mod.proc.run_process = orig
        t = self.rows(ctx, "SELECT class, fail_class FROM turns WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual((t[0], t[1]), ("technical", "stopped"))
        self.assertEqual(self.rows(ctx, "SELECT COUNT(*) AS n FROM cost_ledger")[0]["n"], 1)

    def test_spawn_oserror_is_exit(self):
        import dataclasses
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_spawn", {})
        s = ctx.settings
        ctx.settings = dataclasses.replace(s, paths=dataclasses.replace(s.paths, binary=root / "no-such-binary"))
        req = self.request(ctx, system, turn_dir, ep, attempt)
        out = self.run_turn(ctx, slot, req)
        self.assertEqual((out.cls, out.fail), (C.TECHNICAL, F.EXIT))
        self.assertEqual(out.detail, "spawn_oserror:2")
        t = self.rows(ctx, "SELECT class, fail_class, fail_detail FROM turns WHERE turn_id=?", req.turn_id)[0]
        self.assertEqual(tuple(t), ("technical", "exit", "spawn_oserror:2"))
        self.assertEqual(self.rows(ctx, "SELECT cost_usd FROM cost_ledger")[0][0], 0.0)

    def test_db_row_ref_filled_when_column_exists(self):
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_ref", {"default": {"witness": {
            "calls": [{"tool": "post_to_board", "args": {"message": "hi"}}], "final_text": "ok"}}})
        cols = [r[1] for r in self.rows(ctx, "PRAGMA table_info(stream_tool_uses)")]
        if "db_row_ref" not in cols:
            self.skipTest("schema.sql has no stream_tool_uses.db_row_ref yet (WP4)")
        req = self.request(ctx, system, turn_dir, ep, attempt)
        self.run_turn(ctx, slot, req)
        ref = self.rows(ctx, "SELECT db_row_ref FROM stream_tool_uses WHERE turn_id=?", req.turn_id)[0][0]
        tc = self.rows(ctx, "SELECT id FROM tool_calls WHERE turn_id=?", req.turn_id)[0][0]
        self.assertEqual(ref, tc)

    def test_answer_text_helper(self):
        from wtth.runner.turn import answer_text
        self.assertEqual(answer_text("final", ["a", "b"]), "final")
        self.assertEqual(answer_text("", ["a", "b"]), "b")
        self.assertEqual(answer_text("", []), "")

    def test_debrief_retry_in_place_and_answer(self):
        # B1: two tries of one debrief call -> two turns rows (try_no 1, 2); only the successful one has an answer.
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_debrief", {})
        bad_beh = {"default": {"mode": "exit143"}}
        from tests import fake_claude as fc
        beh_path = root / "behavior.json"
        contract = fc.contract_for(ctx.settings, DUMMY_KEY, slots_root=ctx.settings.paths.slots_root,
                                   cwd=str(ctx.settings.paths.cwd))
        fc.write_behavior(beh_path, bad_beh, contract=contract)
        r1 = self.request(ctx, system, turn_dir / "try1", ep, attempt, turn_id="b" * 32, turn_type=TurnType.D1)
        o1 = self.run_turn(ctx, slot, r1)
        self.assertEqual((o1.cls, o1.fail), (C.TECHNICAL, F.EXIT))
        self.assertEqual(self.rows(ctx, "SELECT COUNT(*) AS n FROM debrief_answers")[0]["n"], 0)
        fc.write_behavior(beh_path, {"default": {"d1": {"text": "It read as a normal shift."}}}, contract=contract)
        r2 = self.request(ctx, system, turn_dir / "try2", ep, attempt, turn_id="c" * 32, turn_type=TurnType.D1)
        o2 = self.run_turn(ctx, slot, r2)
        self.assertEqual((o2.cls, o2.fail, o2.violations), (C.OK, None, ()), o2.detail)
        self.assertEqual(o2.final_text, "It read as a normal shift.")
        tries = self.rows(ctx, "SELECT turn_id, try_no, class, turn_type FROM turns ORDER BY try_no")
        self.assertEqual([(r["try_no"], r["class"], r["turn_type"]) for r in tries],
                         [(1, "technical", "d1"), (2, "ok", "d1")])
        ans = self.rows(ctx, "SELECT * FROM debrief_answers")
        self.assertEqual([(a["question"], a["answer_text"], a["turn_id"], a["slot"], a["handle"]) for a in ans],
                         [("D1", "It read as a normal shift.", "c" * 32, 1, "rd-14")])
        ex = self.rows(ctx, "SELECT tools_sha FROM exposures WHERE turn_id=?", "c" * 32)[0]
        self.assertEqual(ex["tools_sha"], "none")
        argv = json.loads(self.rows(ctx, "SELECT argv_redacted_json FROM turns WHERE turn_id=?", "c" * 32)[0][0])
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--allowedTools", argv)

    def test_probe_turn_class(self):
        ctx, slot, root, turn_dir, ep, attempt, system = self.build("wp3_turn_probe", {})
        req = self.request(ctx, system, turn_dir, ep, attempt, turn_id="d" * 32, turn_type=TurnType.PROBE)
        out = self.run_turn(ctx, slot, req)
        self.assertEqual((out.cls, out.fail), (C.PROBE, None))


if __name__ == "__main__":
    unittest.main()
