"""One CLI turn end to end (research/41 sections 2.5-2.7, 2.9; research/43 B1, B6, B10). Owner: WP3.

Order inside run_turn:
  1. take a K slot (ctx.kgate) and the launch gap (ctx.launch); build argv/env/mcp-config from the request;
  2. INSERT the turns row and the exposures row (they exist before the process does);
  3. run the process group (files for stdout/stderr, kill ladder, cancel event);
  4. harvest the CC MCP log, parse the stream, validate, scan logs, reconcile against the server's tool_calls,
     read the fault file, classify;
  5. ONE write: UPDATE turns + exposures, INSERT stream_tool_uses, cost_ledger, fallback_rows, debrief_answers.

run_turn does not call ctx.cost.charge (it inserts the cost_ledger row itself, as research/41 2.9 says) and never
touches the slot pool: the caller acquires and releases the SlotDirs.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from wtth.runner import classify as classify_mod
from wtth.runner import invocation, proc, validate
from wtth.runner.stream import parse_stream
from wtth.textnorm import canon_json
from wtth.types import (FailClass, ProcResult, RefusalFlags, SlotDirs, StreamRecord, TurnClass, TurnOutcome,
                        TurnRequest, TurnType)

if TYPE_CHECKING:
    from wtth.orchestrator.context import RunContext

_DEBRIEF_Q = {TurnType.D1: "D1", TurnType.D2: "D2"}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _placeholder_proc(req: TurnRequest) -> ProcResult:
    d = Path(req.turn_dir)
    return ProcResult(exit_code=None, term_signal=None, timed_out=False, kill_stage=None, wall_ms=0,
                      stdout_path=d / "stream.jsonl", stderr_path=d / "stderr.txt", pgid=0, peak_rss_kb=None)


def _insert_launch(ctx: "RunContext", req: TurnRequest, slot: SlotDirs, argv_redacted: list[str],
                   env_names: list[str], launched_at: float) -> int:
    """Insert turns + exposures rows. Returns try_no (B1: debrief calls are retried in place)."""
    ts = req.toolset

    def fn(conn: sqlite3.Connection) -> int:
        try_no = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE episode_id=? AND attempt=? AND turn_type=? "
            "AND COALESCE(round,0)=COALESCE(?,0) AND COALESCE(slot,0)=COALESCE(?,0)",
            (req.episode_id, req.attempt, req.turn_type.value, req.round, req.slot)).fetchone()[0] + 1
        conn.execute(
            "INSERT INTO turns(turn_id, run_id, episode_id, attempt, turn_type, round, slot, handle, model, "
            "slot_index, turn_dir, argv_redacted_json, env_names_json, audit_bodies, launched_at, try_no, "
            "max_turns_cap, max_budget_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (req.turn_id, ctx.run_id, req.episode_id, req.attempt, req.turn_type.value, req.round, req.slot,
             req.handle, req.model, slot.index, str(req.turn_dir), json.dumps(argv_redacted),
             json.dumps(env_names), 1 if req.audit_bodies else 0, launched_at, try_no, req.max_turns,
             req.max_budget_usd))
        conn.execute(
            "INSERT INTO exposures(turn_id, system_path, system_sha, tools_sha, prompt_sha, prompt_text, "
            "visible_post_ids_json, visible_note_ids_json, visible_log_ids_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (req.turn_id, str(req.system_path), req.system_sha, ts.sha256 if ts is not None else "none",
             req.prompt.sha256, req.prompt.text, json.dumps(list(req.prompt.visible_post_ids)),
             json.dumps(list(req.prompt.visible_note_ids)), json.dumps(list(req.prompt.visible_log_ids))))
        return try_no

    return ctx.db.write(fn)


def answer_text(final_text: str, assistant_texts: Sequence[str]) -> str:
    """The one definition of a debrief answer: the result text, else the last assistant text, else ''.
    debrief_answers stores this; the D2 prompt must be rendered from the stored row."""
    return final_text or (assistant_texts[-1] if assistant_texts else "")


async def _launch_gate(ctx: "RunContext", cancel: asyncio.Event) -> bool:
    """Wait for the launch gap, but give up at once if `cancel` is set. True = go."""
    if cancel.is_set():
        return False
    lw = asyncio.ensure_future(ctx.launch.wait())
    cw = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({lw, cw}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (lw, cw):
            if not t.done():
                t.cancel()
    return lw.done() and not lw.cancelled() and not cancel.is_set()


async def run_turn(ctx: "RunContext", req: TurnRequest, slot: SlotDirs, *, cancel: asyncio.Event) -> TurnOutcome:
    settings = ctx.settings
    turn_dir = Path(req.turn_dir)
    turn_dir.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = turn_dir / "stream.jsonl", turn_dir / "stderr.txt"

    bodies_dir: Path | None = None
    if req.audit_bodies:
        bodies_dir = turn_dir / "bodies"
        bodies_dir.mkdir(parents=True, exist_ok=True)

    mcp_json = None
    if req.turn_type not in (TurnType.D1, TurnType.D2):
        if req.identity is None or req.toolset is None:
            raise ValueError("witness/probe turn needs identity and toolset")
        mcp_json = invocation.build_mcp_config(settings, req.identity, req.toolset)
    argv = invocation.build_argv(settings, req, mcp_json)
    env = invocation.build_env(settings, slot, ctx._api_key, bodies_dir)
    argv_redacted = invocation.redact_argv(argv)
    env_names = sorted(env)

    launch_wall = time.time()
    inserted = False
    finalized = False
    try:
        async with ctx.kgate:
            go = await _launch_gate(ctx, cancel)
            launch_wall = time.time()
            try_no = _insert_launch(ctx, req, slot, argv_redacted, env_names, launch_wall)
            inserted = True
            stdout_path.write_bytes(b"")
            stderr_path.write_bytes(b"")
            force: tuple[TurnClass, FailClass, str] | None = None
            never_launched = False
            if not go:
                pres, never_launched = _placeholder_proc(req), True
                was_cancelled = True
            else:
                try:
                    pres = await proc.run_process(
                        argv, env, settings.paths.cwd, stdout_path, stderr_path, req.timeout_s,
                        settings.timeouts.grace_int_s, settings.timeouts.grace_term_s, rss_sampler=True,
                        cancel=cancel)
                    was_cancelled = (cancel.is_set() and pres.kill_stage is not None and not pres.timed_out
                                     and not str(pres.kill_stage).startswith("straggler_"))
                except OSError as exc:
                    pres, never_launched, was_cancelled = _placeholder_proc(req), True, False
                    force = (TurnClass.TECHNICAL, FailClass.EXIT, f"spawn_oserror:{exc.errno}")
        out = _finish(ctx, req, slot, pres, launch_wall, try_no, was_cancelled, force=force,
                      never_launched=never_launched)
        finalized = True
        return out
    except BaseException as exc:
        if inserted and not finalized:
            # a launched turn is always finalized: class technical, cost from the stream or the per-call cap
            fail = FailClass.CANCELLED if isinstance(exc, asyncio.CancelledError) else FailClass.STOPPED
            try:
                _finish(ctx, req, slot, _placeholder_proc(req), launch_wall, 0, False,
                        force=(TurnClass.TECHNICAL, fail, f"{type(exc).__name__} during turn"), never_launched=False)
            except Exception:  # noqa: BLE001 - the original exception matters more
                pass
        raise


def _finish(ctx: "RunContext", req: TurnRequest, slot: SlotDirs, pres: ProcResult, launch_wall: float,
            try_no: int, was_cancelled: bool, *, force: tuple[TurnClass, FailClass, str] | None = None,
            never_launched: bool = False) -> TurnOutcome:
    settings = ctx.settings
    turn_dir = Path(req.turn_dir)

    mcp_logs = proc.harvest_mcp_log(slot, str(settings.paths.cwd), turn_dir / "mcp", launch_wall,
                                    getattr(settings, "mcp_server_name", "workspace"))  # WP4-ADD
    rec = parse_stream(pres.stdout_path)
    res = rec.result or {}

    init_v = validate.validate_init(rec, ctx.expected_init, req)
    result_v = validate.validate_result(rec, ctx.expected_init, req)
    if req.turn_type == TurnType.PROBE:
        # the deny probe exists to provoke a permission denial; it is not a violation there
        result_v = [v for v in result_v if not v.startswith("result.permission_denials")]
    reply_v = validate.check_replies(rec, req.toolset)
    refusal = validate.detect_refusal(rec, None)
    stderr_unexp, mcp_unexp, connect_ms = validate.scan_logs(pres.stderr_path, mcp_logs, ctx.allow)
    mcp_calls = validate.count_mcp_calls(mcp_logs, ctx.allow)

    server_rows = ctx.db.read(lambda c: c.execute(
        "SELECT id, tool, args_json FROM tool_calls WHERE turn_id=? ORDER BY id", (req.turn_id,)).fetchall())
    reconcile_v = validate.reconcile(rec, server_rows, mcp_calls) if rec.n_result == 1 else []
    flags = validate.match_rows(rec, server_rows)

    fault_path = Path(settings.paths.faults_dir) / f"{req.turn_id}.jsonl"
    server_fault = fault_path.exists()
    fault_lines: list[str] = []
    if server_fault:
        fault_lines = [ln for ln in fault_path.read_text(encoding="utf-8", errors="replace").splitlines()
                       if ln.strip()]

    if force is not None:
        cls, fail, detail = force
    else:
        cls, fail, detail = classify_mod.classify(
            pres, rec, init_v=init_v, result_v=result_v, reply_v=reply_v, reconcile_v=reconcile_v,
            server_fault=server_fault, cancelled=was_cancelled, refusal=refusal)
    if force is None and req.turn_type == TurnType.PROBE and cls in (TurnClass.OK, TurnClass.REFUSAL, TurnClass.MAX_TURNS,
                                                   TurnClass.MAX_BUDGET):
        cls = TurnClass.PROBE

    cost = _num(res.get("total_cost_usd"))
    if cost is not None:
        cost_usd, estimated = cost, False
    elif never_launched:
        cost_usd, estimated = 0.0, False          # never launched
    else:
        cost_usd, estimated = float(req.max_budget_usd), True   # no result: charge the cap (conservative)

    final_text = res.get("result") if isinstance(res.get("result"), str) else ""
    violations = tuple(init_v + result_v + reply_v + reconcile_v)
    outcome = TurnOutcome(
        turn_id=req.turn_id, cls=cls, fail=fail, detail=detail, violations=violations, record=rec, proc=pres,
        cost_usd=cost_usd, cost_estimated=estimated, final_text=final_text, refusal=refusal)

    _persist(ctx, req, outcome, mcp_unexp=mcp_unexp, stderr_unexp=stderr_unexp, connect_ms=connect_ms,
             server_fault=server_fault, fault_lines=fault_lines, flags=flags, init=rec.init)
    return outcome


def _persist(ctx: "RunContext", req: TurnRequest, out: TurnOutcome, *, mcp_unexp: list[str],
             stderr_unexp: list[str], connect_ms: int | None, server_fault: bool, fault_lines: list[str],
             flags: list[tuple[bool | None, int | None]], init: dict | None) -> None:
    rec, pres = out.record, out.proc
    res = rec.result or {}
    usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    mu = res.get("modelUsage") if isinstance(res.get("modelUsage"), dict) else {}
    mu_one = mu.get(req.model) if isinstance(mu.get(req.model), dict) else {}
    err_uses = [tu for tu in rec.tool_uses if tu.result_is_error]
    ts = time.time()

    cols: dict[str, Any] = {
        "ended_at": ts, "wall_ms": pres.wall_ms, "exit_code": pres.exit_code, "term_signal": pres.term_signal,
        "timed_out": 1 if pres.timed_out else 0, "kill_stage": pres.kill_stage, "peak_rss_kb": pres.peak_rss_kb,
        "n_lines": rec.n_lines, "n_nonjson": rec.n_nonjson, "n_bad_json": rec.n_bad_json,
        "truncated_tail": 1 if rec.truncated_tail else 0, "n_init": rec.n_init, "n_result": rec.n_result,
        "subtype": res.get("subtype"), "is_error": None if res.get("is_error") is None else int(bool(res["is_error"])),
        "stop_reason": res.get("stop_reason"), "terminal_reason": res.get("terminal_reason"),
        "api_error_status": _int(res.get("api_error_status")), "num_turns": _int(res.get("num_turns")),
        "duration_ms": _int(res.get("duration_ms")), "duration_api_ms": _int(res.get("duration_api_ms")),
        "total_cost_usd": _num(res.get("total_cost_usd")),
        "input_tokens": _int(usage.get("input_tokens")),
        "cache_creation_tokens": _int(usage.get("cache_creation_input_tokens")),
        "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")), "thinking_tokens": _int(details.get("thinking_tokens")),
        "model_usage_json": canon_json(mu) if res else None, "cost_basis": mu_one.get("costBasis"),
        "session_id": rec.session_id, "request_ids_json": json.dumps(rec.request_ids),
        "api_retries": len(rec.api_retries),
        "permission_denials_json": json.dumps(res.get("permission_denials")) if res else None,
        "safety_stops": _int(res.get("safety_stops")), "n_tool_use": len(rec.tool_uses),
        "n_tool_error": len(err_uses),
        "tool_error_texts_json": json.dumps([tu.result_text for tu in err_uses]),
        "no_tool_call": 1 if not rec.tool_uses else 0,
        "refusal_stop": 1 if out.refusal.refusal_stop else 0, "refusal_text": 1 if out.refusal.refusal_text else 0,
        "final_text": out.final_text, "assistant_texts_json": json.dumps(rec.assistant_texts),
        "violations_json": json.dumps(list(out.violations)), "stderr_unexpected_json": json.dumps(stderr_unexp),
        "mcp_log_unexpected_json": json.dumps(mcp_unexp), "mcp_connect_ms": connect_ms,
        "server_fault": 1 if server_fault else 0, "class": out.cls.value,
        "fail_class": out.fail.value if out.fail is not None else None,
        "fail_detail": out.detail or None,
        "api_key_source": init.get("apiKeySource") if isinstance(init, dict) else None,
        "canonical_model": mu_one.get("canonicalModel"),
    }
    phase_q = "SELECT phase FROM episodes WHERE episode_id=?"
    answer = None
    if (req.turn_type in _DEBRIEF_Q and out.cls not in (TurnClass.TECHNICAL, TurnClass.STOP)
            and req.slot is not None and req.handle is not None):
        answer = answer_text(out.final_text, rec.assistant_texts)

    def fn(conn: sqlite3.Connection) -> None:
        names = list(cols)
        conn.execute(f"UPDATE turns SET {', '.join(n + '=?' for n in names)} WHERE turn_id=?",
                     [cols[n] for n in names] + [req.turn_id])
        init_uuid = init.get("uuid") if isinstance(init, dict) and isinstance(init.get("uuid"), str) else None
        conn.execute("UPDATE exposures SET request_ids_json=?, session_id=?, init_uuid=? WHERE turn_id=?",
                     (json.dumps(rec.request_ids), rec.session_id, init_uuid, req.turn_id))
        has_ref = any(r[1] == "db_row_ref" for r in conn.execute("PRAGMA table_info(stream_tool_uses)"))
        for tu, (fl, row_id) in zip(rec.tool_uses, flags):
            kind = None
            if tu.result_is_error:
                kind = "server" if classify_mod.TRANSPORT_TEXT.search(tu.result_text or "") else "validation"
            vals = [req.turn_id, tu.seq, tu.tool_use_id, tu.name, canon_json(tu.input), 1 if tu.has_result else 0,
                    tu.result_text, None if tu.result_is_error is None else int(tu.result_is_error), kind,
                    None if fl is None else int(fl)]
            cols_sql = ("turn_id, seq, tool_use_id, name, input_json, has_result, result_text, result_is_error, "
                        "error_kind, reconciled")
            if has_ref:
                cols_sql += ", db_row_ref"
                vals.append(row_id)
            conn.execute(f"INSERT INTO stream_tool_uses({cols_sql}) VALUES ({','.join('?' * len(vals))})", vals)
        phase = conn.execute(phase_q, (req.episode_id,)).fetchone()
        conn.execute("INSERT INTO cost_ledger(turn_id, run_id, phase, model, cost_usd, estimated, ts) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (req.turn_id, ctx.run_id, phase[0] if phase else "unknown", req.model, out.cost_usd,
                      1 if out.cost_estimated else 0, ts))
        for line in fault_lines:
            conn.execute("INSERT INTO fallback_rows(turn_id, line_json, ingested_at) VALUES (?,?,?)",
                         (req.turn_id, line, ts))
        if answer is not None:
            conn.execute(
                "INSERT INTO debrief_answers(episode_id, attempt, slot, handle, question, answer_text, turn_id, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (req.episode_id, req.attempt, req.slot, req.handle, _DEBRIEF_Q[req.turn_type], answer,
                 req.turn_id, ts))

    ctx.db.write(fn)
