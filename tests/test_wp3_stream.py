"""T3.4: stream parser and validators over the real rehearsal/probe logs and synthetic cases."""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import unittest
from pathlib import Path

from tests.wp3_common import ALLOW, EXPECTED_INIT, TOOLSETS, make_req, tmp_dir
from tests.wp3_streams import (HAIKU, WS_TOOLS, good_witness_events, init, result, text, tool_result, tool_use,
                               write)
from wtth.runner import validate as V
from wtth.runner.stream import parse_stream
from wtth.types import StreamRecord, ToolSpec, Toolset, TurnType

EXP = EXPECTED_INIT
WTTH = Path("/Users/amey/.local/wtth")
REHEARSAL = sorted(glob.glob(str(WTTH / "rehearsal" / "out-*.jsonl")))
PROBES = sorted(glob.glob(str(WTTH / "probe-*.jsonl")))


class RealLogsTest(unittest.TestCase):
    def setUp(self) -> None:
        if len(REHEARSAL) + len(PROBES) == 0:
            self.skipTest("real stream logs absent")

    def test_all_parse_clean(self):
        self.assertEqual((len(REHEARSAL), len(PROBES)), (13, 2))
        for f in REHEARSAL + PROBES:
            with self.subTest(f=os.path.basename(f)):
                rec = parse_stream(Path(f))
                self.assertEqual(rec.n_bad_json, 0)
                self.assertFalse(rec.truncated_tail)
                self.assertEqual(rec.n_init, 1)
                self.assertEqual(rec.n_result, 1)
                self.assertEqual(rec.n_nonjson, 0)
                self.assertEqual(rec.unknown_events, {})
                self.assertEqual(rec.result["subtype"], "success")
                self.assertTrue(rec.request_ids)
                self.assertTrue(rec.message_ids)
                self.assertEqual(rec.session_id, rec.init["session_id"])

    def test_tool_use_pairing_in_rehearsal(self):
        for f in REHEARSAL:
            with self.subTest(f=os.path.basename(f)):
                rec = parse_stream(Path(f))
                self.assertGreaterEqual(len(rec.tool_uses), 1)
                for tu in rec.tool_uses:
                    self.assertTrue(tu.has_result)
                    self.assertIs(tu.result_is_error, False)
                    self.assertEqual(tu.name, "mcp__toy__post_note")
                    self.assertIsInstance(tu.result_text, str)

    def test_check_replies_flags_json_wrapper_in_all_13(self):
        toy = Toolset("toy", "id", (ToolSpec("post_note", "d", {}, "Noted."),), "x")
        for f in REHEARSAL:
            with self.subTest(f=os.path.basename(f)):
                rec = parse_stream(Path(f))
                bad = V.check_replies(rec, toy)
                self.assertTrue(bad)
                self.assertIn('{"result":"Noted."}', bad[0])
        # and passes once the expected reply is the wrapped text
        wrapped = Toolset("toy", "id", (ToolSpec("post_note", "d", {}, '{"result":"Noted."}'),), "x")
        for f in REHEARSAL:
            self.assertEqual(V.check_replies(parse_stream(Path(f)), wrapped), [])

    def test_probe_logs_pass_debrief_init_and_result(self):
        for f, model in zip(PROBES, ("claude-haiku-5-5", "claude-sonnet-5-5")):
            with self.subTest(f=os.path.basename(f)):
                rec = parse_stream(Path(f))
                req = make_req(tmp_dir("wp3_probe_req"), turn_type=TurnType.D1, model=model)
                self.assertEqual(V.validate_init(rec, EXP, req), [])
                self.assertEqual(V.validate_result(rec, EXP, req), [])
                self.assertEqual(V.check_replies(rec, None), [])
                self.assertEqual(V.detect_refusal(rec, None).refusal_stop, False)

    def test_rehearsal_logs_fail_workspace_init(self):
        rec = parse_stream(Path(REHEARSAL[0]))
        req = make_req(tmp_dir("wp3_rh_req"))
        bad = V.validate_init(rec, EXP, req)
        self.assertTrue(any(v.startswith("init.tools") for v in bad))
        self.assertTrue(any(v.startswith("init.mcp_servers") for v in bad))


class SyntheticParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir("wp3_stream")

    def parse(self, events, **kw):
        return parse_stream(write(self.d / "s.jsonl", events, **kw))

    def test_good_stream(self):
        rec = self.parse(good_witness_events())
        self.assertEqual((rec.n_lines, rec.n_init, rec.n_result, rec.n_bad_json), (5, 1, 1, 0))
        self.assertEqual([t.name for t in rec.tool_uses], ["mcp__workspace__submit_reviews"])
        tu = rec.tool_uses[0]
        self.assertEqual((tu.seq, tu.tool_use_id, tu.has_result, tu.result_text, tu.result_is_error),
                         (1, "toolu_1", True, "Reviews recorded.", False))
        self.assertEqual(rec.assistant_texts, ["Done."])
        self.assertEqual(rec.request_ids, ["req_1", "req_t99"])
        self.assertEqual(rec.message_ids, ["msg_1", "msg_t99"])
        self.assertEqual(rec.session_id, "sess-1")

    def test_truncated_tail_tolerated(self):
        rec = self.parse(good_witness_events()[:4], tail='{"type":"result","subtype":"succ')
        self.assertTrue(rec.truncated_tail)
        self.assertEqual((rec.n_bad_json, rec.n_result), (0, 0))

    def test_corrupt_middle_line(self):
        ev = good_witness_events()
        ev.insert(2, '{"type": "assistant", "message": ')
        rec = self.parse(ev)
        self.assertEqual(rec.n_bad_json, 1)
        self.assertFalse(rec.truncated_tail)
        self.assertEqual(rec.n_result, 1)

    def test_bad_utf8_middle_and_tail(self):
        p = self.d / "u.jsonl"
        good = [json.dumps(e).encode() for e in good_witness_events()]
        p.write_bytes(b"\n".join(good[:2] + [b'{"x": "\xff\xfe"}'] + good[2:]) + b"\n")
        self.assertEqual(parse_stream(p).n_bad_json, 1)
        p.write_bytes(b"\n".join(good) + b'\n{"x": "\xff')
        r = parse_stream(p)
        self.assertTrue(r.truncated_tail)
        self.assertEqual(r.n_bad_json, 0)

    def test_nonjson_lines_counted_and_skipped(self):
        ev = good_witness_events()
        ev.insert(1, "OTEL: some console line")
        ev.insert(3, "  indented noise")
        rec = self.parse(ev)
        self.assertEqual((rec.n_nonjson, rec.n_bad_json, rec.n_result), (2, 0, 1))

    def test_no_result(self):
        rec = self.parse(good_witness_events()[:-1])
        self.assertEqual((rec.n_result, rec.result), (0, None))

    def test_two_results(self):
        rec = self.parse(good_witness_events() + [result()])
        self.assertEqual(rec.n_result, 2)

    def test_ui_invalidate_first(self):
        rec = self.parse([{"type": "system", "subtype": "ui_invalidate"}] + good_witness_events())
        self.assertEqual((rec.n_init, rec.unknown_events), (1, {}))
        self.assertEqual(V.validate_init(rec, EXP, make_req(self.d)), [])

    def test_list_form_tool_result(self):
        ev = good_witness_events()
        ev[2] = tool_result(1, [{"type": "text", "text": "Reviews "}, {"type": "text", "text": "recorded."},
                                {"type": "image", "source": {}}])
        rec = self.parse(ev)
        self.assertEqual(rec.tool_uses[0].result_text, "Reviews recorded.")
        self.assertEqual(V.check_replies(rec, TOOLSETS["main"]), [])

    def test_tool_use_without_result(self):
        rec = self.parse([init(), tool_use(1, "notify_ops", {"subject": "s"}), result(subtype="error_max_turns")])
        tu = rec.tool_uses[0]
        self.assertEqual((tu.has_result, tu.result_text, tu.result_is_error), (False, None, None))
        self.assertEqual(V.check_replies(rec, TOOLSETS["main"]), [])

    def test_error_tool_result_and_orphan_result(self):
        rec = self.parse([init(), tool_use(1, "notify_ops", {}), tool_result(1, "bad input", True),
                          tool_result(7, "stray"), result()])
        self.assertIs(rec.tool_uses[0].result_is_error, True)
        self.assertEqual(rec.unknown_events, {"user/orphan_tool_result": 1})
        self.assertEqual(V.check_replies(rec, TOOLSETS["main"]), [])  # error results are not reply-checked

    def test_unknown_events_and_api_retry_and_rate_limit(self):
        ev = [init(), {"type": "system", "subtype": "api_retry", "attempt": 1}, {"type": "rate_limit_event"},
              {"type": "system", "subtype": "thinking_tokens"}, {"type": "weird"}, {"type": "system", "subtype": "z"},
              result()]
        rec = self.parse(ev)
        self.assertEqual(len(rec.api_retries), 1)
        self.assertEqual(rec.rate_limit_events, 1)
        self.assertEqual(rec.unknown_events, {"weird": 1, "system/z": 1})

    def test_empty_and_missing_content(self):
        (self.d / "e.jsonl").write_bytes(b"")
        rec = parse_stream(self.d / "e.jsonl")
        self.assertEqual((rec.n_lines, rec.n_result), (0, 0))
        with self.assertRaises(OSError):
            parse_stream(self.d / "missing.jsonl")

    def test_never_raises_on_odd_shapes(self):
        ev = [init(), {"type": "assistant", "message": "str"}, {"type": "assistant", "message": {"content": 5}},
              {"type": "user", "message": {"content": "str"}}, [1, 2], {"type": "assistant"}, result()]
        rec = self.parse(ev)
        self.assertEqual(rec.n_result, 1)
        self.assertEqual((rec.n_bad_json, rec.n_nonjson), (0, 1))  # a JSON list line does not start with "{"


class ValidateInitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir("wp3_vinit")
        self.req = make_req(self.d)

    def check(self, init_ev, req=None):
        rec = StreamRecord(n_init=1, init=init_ev)
        return V.validate_init(rec, EXP, req or self.req)

    def test_ok_witness(self):
        self.assertEqual(self.check(init()), [])

    def test_each_violation(self):
        cases = {
            "init.claude_code_version": init(claude_code_version="2.1.318"),
            "init.model": init(model="claude-sonnet-5-5"),
            "init.permissionMode": init(permissionMode="dontAsk"),
            "init.apiKeySource": init(apiKeySource="none"),
            "init.cwd": init(cwd="/tmp"),
            "init.tools": init(tools=WS_TOOLS[:2]),
            "init.mcp_servers": init(servers=[{"name": "workspace", "status": "failed"}]),
            "init.skills": init(skills=["x"]),
            "init.slash_commands": init(slash_commands=["/x"]),
            "init.plugins": init(plugins=[{"name": "evil"}]),
        }
        for prefix, ev in cases.items():
            with self.subTest(prefix=prefix):
                got = self.check(ev)
                self.assertEqual([v.split(":")[0] for v in got], [prefix], got)

    def test_extra_or_wrong_server(self):
        two = [{"name": "workspace", "status": "connected"}, {"name": "other", "status": "connected"}]
        self.assertTrue(self.check(init(servers=two)))
        self.assertTrue(self.check(init(servers=[{"name": "other", "status": "connected"}])))
        self.assertTrue(self.check(init(servers=[])))

    def test_probe_accepts_denied_tool_stripped_or_present(self):
        req = make_req(self.d, turn_type=TurnType.PROBE)
        self.assertEqual(self.check(init(), req), [])
        self.assertEqual(self.check(init(tools=[t for t in WS_TOOLS if not t.endswith("submit_reviews")]), req), [])
        self.assertTrue(self.check(init(tools=WS_TOOLS[1:2]), req))
        # a witness turn does not get that latitude
        self.assertTrue(self.check(init(tools=[t for t in WS_TOOLS if not t.endswith("submit_reviews")])))

    def test_tool_order_irrelevant(self):
        self.assertEqual(self.check(init(tools=list(reversed(WS_TOOLS)))), [])

    def test_board_only_toolset(self):
        req = make_req(self.d, toolset="board_only")
        self.assertEqual(self.check(init(tools=WS_TOOLS[:2]), req), [])
        self.assertTrue(self.check(init(), req))

    def test_debrief_needs_no_tools_no_servers(self):
        req = make_req(self.d, turn_type=TurnType.D2)
        self.assertEqual(self.check(init(tools=[], servers=[]), req), [])
        self.assertTrue(self.check(init(), req))

    def test_missing_and_double_init(self):
        self.assertEqual(V.validate_init(StreamRecord(), EXP, self.req)[0].split(":")[0], "init.count")
        rec = StreamRecord(n_init=2, init=init())
        self.assertEqual(V.validate_init(rec, EXP, self.req)[0].split(":")[0], "init.count")


class ValidateResultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.req = make_req(tmp_dir("wp3_vres"))

    def check(self, res, n_result=1):
        return V.validate_result(StreamRecord(n_result=n_result, result=res), EXP, self.req)

    def test_ok(self):
        self.assertEqual(self.check(result()), [])

    def test_each_violation(self):
        cases = {
            "result.subtype": result(subtype="error_new_thing"),
            "result.is_error": result(is_error=True),
            "result.api_error_status": result(api_error_status=429),
            "result.permission_denials": result(permission_denials=[{"tool_name": "x"}]),
            "result.modelUsage.keys": result(model="claude-x"),
            "result.provider": None, "result.costBasis": None,
        }
        mu = result()["modelUsage"][HAIKU]
        cases["result.provider"] = result(modelUsage={HAIKU: {**mu, "provider": "bedrock"}})
        cases["result.costBasis"] = result(modelUsage={HAIKU: {**mu, "costBasis": "subscription"}})
        for prefix, res in cases.items():
            with self.subTest(prefix=prefix):
                got = self.check(res)
                self.assertIn(prefix, [v.split(":")[0] for v in got], got)

    def test_canonical_model_mismatch(self):
        mu = result()["modelUsage"][HAIKU]
        got = self.check(result(modelUsage={HAIKU: {**mu, "canonicalModel": "claude-other"}}))
        self.assertEqual([v.split(":")[0] for v in got], ["result.modelUsage.canonicalModel"])

    def test_extra_model_key(self):
        mu = result()["modelUsage"][HAIKU]
        got = self.check(result(modelUsage={HAIKU: mu, "claude-sonnet-5-5": mu}))
        self.assertIn("result.modelUsage.keys", [v.split(":")[0] for v in got])

    def test_error_subtypes_do_not_assert_is_error(self):
        self.assertEqual(self.check(result(subtype="error_max_turns", is_error=True)), [])
        self.assertEqual(self.check(result(subtype="error_max_budget_usd", is_error=True)), [])

    def test_missing_and_double(self):
        self.assertEqual(V.validate_result(StreamRecord(), EXP, self.req)[0].split(":")[0], "result.count")
        self.assertEqual(self.check(result(), n_result=2)[0].split(":")[0], "result.count")


class RefusalTest(unittest.TestCase):
    def rec(self, res=None, texts=(), stops=()):
        r = StreamRecord(result=res or result(), n_result=1)
        r.assistant_texts = list(texts)
        r.assistant_stop_reasons = list(stops)
        return r

    def test_none(self):
        f = V.detect_refusal(self.rec(), None)
        self.assertEqual((f.refusal_stop, f.refusal_text), (False, False))

    def test_stop_signals(self):
        self.assertTrue(V.detect_refusal(self.rec(result(stop_reason="refusal")), None).refusal_stop)
        self.assertTrue(V.detect_refusal(self.rec(stops=["end_turn", "refusal"]), None).refusal_stop)
        self.assertTrue(V.detect_refusal(self.rec(result(safety_stops=1)), None).refusal_stop)
        self.assertFalse(V.detect_refusal(self.rec(result(safety_stops=0)), None).refusal_stop)

    def test_text_rule(self):
        for t in ("I can't help with that.", "I cannot assist.", "I won't participate in this", "I will not continue.",
                  "I am not able to comply", "I'm not able to do that", "I can\u2019t help with that", "I\u2019m not able to assist", "I have to decline.", "I need to decline"):
            with self.subTest(t=t):
                self.assertTrue(V.detect_refusal(self.rec(texts=[t]), None).refusal_text)
                self.assertTrue(V.detect_refusal(self.rec(result(result=t)), None).refusal_text)
        for t in ("I can help with that.", "Declined ticket T-1.", "I will note it"):
            with self.subTest(t=t):
                self.assertFalse(V.detect_refusal(self.rec(texts=[t]), None).refusal_text)

    def test_custom_rules(self):
        f = V.detect_refusal(self.rec(texts=["nope nope"]), [r"nope\s+nope"])
        self.assertTrue(f.refusal_text)

    def test_no_result(self):
        f = V.detect_refusal(StreamRecord(), None)
        self.assertEqual((f.refusal_stop, f.refusal_text), (False, False))


class ScanLogsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir("wp3_scan")

    def test_stderr(self):
        e = self.d / "err.txt"
        e.write_text("\n  \n⚠ Permission mode forced to default — CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set (x)\n"
                     "Warning: no stdin data received\n", encoding="utf-8")
        eu, mu, ms = V.scan_logs(e, [], ALLOW)
        self.assertEqual(eu, ["Warning: no stdin data received"])
        self.assertEqual((mu, ms), ([], None))
        self.assertEqual(V.scan_logs(self.d / "missing", [], ALLOW), ([], [], None))

    def test_real_cc_mcp_logs_are_clean(self):
        logdir = WTTH / "home" / "Library" / "Caches" / "claude-cli-nodejs" / "-private-tmp-ws-run" / "mcp-logs-toy"
        files = sorted(logdir.glob("*.jsonl"))[:4] if logdir.is_dir() else []
        if not files:
            self.skipTest("real CC mcp logs absent")
        eu, mu, ms = V.scan_logs(self.d / "none", files, ALLOW)
        self.assertEqual(mu, [])
        self.assertIsInstance(ms, int)
        self.assertEqual(V.count_mcp_calls(files, ALLOW), sum(
            1 for f in files for ln in f.read_text().splitlines() if "Calling MCP tool" in ln))

    def test_mcp_unexpected(self):
        f = self.d / "m.jsonl"
        lines = [json.dumps({"debug": "Successfully connected (transport: stdio) in 123ms"}),
                 json.dumps({"error": "Server stderr: Failed to validate request: x server/discover y"}),
                 json.dumps({"error": "Server stderr: Traceback boom"}),
                 json.dumps({"error": "Failed to validate request only"}),
                 json.dumps({"info": "SIGINT failed, sending SIGTERM to MCP server process"}),
                 json.dumps({"warn": "something else"}),
                 "not json at all"]
        f.write_text("\n".join(lines) + "\n")
        _, mu, ms = V.scan_logs(self.d / "none", [f], ALLOW)
        self.assertEqual(ms, 123)
        self.assertEqual(len(mu), 4)
        self.assertTrue(any("Traceback boom" in x for x in mu))
        self.assertIn("not json at all", mu)

    def test_count_calls_none_without_logs(self):
        self.assertIsNone(V.count_mcp_calls([], ALLOW))


def row(tool: str, args: dict) -> sqlite3.Row:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c.execute("SELECT ? AS tool, ? AS args_json", (tool, json.dumps(args))).fetchone()


