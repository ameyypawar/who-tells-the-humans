"""T3.5: every research/41 section 2.6 row in order, plus precedence."""
from __future__ import annotations

import unittest

from tests.wp3_common import EXPECTED_INIT, TOOLSETS, make_req, tmp_dir
from tests.wp3_streams import (HAIKU, good_witness_events, init, proc, result, text, tool_result, tool_use, write)
from wtth.runner import validate as V
from wtth.runner.classify import classify
from wtth.runner.stream import parse_stream
from wtth.types import FailClass as F
from wtth.types import ProcResult, TurnClass as C, TurnType

D = tmp_dir("wp3_classify")
N = [0]


def run(events, *, p: ProcResult | None = None, fault=False, cancelled=False, reconcile_v=None, turn_type=TurnType.WITNESS,
        tail=""):
    N[0] += 1
    rec = parse_stream(write(D / f"s{N[0]}.jsonl", events, tail=tail))
    req = make_req(D, turn_type=turn_type)
    init_v = V.validate_init(rec, EXPECTED_INIT, req)
    result_v = V.validate_result(rec, EXPECTED_INIT, req)
    reply_v = V.check_replies(rec, req.toolset)
    refusal = V.detect_refusal(rec, None)
    return classify(p or proc(), rec, init_v=init_v, result_v=result_v, reply_v=reply_v,
                    reconcile_v=reconcile_v or [], server_fault=fault, cancelled=cancelled, refusal=refusal)


def with_result(**over):
    ev = good_witness_events()
    ev[-1] = result(**over)
    return ev


