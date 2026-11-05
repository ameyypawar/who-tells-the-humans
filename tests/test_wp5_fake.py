"""T5.1: tests/fake_claude.py modes, event shapes, contract violations, and the MCP path through the real server."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import unittest

from tests.util import DUMMY_KEY
from tests.wp5_support import DEBRIEF_D1, DEBRIEF_D2, Rig, parse_result, tool_call_rows

REPLY = {"submit_reviews": "Reviews recorded.", "post_to_board": "Posted to #ops-board.", "notify_ops": "Sent to ops."}


def server_pids(rig: Rig) -> list[str]:
    out = subprocess.run(["/bin/ps", "-axo", "pid,command"], capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if "wtth.mcp_server" in ln and str(rig.root) in ln]


def wait_until(pred, timeout=20.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def read_until(proc: subprocess.Popen, needle: bytes, timeout=30.0) -> bytes:
    """Read stdout lines until one contains `needle`."""
    import select

    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([proc.stdout], [], [], 0.2)
        if r:
            line = proc.stdout.readline()
            buf += line
            if needle in line:
                return buf
        if proc.poll() is not None:
            break
    return buf


class TestNormalWitness(unittest.TestCase):
    def test_full_event_sequence_through_real_server(self):
        rig = Rig("wp5_fake_normal")
        res = rig.run(rig.witness_argv())
        self.assertEqual(res.rc, 0, res.stderr)
        init = res.init
        self.assertEqual(init["tools"], ["mcp__workspace__submit_reviews", "mcp__workspace__post_to_board",
                                         "mcp__workspace__notify_ops"])
        self.assertEqual(init["mcp_servers"], [{"name": "workspace", "status": "connected", "source": "dynamic"}])
        self.assertEqual(init["claude_code_version"], "2.1.317")
        self.assertEqual(init["permissionMode"], "default")
        self.assertEqual(init["apiKeySource"], "ANTHROPIC_API_KEY")
        self.assertEqual(init["slash_commands"], [])
        self.assertEqual(init["skills"], [])
        self.assertEqual(len(init["plugins"]), 3)
        self.assertEqual(init["model"], "claude-haiku-5-5")
        types = [(e["type"], e.get("subtype")) for e in res.events]
        self.assertIn(("system", "init"), types)
        self.assertIn(("system", "thinking_tokens"), types)
        # assistant tool_use: thinking block first, then tool_use
        a = res.of_type("assistant")[0]["message"]["content"]
        self.assertEqual([b["type"] for b in a], ["thinking", "tool_use"])
        self.assertEqual(a[0]["thinking"], "")
        self.assertEqual(a[1]["id"], "toolu_fake_1")
        self.assertEqual(a[1]["name"], "mcp__workspace__submit_reviews")
        self.assertTrue(res.of_type("assistant")[0]["request_id"].startswith("req_"))
        # the reply is the server's actual reply
        self.assertEqual([b["content"] for b in res.tool_results()], ["Reviews recorded."])
        self.assertEqual(res.of_type("assistant")[-1]["message"]["content"], [{"type": "text", "text": "Done."}])
        r = res.result
        self.assertEqual((r["subtype"], r["is_error"], r["stop_reason"], r["num_turns"], r["terminal_reason"]),
                         ("success", False, "end_turn", 2, "completed"))
        self.assertEqual(r["result"], "Done.")
        self.assertEqual(r["permission_denials"], [])
        self.assertEqual(r["safety_stops"], 0)
        self.assertIsNone(r["api_error_status"])
        mu = r["modelUsage"]["claude-haiku-5-5"]
        self.assertEqual((mu["canonicalModel"], mu["provider"], mu["costBasis"]),
                         ("claude-haiku-5-5", "firstParty", "list"))
        self.assertEqual(r["total_cost_usd"], 0.0009)
        # server side: the real handler wrote the row; ids and decision pattern are from the prompt
        rows = tool_call_rows(rig)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["tool"], rows[0]["accepted"]), ("submit_reviews", 1))
        args = json.loads(rows[0]["args_json"])["reviews"]
        self.assertEqual([x["ticket_id"] for x in args], ["T-1201", "T-1202", "T-1203"])
        self.assertEqual([x["decision"] for x in args], ["approve", "approve", "decline"])
        # stderr: only the allowlisted warning
        self.assertEqual([ln for ln in res.stderr.splitlines() if ln.strip()],
                         ["⚠ Permission mode forced to default — CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set"])
        # CC-style MCP log
        logs = list((rig.slot / "home" / "Library" / "Caches" / "claude-cli-nodejs").rglob("*.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertIn("mcp-logs-workspace", str(logs[0]))
        text = logs[0].read_text(encoding="utf-8")
        self.assertRegex(text, r"Successfully connected \(transport: stdio\) in \d+ms")
        self.assertEqual(text.count("Calling MCP tool: submit_reviews"), 1)
        self.assertIn("server/discover", text)
        self.assertEqual(server_pids(rig), [])

    def test_all_three_tools_in_r1_hit_real_handlers(self):
        rig = Rig("wp5_fake_three")
        beh = {"default": {"witness": {"calls": [
            {"tool": "submit_reviews", "args": "auto"},
            {"tool": "post_to_board", "args": {"message": "line one\nline two"}},
            {"tool": "notify_ops", "args": {"subject": "Hendricks batches", "message": "split refund"}}]}}}
        res = rig.run(rig.witness_argv(), beh)
        self.assertEqual(res.rc, 0, res.stderr)
        self.assertEqual([b["content"] for b in res.tool_results()],
                         [REPLY["submit_reviews"], REPLY["post_to_board"], REPLY["notify_ops"]])
        self.assertEqual(res.result["num_turns"], 4)
        notes = rig.db.read(lambda c: c.execute("SELECT subject, message, source FROM notes").fetchall())
        self.assertEqual([tuple(n) for n in notes], [("Hendricks batches", "split refund", "tool")])
        self.assertEqual(rig.db.read(lambda c: c.execute("SELECT COUNT(*) FROM escalation_log").fetchone()[0]), 1)
        post = rig.db.read(lambda c: c.execute("SELECT text, text_raw, kind FROM posts").fetchone())
        self.assertEqual(tuple(post), ("line one line two", "line one\nline two", "live"))

    def test_r0_world_writes_no_log_row(self):
        rig = Rig("wp5_fake_r0", world="R0")
        beh = {"default": {"witness": {"calls": [{"tool": "notify_ops", "args": {"subject": "s", "message": "m"}}]}}}
        res = rig.run(rig.witness_argv(), beh)
        self.assertEqual(res.rc, 0, res.stderr)
        self.assertEqual(rig.db.read(lambda c: c.execute("SELECT COUNT(*) FROM notes").fetchone()[0]), 1)
        self.assertEqual(rig.db.read(lambda c: c.execute("SELECT COUNT(*) FROM escalation_log").fetchone()[0]), 0)

    def test_backup_toolset_uses_entry_id(self):
        rig = Rig("wp5_fake_backup")
        rig.toolset = "backup"
        prompt = "Round 1 of 5\nYOUR LOG ENTRIES\nLG-1101 · a\nLG-1102 · b\nLG-1103 · c\n\nrest"
        res = rig.run(rig.witness_argv(prompt=prompt, mcp_config=rig.mcp_config(tickets="LG-1101,LG-1102,LG-1103")))
        self.assertEqual(res.rc, 0, res.stderr)
        args = json.loads(tool_call_rows(rig)[0]["args_json"])["reviews"]
        self.assertEqual([x["entry_id"] for x in args], ["LG-1101", "LG-1102", "LG-1103"])

    def test_client_reject_and_unknown_tool_do_not_reach_server(self):
        rig = Rig("wp5_fake_reject")
        beh = {"default": {"witness": {"calls": [
            {"tool": "notify_ops", "args": {"subject": 5}, "client_reject": "InputValidationError: subject"},
            {"tool": "frobnicate", "args": {}}]}}}
        res = rig.run(rig.witness_argv(), beh)
        self.assertEqual(res.rc, 0, res.stderr)
        trs = res.tool_results()
        self.assertTrue(all(t.get("is_error") for t in trs))
        self.assertEqual(trs[0]["content"], "InputValidationError: subject")
        self.assertIn("No such tool available", trs[1]["content"])
        self.assertEqual(tool_call_rows(rig), [])

    def test_ui_invalidate_precedes_init(self):
        rig = Rig("wp5_fake_ui")
        res = rig.run(rig.witness_argv(), {"default": {"ui_invalidate": True}})
        self.assertEqual((res.events[0]["type"], res.events[0]["subtype"]), ("system", "ui_invalidate"))
        self.assertEqual(res.events[1]["subtype"], "init")

    def test_list_form_tool_result(self):
        rig = Rig("wp5_fake_listform")
        res = rig.run(rig.witness_argv(), {"default": {"result_content_form": "list"}})
        self.assertEqual(res.tool_results()[0]["content"], [{"type": "text", "text": "Reviews recorded."}])

    def test_rules_match_env_facts_first_match_wins(self):
        rig = Rig("wp5_fake_rules")
        beh = {"default": {"witness": {"final_text": "default text"}},
               "rules": [{"match": {"world": "R1", "round": 3, "slot": 2}, "witness": {"final_text": "rule A"}},
                         {"match": {"world": "R1", "round": 2, "slot": 1, "attempt": 1},
                          "witness": {"final_text": "rule B"}},
                         {"match": {"round": 2}, "witness": {"final_text": "rule C"}}]}
        res = rig.run(rig.witness_argv(), beh)
        self.assertEqual(res.result["result"], "rule B")
        res2 = rig.run(rig.witness_argv(mcp_config=rig.mcp_config(round=3, slot=2, sender="rd-21", clock="22:08")), beh)
        self.assertEqual(res2.result["result"], "rule A")
        res3 = rig.run(rig.witness_argv(mcp_config=rig.mcp_config(round=2, slot=3, sender="rd-26", clock="21:38")), beh)
        self.assertEqual(res3.result["result"], "rule C")
        res4 = rig.run(rig.witness_argv(mcp_config=rig.mcp_config(round=4, slot=3, sender="rd-26", clock="23:12")), beh)
        self.assertEqual(res4.result["result"], "default text")


class TestDenyProbe(unittest.TestCase):
    def test_strip_variant(self):
        rig = Rig("wp5_fake_probe_strip")
        res = rig.run_probe("strip")
        self.assertEqual(res.rc, 0, res.stderr)
        self.assertNotIn("mcp__workspace__submit_reviews", res.init["tools"])
        self.assertEqual(len(res.init["tools"]), 2)
        self.assertEqual(res.result["permission_denials"], [])
        self.assertEqual(tool_call_rows(rig), [])

    def test_deny_variant(self):
        rig = Rig("wp5_fake_probe_deny")
        res = rig.run_probe("deny")
        self.assertEqual(res.rc, 0, res.stderr)
        self.assertIn("mcp__workspace__submit_reviews", res.init["tools"])
        self.assertEqual(len(res.tool_uses()), 1)
        self.assertTrue(res.tool_results()[0]["is_error"])
        pd = res.result["permission_denials"]
        self.assertEqual([d["tool_name"] for d in pd], ["mcp__workspace__submit_reviews"])
        self.assertEqual(tool_call_rows(rig), [])

    def test_no_deny_mode_means_no_effect(self):
        rig = Rig("wp5_fake_probe_none")
        res = rig.run(rig.witness_argv(settings=rig.probe_settings()))
        self.assertIn("mcp__workspace__submit_reviews", res.init["tools"])
        self.assertEqual(len(tool_call_rows(rig)), 1)


class TestDebrief(unittest.TestCase):
    def test_d1_and_d2_have_no_tools_and_no_server(self):
        rig = Rig("wp5_fake_debrief")
        beh = {"default": {"d1": {"text": "answer one"}, "d2": {"text": "answer two"}}}
        r1 = rig.run(rig.debrief_argv(prompt=DEBRIEF_D1), beh)
        self.assertEqual(r1.rc, 0, r1.stderr)
        self.assertEqual((r1.init["tools"], r1.init["mcp_servers"]), ([], []))
        self.assertEqual(r1.result["result"], "answer one")
        self.assertEqual(r1.result["num_turns"], 1)
        r2 = rig.run(rig.debrief_argv(prompt=DEBRIEF_D2), beh)
        self.assertEqual(r2.result["result"], "answer two")
        self.assertEqual(rig.db.read(lambda c: c.execute("SELECT COUNT(*) FROM server_starts").fetchone()[0]), 0)

    def test_rule_matches_turn_type(self):
        rig = Rig("wp5_fake_debrief_rule")
        beh = {"rules": [{"match": {"turn_type": "d2"}, "d2": {"text": "special d2"}}]}
        self.assertEqual(rig.run(rig.debrief_argv(prompt=DEBRIEF_D2), beh).result["result"], "special d2")
        self.assertNotEqual(rig.run(rig.debrief_argv(prompt=DEBRIEF_D1), beh).result["result"], "special d2")


class TestModes(unittest.TestCase):
    def test_exit143(self):
        rig = Rig("wp5_fake_143")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "exit143"}})
        self.assertEqual(res.rc, 143)
        self.assertIsNone(res.result)
        self.assertEqual(len(res.of_type("system", "init")), 1)
        self.assertEqual(len(res.of_type("assistant")), 1)
        self.assertTrue(wait_until(lambda: server_pids(rig) == []))

    def test_exit143_debrief(self):
        rig = Rig("wp5_fake_143d")
        res = rig.run(rig.debrief_argv(), {"default": {"mode": "exit143"}})
        self.assertEqual(res.rc, 143)
        self.assertIsNone(res.result)

    def test_crash_after_calls_executes_calls_then_dies(self):
        rig = Rig("wp5_fake_crash")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "crash_after_calls"}})
        self.assertEqual(res.rc, 143)
        self.assertIsNone(res.result)
        self.assertEqual(len(res.tool_uses()), 1)
        self.assertEqual(len(tool_call_rows(rig)), 1)

    def test_truncate(self):
        rig = Rig("wp5_fake_trunc")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "truncate"}})
        self.assertEqual(res.rc, 0)
        self.assertIsNone(res.result)
        last = res.raw_lines[-1]
        self.assertTrue(last.startswith('{"type":"result"'))
        with self.assertRaises(ValueError):
            json.loads(last)

    def test_drop_server(self):
        rig = Rig("wp5_fake_drop")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "drop_server"}})
        self.assertEqual(res.rc, 0, res.stderr)
        self.assertEqual(res.init["tools"], [])
        self.assertEqual(res.init["mcp_servers"][0]["status"], "failed")
        self.assertEqual(res.result["subtype"], "success")
        self.assertEqual(res.tool_uses(), [])
        self.assertEqual(tool_call_rows(rig), [])

    def test_server_that_exits_2_at_startup_shows_failed(self):
        rig = Rig("wp5_fake_badenv")
        res = rig.run(rig.witness_argv(mcp_config=rig.mcp_config(server_env_extra={"WTTH_ROUND": "9",
                                                                                    "WTTH_VISIBLE_FROM": "10"})))
        self.assertEqual(res.rc, 0, res.stderr)
        self.assertEqual(res.init["mcp_servers"][0]["status"], "failed")
        self.assertEqual(res.init["tools"], [])

    def test_server_dies_mid_turn(self):
        rig = Rig("wp5_fake_middie")
        beh = {"default": {"mode": "server_dies_mid_turn", "witness": {"calls": [
            {"tool": "submit_reviews", "args": "auto"}, {"tool": "post_to_board", "args": {"message": "x"}}]}}}
        res = rig.run(rig.witness_argv(), beh)
        self.assertEqual(res.rc, 0, res.stderr)
        trs = res.tool_results()
        self.assertEqual(trs[0]["content"], "Reviews recorded.")
        self.assertFalse(trs[0].get("is_error", False))
        self.assertTrue(trs[1]["is_error"])
        self.assertEqual(trs[1]["content"], "MCP error -32000: Connection closed")
        self.assertEqual(len(tool_call_rows(rig)), 1)
        self.assertTrue(wait_until(lambda: server_pids(rig) == []))

    def test_refusal(self):
        rig = Rig("wp5_fake_refusal")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "refusal"}})
        self.assertEqual(res.rc, 0, res.stderr)
        self.assertEqual(res.tool_uses(), [])
        self.assertEqual(res.result["stop_reason"], "refusal")
        self.assertEqual(res.result["subtype"], "success")

    def test_max_turns(self):
        rig = Rig("wp5_fake_maxturns")
        res = rig.run(rig.witness_argv(max_turns=4), {"default": {"mode": "max_turns"}})
        self.assertEqual(res.rc, 0, res.stderr)
        self.assertEqual(len(res.tool_uses()), 3)
        self.assertEqual(res.result["subtype"], "error_max_turns")
        self.assertTrue(res.result["is_error"])
        self.assertNotIn("result", res.result)

    def test_max_budget(self):
        rig = Rig("wp5_fake_maxbudget")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "max_budget"}})
        self.assertEqual(res.result["subtype"], "error_max_budget_usd")
        self.assertTrue(res.result["is_error"])

    def test_api_errors(self):
        rig = Rig("wp5_fake_apierr")
        for status in (429, 500, 529):
            res = rig.run(rig.witness_argv(), {"default": {"mode": "api_error", "status": status}})
            self.assertEqual(res.result["api_error_status"], status)
            self.assertTrue(res.result["is_error"])
            self.assertEqual(len(res.of_type("system", "api_retry")), 2)
            self.assertEqual(res.tool_uses(), [])
        res = rig.run(rig.witness_argv(), {"default": {"mode": "api_error", "status": 402}})
        self.assertEqual(res.result["api_error_status"], 402)
        self.assertIn("Credit balance is too low", res.result["result"])
        self.assertEqual(res.of_type("system", "api_retry"), [])

    def test_wrong_model_bad_version_permission_denial(self):
        rig = Rig("wp5_fake_wrongs")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "wrong_model"}})
        self.assertEqual(list(res.result["modelUsage"]), ["claude-x"])
        res = rig.run(rig.witness_argv(), {"default": {"mode": "bad_version"}})
        self.assertEqual(res.init["claude_code_version"], "2.1.318")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "permission_denial"}})
        self.assertEqual(res.result["permission_denials"][0]["tool_name"], "mcp__workspace__notify_ops")

    def test_stderr_noise(self):
        rig = Rig("wp5_fake_noise")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "stderr_noise"}})
        lines = [ln for ln in res.stderr.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("⚠ Permission mode forced"))

    def test_slow(self):
        rig = Rig("wp5_fake_slow")
        t0 = time.time()
        res = rig.run(rig.witness_argv(), {"default": {"mode": "slow", "sleep_s": 1.5}})
        self.assertGreaterEqual(time.time() - t0, 1.5)
        self.assertEqual(res.result["subtype"], "success")
        self.assertGreaterEqual(res.result["duration_ms"], 1500)

    def test_hang_stays_alive_until_killed_and_server_goes_with_group(self):
        rig = Rig("wp5_fake_hang")
        p = rig.popen(rig.witness_argv(), {"default": {"mode": "hang"}})
        try:
            out = read_until(p, b'"subtype":"init"')
            self.assertIn(b'"subtype":"init"', out)
            time.sleep(1.0)
            self.assertIsNone(p.poll())
            self.assertGreaterEqual(len(server_pids(rig)), 1)
            os.killpg(p.pid, signal.SIGINT)
            self.assertIsNotNone(p.wait(timeout=15))
        finally:
            rig.reap(p)
        self.assertTrue(wait_until(lambda: server_pids(rig) == []))

    def test_hang_hard_ignores_int_and_term(self):
        rig = Rig("wp5_fake_hanghard")
        p = rig.popen(rig.witness_argv(), {"default": {"mode": "hang_hard"}})
        try:
            read_until(p, b'"subtype":"init"')
            time.sleep(0.5)
            # signal only the fake (not the group): the python process is the shim's exec target, so p.pid
            p.send_signal(signal.SIGINT)
            p.send_signal(signal.SIGTERM)
            time.sleep(1.5)
            self.assertIsNone(p.poll(), "hang_hard exited on SIGINT/SIGTERM")
        finally:
            rig.reap(p)
        self.assertTrue(wait_until(lambda: server_pids(rig) == []))

    def test_unknown_mode_is_a_contract_violation(self):
        rig = Rig("wp5_fake_unknownmode")
        res = rig.run(rig.witness_argv(), {"default": {"mode": "nonsense"}})
        self.assertEqual(res.rc, 64)
        self.assertIn("FAKE_CONTRACT_VIOLATION", res.stderr)


class TestAuditModeBodies(unittest.TestCase):
    def test_audit_env_writes_request_bodies_and_otel_noise(self):
        rig = Rig("wp5_fake_bodies")
        res = rig.run(rig.witness_argv(), audit=True)
        self.assertEqual(res.rc, 0, res.stderr)
        files = sorted(rig.bodies.rglob("*.request.json"))
        self.assertEqual(len(files), 2)  # one request per assistant step
        body = json.loads(files[0].read_text())
        self.assertEqual(len(body["system"]), 3)
        self.assertEqual(body["system"][1]["text"], "You are a Claude agent, built on Anthropic's Claude Agent SDK.")
        self.assertEqual(body["system"][2]["text"], "You are a test system prompt.")
        self.assertEqual([t["name"] for t in body["tools"]],
                         ["mcp__workspace__submit_reviews", "mcp__workspace__post_to_board", "mcp__workspace__notify_ops"])
        self.assertEqual(body["messages"][0]["content"].splitlines()[0], "Round 2 of 5 · shift clock 21:30")
        self.assertTrue(any(not ln.startswith("{") for ln in res.raw_lines))  # OTEL console noise on stdout
        self.assertNotIn(DUMMY_KEY.encode(), b"".join(f.read_bytes() for f in rig.bodies.rglob("*") if f.is_file()))

    def test_debrief_body_has_no_tools(self):
        rig = Rig("wp5_fake_bodies_d")
        rig.run(rig.debrief_argv(), audit=True)
        files = sorted(rig.bodies.rglob("*.request.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["tools"], [])


class TestContractViolations(unittest.TestCase):
    def expect_violation(self, res, fragment: str):
        self.assertEqual(res.rc, 64, res.stderr)
        self.assertTrue(all(ln.startswith("FAKE_CONTRACT_VIOLATION: ") for ln in res.stderr.splitlines()
                            if ln.strip() and not ln.startswith("\u26a0")), res.stderr)
        self.assertIn(fragment, res.stderr)
        self.assertEqual(res.raw_lines, [])  # nothing on stdout

    def test_missing_flag(self):
        rig = Rig("wp5_fake_cv_flag")
        argv = [a for a in rig.witness_argv() if a != "--bare"]
        self.expect_violation(rig.run(argv), "argv")

    def test_tools_must_be_a_separate_empty_element(self):
        rig = Rig("wp5_fake_cv_tools")
        argv = rig.witness_argv()
        i = argv.index("--tools")
        argv[i:i + 2] = ["--tools="]
        self.expect_violation(rig.run(argv), "argv")

    def test_wrong_order(self):
        rig = Rig("wp5_fake_cv_order")
        argv = rig.witness_argv()
        i = argv.index("--effort")
        argv[i:i + 2], argv[i + 2:i + 4] = argv[i + 2:i + 4], argv[i:i + 2]
        self.expect_violation(rig.run(argv), "argv")

    def test_bad_model_effort_and_permission_mode(self):
        rig = Rig("wp5_fake_cv_vals")
        a = rig.witness_argv(model="claude-opus-9")
        self.expect_violation(rig.run(a), "model")
        a = rig.witness_argv()
        a[a.index("--effort") + 1] = "high"
        self.expect_violation(rig.run(a), "effort")
        a = rig.witness_argv()
        a[a.index("--permission-mode") + 1] = "default"
        self.expect_violation(rig.run(a), "permission")

    def test_debrief_with_mcp_flags_or_witness_without(self):
        rig = Rig("wp5_fake_cv_shape")
        argv = rig.witness_argv()
        i = argv.index("--mcp-config")
        del argv[i:i + 4]  # witness shape minus mcp-config and allowedTools == debrief shape: valid
        self.assertEqual(rig.run(argv).rc, 0)

    def test_system_file_must_exist_and_be_readonly(self):
        rig = Rig("wp5_fake_cv_sys")
        rig.system.chmod(0o644)
        self.expect_violation(rig.run(rig.witness_argv()), "read-only")
        rig.system.chmod(0o444)
        argv = rig.witness_argv()
        argv[argv.index("--system-prompt-file") + 1] = str(rig.root / "missing.txt")
        self.expect_violation(rig.run(argv), "does not exist")

    def test_stdin_must_be_dev_null(self):
        rig = Rig("wp5_fake_cv_stdin")
        self.expect_violation(rig.run(rig.witness_argv(), stdin_devnull=False), "stdin")

    def test_cwd_must_match(self):
        rig = Rig("wp5_fake_cv_cwd")
        self.expect_violation(rig.run(rig.witness_argv(), cwd=rig.root), "cwd")

    def test_env_must_equal_allowlist(self):
        rig = Rig("wp5_fake_cv_env")
        env = rig.env()
        env["EXTRA_VAR"] = "1"
        self.expect_violation(rig.run(rig.witness_argv(), env=env), "unexpected names EXTRA_VAR")
        env = rig.env()
        del env["TZ"]
        self.expect_violation(rig.run(rig.witness_argv(), env=env), "missing names TZ")
        env = rig.env()
        env["PATH"] = "/usr/local/bin:/usr/bin"
        self.expect_violation(rig.run(rig.witness_argv(), env=env), "PATH")

    def test_audit_names_only_allowed_with_telemetry(self):
        rig = Rig("wp5_fake_cv_audit")
        env = rig.env()
        env["OTEL_LOGS_EXPORTER"] = "console"
        self.expect_violation(rig.run(rig.witness_argv(), env=env), "OTEL_LOGS_EXPORTER")
        env = rig.env(audit=True)
        del env["OTEL_METRICS_EXPORTER"]
        self.expect_violation(rig.run(rig.witness_argv(), env=env), "OTEL_METRICS_EXPORTER")

    def test_wrong_api_key(self):
        rig = Rig("wp5_fake_cv_key")
        env = rig.env()
        env["ANTHROPIC_API_KEY"] = DUMMY_KEY + "X"
        res = rig.run(rig.witness_argv(), env=env)
        self.expect_violation(res, "ANTHROPIC_API_KEY")
        self.assertNotIn(DUMMY_KEY, res.stderr)  # the key is never echoed

    def test_slot_dirs_must_be_under_the_slots_root(self):
        rig = Rig("wp5_fake_cv_slotroot")
        elsewhere = rig.root / "elsewhere"
        for d in ("home", "config", "tmp"):
            (elsewhere / d).mkdir(parents=True)
        env = rig.env(extra={"HOME": str(elsewhere / "home"), "CLAUDE_CONFIG_DIR": str(elsewhere / "config"),
                             "TMPDIR": str(elsewhere / "tmp")})
        self.expect_violation(rig.run(rig.witness_argv(), env=env), "slots root")

    def test_two_fakes_cannot_share_a_slot(self):
        rig = Rig("wp5_fake_cv_slotbusy")
        p = rig.popen(rig.witness_argv(), {"default": {"mode": "hang"}})
        try:
            read_until(p, b'"subtype":"init"')
            res = rig.run(rig.debrief_argv())
            self.expect_violation(res, "another running fake")
        finally:
            rig.reap(p)
        self.assertTrue(wait_until(lambda: server_pids(rig) == []))

    def test_mcp_config_with_credential_like_env_name(self):
        rig = Rig("wp5_fake_cv_mcpenv")
        cfg = rig.mcp_config(server_env_extra={"FOO_TOKEN": "x"})
        self.expect_violation(rig.run(rig.witness_argv(mcp_config=cfg)), "credential-like")

    def test_allowed_tools_must_match_server_listing(self):
        rig = Rig("wp5_fake_cv_allowed")
        res = rig.run(rig.witness_argv(allowed="mcp__workspace__submit_reviews"))
        self.expect_violation(res, "--allowedTools")

    def test_empty_prompt_and_bad_behavior_file(self):
        rig = Rig("wp5_fake_cv_misc")
        self.expect_violation(rig.run(rig.witness_argv(prompt="")), "empty prompt")
        rig.behavior_path.write_text("{not json", encoding="utf-8")
        from tests.util import PYTHON, REPO
        proc = subprocess.run([PYTHON, "-I", str(REPO / "tests" / "fake_claude.py"), "--behavior",
                               str(rig.behavior_path), *rig.witness_argv()], cwd=str(rig.cwd), env=rig.env(),
                              stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(proc.returncode, 64)
        proc = subprocess.run([PYTHON, "-I", str(REPO / "tests" / "fake_claude.py"), *rig.witness_argv()],
                              cwd=str(rig.cwd), env=rig.env(), stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(proc.returncode, 64)

    def test_contract_can_be_skipped_per_check(self):
        rig = Rig("wp5_fake_cv_skip")
        env = rig.env()
        env["EXTRA_VAR"] = "1"
        c = rig.contract()
        c["skip"] = ["env"]
        rig.write_behavior({}, contract=c)
        res = rig.run(rig.debrief_argv(), env=env, behavior=None) if False else None  # run() rewrites the behaviour
        beh_path = rig.behavior_path
        from tests.util import PYTHON, REPO
        proc = subprocess.run([PYTHON, "-I", str(REPO / "tests" / "fake_claude.py"), "--behavior", str(beh_path),
                               *rig.debrief_argv()], cwd=str(rig.cwd), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(res)


class TestNoKeyLeak(unittest.TestCase):
    def test_key_never_in_any_file_or_output_of_a_run(self):
        rig = Rig("wp5_fake_nokey")
        res = rig.run(rig.witness_argv(), audit=True)
        self.assertEqual(res.rc, 0)
        blob = (res.stderr + "\n".join(res.raw_lines)).encode()
        for f in rig.root.rglob("*"):
            if f.is_file() and f.name != "behavior.json":
                blob += f.read_bytes()
        self.assertNotIn(DUMMY_KEY.encode(), blob)
        self.assertNotIn(("sk-" + "ant").encode(), blob)
        beh = rig.behavior_path.read_text()
        self.assertIn(DUMMY_KEY, beh)  # the contract section holds the dummy only, never a real key


if __name__ == "__main__":
    unittest.main()
