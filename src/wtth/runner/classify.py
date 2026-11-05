"""Turn classification (research/41 section 2.6 table; research/43 B1, B6). Owner: WP3.

classify() evaluates the rows top to bottom; the first match wins. It is pure: all inputs come in as arguments.

Violation strings come from wtth.runner.validate and are keyed on their prefix:
  init.apiKeySource, result.provider, result.costBasis   -> billing_route (row 6)
  result.permission_denials                             -> permission_denied (row 11)
  result.modelUsage.*                                   -> model_mismatch (row 10)
  result.subtype                                        -> unknown_subtype (row 15)
  other init.*                                          -> init_mismatch (row 9)
  other result.* (except .count/.missing)               -> api_other (before row 17)
  `.count` / `.missing` are covered by rows 4 and 5 from the record itself.
"""
from __future__ import annotations

import re

from wtth.types import FailClass, ProcResult, RefusalFlags, StreamRecord, TurnClass

BILLING_ROUTE_KEYS = ("init.apiKeySource", "result.provider", "result.costBasis")
BILLING_STATUSES = frozenset({400, 401, 402, 403})
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
BILLING_TEXT = re.compile(r"(?i)credit balance|billing|insufficient|quota|payment")
TRANSIENT_TEXT = re.compile(r"(?i)overloaded")
TRANSPORT_TEXT = re.compile(
    r"(?i)mcp error|connection closed|not connected|disconnected|server.*(exit|crash)|timed out")
KNOWN_SUBTYPES = ("success", "error_max_turns", "error_max_budget_usd", "error_during_execution")
TOOL_PREFIX = "mcp__workspace__"


def _has(violations: list[str], *prefixes: str) -> list[str]:
    return [v for v in violations if any(v.startswith(p) for p in prefixes)]


def _api_status(rec: StreamRecord) -> int | None:
    s = (rec.result or {}).get("api_error_status")
    return s if isinstance(s, int) and not isinstance(s, bool) else None


def _error_texts(rec: StreamRecord) -> list[str]:
    """Texts that are API/error messages rather than model output: only inspected when the result itself is an
    error (is_error true or an api_error_status). A successful turn's text is model output and may legitimately
    mention payments or billing, so it is never scanned for the billing words."""
    res = rec.result or {}
    if res.get("is_error") is True or _api_status(rec) is not None:
        texts = [res.get("result")] + list(rec.assistant_texts)
        return [t for t in texts if isinstance(t, str)]
    return []


def classify(proc: ProcResult, rec: StreamRecord, *, init_v: list[str], result_v: list[str], reply_v: list[str],
             reconcile_v: list[str], server_fault: bool, cancelled: bool,
             refusal: RefusalFlags) -> tuple[TurnClass, FailClass | None, str]:
    T, S = TurnClass.TECHNICAL, TurnClass.STOP
    res = rec.result or {}
    sub = res.get("subtype")
    status = _api_status(rec)

    # 1 cancelled because a sibling failed
    if cancelled:
        return T, FailClass.CANCELLED, "cancelled: a sibling turn failed"
    # 2 timed out
    if proc.timed_out:
        return T, FailClass.TIMEOUT, f"timed out; kill_stage={proc.kill_stage}"
    # 3 fault file exists for the turn
    if server_fault:
        return T, FailClass.SERVER_FAULT, "server fault file exists for the turn"
    # 4 no result / non-zero exit with no result / killed by a signal
    if rec.n_result == 0:
        if proc.term_signal is not None:
            return T, FailClass.EXIT, f"exit_signal_{proc.term_signal}"
        if proc.exit_code not in (0, None):
            return T, FailClass.EXIT, f"exit_{proc.exit_code}"
        return T, FailClass.NO_RESULT, "no result event"
    if proc.term_signal is not None:
        return T, FailClass.EXIT, f"exit_signal_{proc.term_signal}"
    # 5 corrupt stream
    if rec.n_bad_json > 0 or rec.n_result > 1 or rec.n_init != 1:
        return T, FailClass.CORRUPT_STREAM, (
            f"n_bad_json={rec.n_bad_json} n_result={rec.n_result} n_init={rec.n_init}")
    # 6 billing route
    route = _has(init_v, "init.apiKeySource") + _has(result_v, "result.provider", "result.costBasis")
    if route:
        return S, FailClass.BILLING_ROUTE, route[0]
    # 7 billing
    if status in BILLING_STATUSES:
        return S, FailClass.BILLING, f"api_error_status={status}"
    for t in _error_texts(rec):
        if BILLING_TEXT.search(t):
            return S, FailClass.BILLING, "billing words in an error message"
    # 8 transient API trouble
    if status in TRANSIENT_STATUSES:
        return T, FailClass.API_TRANSIENT, f"api_error_status={status}"
    if any(TRANSIENT_TEXT.search(t) for t in _error_texts(rec)):
        return T, FailClass.API_TRANSIENT, "overloaded"
    if sub == "error_during_execution" and rec.api_retries:
        return T, FailClass.API_TRANSIENT, f"error_during_execution after {len(rec.api_retries)} api_retry events"
    # 9 init violations
    init_rest = [v for v in init_v if not v.startswith(("init.apiKeySource", "init.count"))]
    if init_rest:
        return T, FailClass.INIT_MISMATCH, init_rest[0]
    # 10 model usage
    mu = _has(result_v, "result.modelUsage")
    if mu:
        return T, FailClass.MODEL_MISMATCH, mu[0]
    # 11 permission denials
    pd = _has(result_v, "result.permission_denials")
    if pd:
        return T, FailClass.PERMISSION_DENIED, pd[0]
    # 12 reply bytes
    if reply_v:
        return S, FailClass.REPLY_MISMATCH, reply_v[0]
    # 13 transport errors on our tools (a validation error is data, B6; only transport-looking text is technical)
    for tu in rec.tool_uses:
        if tu.name.startswith(TOOL_PREFIX) and tu.result_is_error and TRANSPORT_TEXT.search(tu.result_text or ""):
            return T, FailClass.MCP_TRANSPORT, f"{tu.name}: {(tu.result_text or '')[:120]}"
    # 14 reconcile
    if reconcile_v:
        return T, FailClass.RECONCILE, reconcile_v[0]
    # 15 unknown subtype
    if sub not in KNOWN_SUBTYPES:
        return S, FailClass.UNKNOWN_SUBTYPE, f"subtype={sub!r}"
    # 16 error_during_execution (not caught above); also any other result-level violation
    if sub == "error_during_execution":
        return T, FailClass.API_OTHER, "error_during_execution"
    rest = [v for v in result_v if not v.startswith(("result.count", "result.missing", "result.subtype"))]
    if rest:
        return T, FailClass.API_OTHER, rest[0]
    # 17 / 18 clips are data (B6)
    if sub == "error_max_turns":
        return TurnClass.MAX_TURNS, None, "error_max_turns"
    if sub == "error_max_budget_usd":
        return TurnClass.MAX_BUDGET, None, "error_max_budget_usd"
    # 19 refusal
    if refusal.refusal_stop:
        return TurnClass.REFUSAL, None, "refusal stop"
    # 20 everything else is data
    return TurnClass.OK, None, "refusal_text" if refusal.refusal_text else ""