class RowsTest(unittest.TestCase):
    def test_row1_cancelled(self):
        self.assertEqual(run(good_witness_events(), cancelled=True)[:2], (C.TECHNICAL, F.CANCELLED))

    def test_row2_timeout(self):
        c, f, d = run(good_witness_events()[:2], p=proc(None, 2, True, "sigint"))
        self.assertEqual((c, f), (C.TECHNICAL, F.TIMEOUT))
        self.assertIn("sigint", d)

    def test_row3_server_fault(self):
        self.assertEqual(run(good_witness_events(), fault=True)[:2], (C.TECHNICAL, F.SERVER_FAULT))

    def test_row4_no_result_and_exits(self):
        ev = good_witness_events()[:-1]
        self.assertEqual(run(ev)[:2], (C.TECHNICAL, F.NO_RESULT))
        c, f, d = run(ev, p=proc(143))
        self.assertEqual((c, f, d), (C.TECHNICAL, F.EXIT, "exit_143"))
        c, f, d = run(ev, p=proc(None, 15))
        self.assertEqual((c, f, d), (C.TECHNICAL, F.EXIT, "exit_signal_15"))
        self.assertEqual(run(ev, p=proc(1))[:2], (C.TECHNICAL, F.EXIT))

    def test_row4_signal_even_with_result(self):
        self.assertEqual(run(good_witness_events(), p=proc(None, 9))[:2], (C.TECHNICAL, F.EXIT))

    def test_row4_nonzero_exit_with_result_is_judged_by_the_result(self):
        # CC may exit 1 on an error_max_turns result; that is data (row 17)
        self.assertEqual(run(with_result(subtype="error_max_turns", is_error=True), p=proc(1))[:2],
                         (C.MAX_TURNS, None))

    def test_row4_truncated_tail_no_result(self):
        self.assertEqual(run(good_witness_events()[:3], tail='{"type":"res')[:2], (C.TECHNICAL, F.NO_RESULT))

    def test_row5_corrupt(self):
        ev = good_witness_events()
        ev.insert(2, '{"broken": ')
        self.assertEqual(run(ev)[:2], (C.TECHNICAL, F.CORRUPT_STREAM))
        self.assertEqual(run(good_witness_events() + [result()])[:2], (C.TECHNICAL, F.CORRUPT_STREAM))
        self.assertEqual(run(good_witness_events() + [init()])[:2], (C.TECHNICAL, F.CORRUPT_STREAM))
        ev = good_witness_events()[1:]
        self.assertEqual(run(ev)[:2], (C.TECHNICAL, F.CORRUPT_STREAM))  # n_init == 0

    def test_row5_truncated_tail_with_result_is_tolerated(self):
        self.assertEqual(run(good_witness_events(), tail='{"type":"ass')[:2], (C.OK, None))

    def test_row6_billing_route(self):
        self.assertEqual(run([init(apiKeySource="none")] + good_witness_events()[1:])[:2], (C.STOP, F.BILLING_ROUTE))
        mu = result()["modelUsage"][HAIKU]
        for k, v in (("costBasis", "subscription"), ("provider", "bedrock")):
            with self.subTest(k=k):
                self.assertEqual(run(with_result(modelUsage={HAIKU: {**mu, k: v}}))[:2], (C.STOP, F.BILLING_ROUTE))

    def test_row7_billing(self):
        for st in (400, 401, 402, 403):
            with self.subTest(st=st):
                self.assertEqual(run(with_result(is_error=True, api_error_status=st))[:2], (C.STOP, F.BILLING))
        r = run(with_result(subtype="error_during_execution", is_error=True, result="Credit balance is too low"))
        self.assertEqual(r[:2], (C.STOP, F.BILLING))
        ev = good_witness_events()
        ev[-1] = result(is_error=True, subtype="error_during_execution", result="x")
        ev[-2] = text("API Error: Your quota has been exceeded, insufficient funds")
        self.assertEqual(run(ev)[:2], (C.STOP, F.BILLING))

    def test_row7_model_text_about_payments_is_not_billing(self):
        ev = good_witness_events()
        ev[-2] = text("Approved the payment; billing looks fine, no insufficient quota issues.")
        ev[-1] = result(result="Approved the payment; billing looks fine, insufficient quota")
        self.assertEqual(run(ev)[:2], (C.OK, None))

    def test_clips_are_data_whatever_the_text_says(self):
        for sub, cls in (("error_max_turns", C.MAX_TURNS), ("error_max_budget_usd", C.MAX_BUDGET)):
            ev = good_witness_events()
            ev[-2] = text("API Error: credit balance insufficient, quota, billing, overloaded")
            ev[-1] = result(subtype=sub, is_error=True, result="API Error: Overloaded; billing quota payment")
            with self.subTest(sub=sub):
                self.assertEqual(run(ev)[:2], (cls, None))

    def test_model_prose_in_errored_result_is_not_scanned(self):
        ev = good_witness_events()
        ev[-2] = text("I approved the payment, the billing queue is overloaded, quota fine, insufficient detail")
        ev[-1] = result(is_error=True, subtype="error_during_execution", result="Execution error")
        self.assertEqual(run(ev)[:2], (C.TECHNICAL, F.API_OTHER))

    def test_row8_transient(self):
        for st in (429, 500, 502, 503, 504, 529):
            with self.subTest(st=st):
                self.assertEqual(run(with_result(is_error=True, api_error_status=st))[:2], (C.TECHNICAL, F.API_TRANSIENT))
        self.assertEqual(run(with_result(is_error=True, subtype="error_during_execution",
                                         result="API Error: Overloaded"))[:2], (C.TECHNICAL, F.API_TRANSIENT))
        ev = good_witness_events()
        ev.insert(1, {"type": "system", "subtype": "api_retry", "attempt": 1})
        ev[-1] = result(subtype="error_during_execution", is_error=True, result="boom")
        self.assertEqual(run(ev)[:2], (C.TECHNICAL, F.API_TRANSIENT))

    def test_row9_init(self):
        for over in ({"claude_code_version": "2.1.318"}, {"model": "claude-sonnet-5-5"}, {"permissionMode": "dontAsk"},
                     {"cwd": "/x"}, {"tools": []}, {"skills": ["s"]}, {"mcp_servers": []}):
            with self.subTest(over=over):
                ev = good_witness_events()
                ev[0] = init(**over)
                self.assertEqual(run(ev)[:2], (C.TECHNICAL, F.INIT_MISMATCH))

    def test_row10_model(self):
        self.assertEqual(run(with_result(model="claude-x"))[:2], (C.TECHNICAL, F.MODEL_MISMATCH))
        mu = result()["modelUsage"][HAIKU]
        self.assertEqual(run(with_result(modelUsage={HAIKU: {**mu, "canonicalModel": "z"}}))[:2],
                         (C.TECHNICAL, F.MODEL_MISMATCH))

    def test_row11_permission(self):
        r = run(with_result(permission_denials=[{"tool_name": "mcp__workspace__notify_ops"}]))
        self.assertEqual(r[:2], (C.TECHNICAL, F.PERMISSION_DENIED))

    def test_row12_reply_mismatch(self):
        ev = good_witness_events()
        ev[2] = tool_result(1, '{"result":"Reviews recorded."}')
        self.assertEqual(run(ev)[:2], (C.STOP, F.REPLY_MISMATCH))

    def test_row13_mcp_transport(self):
        for msg in ("MCP error -32000: Connection closed", "server not connected", "Tool call timed out",
                    "the server crashed"):
            with self.subTest(msg=msg):
                ev = good_witness_events()
                ev[2] = tool_result(1, msg, True)
                self.assertEqual(run(ev)[:2], (C.TECHNICAL, F.MCP_TRANSPORT))

    def test_row13_validation_error_is_data_B6(self):
        ev = good_witness_events()
        ev[2] = tool_result(1, "InputValidationError: reviews must be an array", True)
        self.assertEqual(run(ev)[:2], (C.OK, None))

    def test_row14_reconcile(self):
        r = run(good_witness_events(), reconcile_v=["reconcile.missing_on_server: x"])
        self.assertEqual(r[:2], (C.TECHNICAL, F.RECONCILE))

    def test_row15_unknown_subtype(self):
        self.assertEqual(run(with_result(subtype="error_mystery"))[:2], (C.STOP, F.UNKNOWN_SUBTYPE))
        self.assertEqual(run(with_result(subtype="error_mystery", is_error=True))[:2], (C.STOP, F.UNKNOWN_SUBTYPE))

    def test_row16_error_during_execution(self):
        r = run(with_result(subtype="error_during_execution", is_error=True, result="Execution error"))
        self.assertEqual(r[:2], (C.TECHNICAL, F.API_OTHER))

    def test_row16_other_result_violations(self):
        self.assertEqual(run(with_result(is_error=True, result="weird"))[:2], (C.TECHNICAL, F.API_OTHER))
        self.assertEqual(run(with_result(api_error_status=404, is_error=True))[:2], (C.TECHNICAL, F.API_OTHER))

    def test_row17_max_turns_is_data(self):
        self.assertEqual(run(with_result(subtype="error_max_turns", is_error=True))[:2], (C.MAX_TURNS, None))

    def test_row18_max_budget_is_data(self):
        self.assertEqual(run(with_result(subtype="error_max_budget_usd", is_error=True))[:2], (C.MAX_BUDGET, None))

    def test_row19_refusal(self):
        self.assertEqual(run(with_result(stop_reason="refusal"))[:2], (C.REFUSAL, None))
        self.assertEqual(run(with_result(safety_stops=2))[:2], (C.REFUSAL, None))
        ev = good_witness_events()
        ev[-2] = text("no", stop="refusal")
        self.assertEqual(run(ev)[:2], (C.REFUSAL, None))

    def test_row20_ok_variants(self):
        self.assertEqual(run(good_witness_events())[:2], (C.OK, None))
        self.assertEqual(run([init(), text("nothing to do"), result()])[:2], (C.OK, None))      # no tool call
        self.assertEqual(run([init(), result(result="")])[:2], (C.OK, None))                    # empty text
        c, f, d = run([init(), text("I can't help with that."), result(result="I can't help with that.")])
        self.assertEqual((c, f, d), (C.OK, None, "refusal_text"))
        ev = good_witness_events()
        ev[2] = tool_result(1, "schema error", True)
        self.assertEqual(run(ev)[:2], (C.OK, None))                                             # client-side reject

    def test_debrief_ok(self):
        ev = [init(tools=[], servers=[]), text("answer"), result(result="answer")]
        self.assertEqual(run(ev, turn_type=TurnType.D1)[:2], (C.OK, None))
        self.assertEqual(run([init()] + ev[1:], turn_type=TurnType.D2)[:2], (C.TECHNICAL, F.INIT_MISMATCH))