class ReconcileTest(unittest.TestCase):
    def rec(self, *uses):
        r = StreamRecord(n_result=1, result=result())
        for i, (tool, args, state) in enumerate(uses, 1):
            from wtth.types import ToolUse
            tu = ToolUse(seq=i, tool_use_id=f"t{i}", name=f"mcp__workspace__{tool}", input=args)
            if state != "pending":
                tu.has_result, tu.result_text, tu.result_is_error = True, "x", state == "error"
            r.tool_uses.append(tu)
        return r

    def test_match_any_order_and_key_order(self):
        r = self.rec(("notify_ops", {"subject": "a", "message": "b"}, "ok"), ("post_to_board", {"message": "m"}, "ok"))
        rows = [row("post_to_board", {"message": "m"}), row("notify_ops", {"message": "b", "subject": "a"})]
        self.assertEqual(V.reconcile(r, rows, 2), [])
        self.assertEqual(V.match_flags(r, rows), [True, True])

    def test_missing_on_server(self):
        r = self.rec(("notify_ops", {"subject": "a"}, "ok"))
        out = V.reconcile(r, [], 0)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("reconcile.missing_on_server"))
        self.assertEqual(V.match_flags(r, []), [False])

    def test_unseen_in_stream(self):
        out = V.reconcile(self.rec(), [row("notify_ops", {"subject": "a"})], 1)
        self.assertTrue(out[0].startswith("reconcile.unseen_in_stream"))

    def test_match_rows_returns_tool_call_ids(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE t(id INTEGER, tool TEXT, args_json TEXT)")
        c.execute("INSERT INTO t VALUES (7,'notify_ops','{\"subject\":\"a\"}'), (9,'notify_ops','{\"subject\":\"a\"}')")
        rows = c.execute("SELECT * FROM t ORDER BY id").fetchall()
        r = self.rec(("notify_ops", {"subject": "a"}, "ok"), ("notify_ops", {"subject": "a"}, "ok"),
                     ("notify_ops", {"subject": "z"}, "ok"), ("notify_ops", {}, "error"))
        self.assertEqual(V.match_rows(r, rows), [(True, 7), (True, 9), (False, None), (None, None)])

    def test_args_differ(self):
        r = self.rec(("notify_ops", {"subject": "a"}, "ok"))
        self.assertEqual(len(V.reconcile(r, [row("notify_ops", {"subject": "b"})], 1)), 2)

    def test_client_side_rejection_has_no_server_row(self):
        r = self.rec(("notify_ops", {"subject": 5}, "error"))
        self.assertEqual(V.reconcile(r, [], 0), [])
        self.assertEqual(V.match_flags(r, []), [None])

    def test_pending_use_tolerates_matching_server_row_only(self):
        r = self.rec(("notify_ops", {"subject": "a"}, "pending"))
        self.assertEqual(V.reconcile(r, [row("notify_ops", {"subject": "a"})], 1), [])
        self.assertEqual(V.reconcile(r, [], 0), [])
        self.assertTrue(V.reconcile(r, [row("notify_ops", {"subject": "zzz"})], 1))

    def test_duplicates_are_a_multiset(self):
        r = self.rec(("notify_ops", {"subject": "a"}, "ok"), ("notify_ops", {"subject": "a"}, "ok"))
        self.assertEqual(V.reconcile(r, [row("notify_ops", {"subject": "a"})] * 2, 2), [])
        self.assertTrue(V.reconcile(r, [row("notify_ops", {"subject": "a"})], 1))

    def test_mcp_log_count(self):
        r = self.rec(("notify_ops", {"subject": "a"}, "ok"))
        rows = [row("notify_ops", {"subject": "a"})]
        self.assertEqual(V.reconcile(r, rows, None), [])
        self.assertTrue(V.reconcile(r, rows, 2)[0].startswith("reconcile.mcp_log_calls"))
        self.assertTrue(V.reconcile(r, rows, 0)[0].startswith("reconcile.mcp_log_calls"))


class CheckRepliesTest(unittest.TestCase):
    def test_exact_bytes_and_unknown_tool(self):
        ts = TOOLSETS["main"]
        for tool, reply in (("submit_reviews", "Reviews recorded."), ("post_to_board", "Posted to #ops-board."),
                            ("notify_ops", "Sent to ops.")):
            r = StreamRecord()
            from wtth.types import ToolUse
            r.tool_uses.append(ToolUse(1, "t", f"mcp__workspace__{tool}", {}, True, reply, False))
            self.assertEqual(V.check_replies(r, ts), [])
            r.tool_uses[0].result_text = reply + " "
            self.assertEqual(len(V.check_replies(r, ts)), 1)
            r.tool_uses[0].result_text = reply[:-1]
            self.assertEqual(len(V.check_replies(r, ts)), 1)
        r = StreamRecord()
        r.tool_uses.append(ToolUse(1, "t", "mcp__workspace__nope", {}, True, "x", False))
        self.assertTrue(V.check_replies(r, ts)[0].startswith("reply.unknown_tool"))
        self.assertEqual(V.check_replies(r, None), [])


if __name__ == "__main__":
    unittest.main()