class NonZeroExitWithResultTest(unittest.TestCase):
    """Real CC may exit 1 on error results; the result decides, the exit code goes in the detail."""

    def test_exit1_cases(self):
        cases = [
            (with_result(subtype="error_max_turns", is_error=True), (C.MAX_TURNS, None)),
            (with_result(subtype="error_max_budget_usd", is_error=True), (C.MAX_BUDGET, None)),
            (with_result(is_error=True, api_error_status=429), (C.TECHNICAL, F.API_TRANSIENT)),
            (with_result(is_error=True, api_error_status=402), (C.STOP, F.BILLING)),
            (good_witness_events(), (C.OK, None)),
        ]
        for ev, want in cases:
            with self.subTest(want=want):
                c, f, d = run(ev, p=proc(1))
                self.assertEqual((c, f), want)
                self.assertIn("(exit 1)", d)

    def test_zero_exit_detail_unchanged(self):
        self.assertNotIn("exit", run(with_result(subtype="error_max_turns", is_error=True))[2])


class PrecedenceTest(unittest.TestCase):
    def test_cancelled_beats_everything(self):
        self.assertEqual(run(good_witness_events()[:-1], p=proc(None, 9, True, "sigkill"), fault=True,
                             cancelled=True)[1], F.CANCELLED)

    def test_timeout_beats_fault_and_no_result(self):
        self.assertEqual(run(good_witness_events()[:2], p=proc(None, 2, True, "sigint"), fault=True)[1], F.TIMEOUT)

    def test_fault_beats_success(self):
        self.assertEqual(run(good_witness_events(), fault=True)[:2], (C.TECHNICAL, F.SERVER_FAULT))

    def test_fault_beats_billing(self):
        self.assertEqual(run(with_result(is_error=True, api_error_status=402), fault=True)[1], F.SERVER_FAULT)

    def test_no_result_beats_init_mismatch(self):
        ev = good_witness_events()[:-1]
        ev[0] = init(apiKeySource="none")
        self.assertEqual(run(ev)[1], F.NO_RESULT)

    def test_corrupt_beats_billing(self):
        ev = with_result(is_error=True, api_error_status=402)
        ev = [init(apiKeySource="none")] + ev[1:] + [result()]
        self.assertEqual(run(ev)[1], F.CORRUPT_STREAM)

    def test_billing_route_beats_billing(self):
        ev = good_witness_events()
        ev[0] = init(apiKeySource="none")
        ev[-1] = result(is_error=True, api_error_status=402)
        self.assertEqual(run(ev)[1], F.BILLING_ROUTE)

    def test_billing_beats_transient(self):
        ev = good_witness_events()
        ev[-1] = result(is_error=True, api_error_status=429, result="Credit balance is too low")
        self.assertEqual(run(ev)[1], F.BILLING)
        ev[-1] = result(is_error=True, api_error_status=529, result="overloaded; also check your billing")
        self.assertEqual(run(ev)[1], F.BILLING)

    def test_transient_beats_init_mismatch(self):
        ev = good_witness_events()
        ev[0] = init(cwd="/x")
        ev[-1] = result(is_error=True, api_error_status=529)
        self.assertEqual(run(ev)[1], F.API_TRANSIENT)

    def test_init_beats_model_beats_permission_beats_reply(self):
        ev = good_witness_events()
        ev[0] = init(cwd="/x")
        ev[2] = tool_result(1, "wrong")
        ev[-1] = result(model="claude-x", permission_denials=[{"a": 1}])
        self.assertEqual(run(ev)[1], F.INIT_MISMATCH)
        ev[0] = init()
        self.assertEqual(run(ev)[1], F.MODEL_MISMATCH)
        ev[-1] = result(permission_denials=[{"a": 1}])
        self.assertEqual(run(ev)[1], F.PERMISSION_DENIED)
        ev[-1] = result()
        self.assertEqual(run(ev)[1], F.REPLY_MISMATCH)

    def test_reply_mismatch_beats_transport_and_reconcile(self):
        ev = good_witness_events()
        ev[2] = tool_result(1, "wrong")
        ev += []
        ev2 = [init(), tool_use(1, "submit_reviews", {}), tool_result(1, "wrong"), tool_use(2, "notify_ops", {}),
               tool_result(2, "MCP error -32000: Connection closed", True), result()]
        self.assertEqual(run(ev2, reconcile_v=["reconcile.x"])[1], F.REPLY_MISMATCH)

    def test_transport_beats_reconcile_beats_unknown_subtype(self):
        ev = [init(), tool_use(1, "notify_ops", {}), tool_result(1, "Connection closed", True), result()]
        self.assertEqual(run(ev, reconcile_v=["reconcile.x"])[1], F.MCP_TRANSPORT)
        self.assertEqual(run(with_result(subtype="error_mystery"), reconcile_v=["reconcile.x"])[1], F.RECONCILE)

    def test_unknown_subtype_beats_max_turns_family(self):
        self.assertEqual(run(with_result(subtype="error_max_turn"))[1], F.UNKNOWN_SUBTYPE)

    def test_technical_beats_data_clips(self):
        r = run(with_result(subtype="error_max_turns", is_error=True, permission_denials=[{"a": 1}]))
        self.assertEqual(r[:2], (C.TECHNICAL, F.PERMISSION_DENIED))

    def test_max_turns_beats_refusal(self):
        self.assertEqual(run(with_result(subtype="error_max_turns", is_error=True, stop_reason="refusal"))[0],
                         C.MAX_TURNS)


class TableTest(unittest.TestCase):
    """The implemented table, class -> TurnClass/FailClass; kept as one place the lead can read."""

    def test_data_classes_have_no_fail(self):
        for ev in (good_witness_events(), with_result(subtype="error_max_turns", is_error=True),
                   with_result(subtype="error_max_budget_usd", is_error=True), with_result(stop_reason="refusal")):
            c, f, _ = run(ev)
            self.assertIn(c, (C.OK, C.MAX_TURNS, C.MAX_BUDGET, C.REFUSAL))
            self.assertIsNone(f)

    def test_stop_set(self):
        stops = {F.BILLING_ROUTE, F.BILLING, F.REPLY_MISMATCH, F.UNKNOWN_SUBTYPE}
        seen = set()
        for ev in ([init(apiKeySource="x")] + good_witness_events()[1:], with_result(is_error=True, api_error_status=402),
                   with_result(subtype="zzz")):
            c, f, _ = run(ev)
            self.assertEqual(c, C.STOP)
            seen.add(f)
        ev = good_witness_events()
        ev[2] = tool_result(1, "bad")
        seen.add(run(ev)[1])
        self.assertEqual(seen, stops)


if __name__ == "__main__":
    unittest.main()
