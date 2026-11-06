"""One judge call built from the runner's pieces, plus persistence of the call and ledger rows (plan 47 section 2.5)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wtth.judge import labels as labels_mod
from wtth.judge import prompt as prompt_mod
from wtth.judge.budget import JudgeBudget
from wtth.judge.db import active_version, judge_db_path, open_judge_db
from wtth.judge.types import BudgetBinds, CallOutcome, Item, ItemType, ParsedOutput
from wtth.orchestrator.context import Breaker, KGate, LaunchGate, SlotPool
from wtth.runner import classify as classify_mod
from wtth.runner import invocation, proc, validate
from wtth.runner.stream import parse_stream
from wtth.runner.turn import _launch_gate, answer_text  # LEAD-REVIEW: _launch_gate is private to runner.turn; reused so the launch gap honours cancel exactly as witness turns do
from wtth.textnorm import sha256_text
from wtth.types import (FailClass, ProcResult, RenderedPrompt, TurnClass, TurnRequest, TurnType)

# Same value as tests.util.DUMMY_KEY; composed at runtime so the literal prefix is never in the repo.
FAKE_KEY = "-".join(("sk", "ant", "TESTDUMMY0000"))  # joined at runtime: not constant-folded into the .pyc
MAX_K = 15


@dataclass
class JudgeContext:
    settings: Any
    jdb: Any
    main_db: Any
    run_id: str
    prompt: prompt_mod.JudgePrompt
    system_path: Path
    expected_init: dict
    allow: dict
    slots: SlotPool
    kgate: KGate
    launch: LaunchGate
    breaker: Breaker
    budget: JudgeBudget
    calls_dir: Path
    model: str
    max_budget_usd: float
    _api_key: str = field(default="", repr=False)
    inflight: int = 0
    halt: str | None = None   # 'budget' | 'stop' once launching must end


def make_context(settings: Any, *, run_id: str, prompt_version: str, k: int, command: str = "judge",
                 args: dict | None = None, deviation: str | None = None, manifest_path: str = "") -> JudgeContext:
    """Open (creating) the judge database, write the system file, register the run, build the gates."""
    from wtth import secrets
    from wtth.config import CONFIGS_DIR, env_allowlist, load_expected_init, load_log_allowlists
    from wtth.db import Db
    from wtth.orchestrator import guards

    jdb = open_judge_db(settings, create=True)
    from wtth.judge.db import ensure_meta
    ensure_meta(jdb)
    main_path = settings.paths.db_path
    main_db = Db(main_path) if Path(main_path).exists() else None
    pr = prompt_mod.load_revision(settings, prompt_version)
    spath = prompt_mod.write_system_file(settings, pr)
    budget = JudgeBudget(settings, main_db, jdb)
    model = settings.models.judge
    k = max(1, min(int(k), settings.scheduler.slots, MAX_K))
    launch = LaunchGate(float(settings.scheduler.launch_gap_ms))  # LEAD-REVIEW: plan says 150 ms; the config value is 150 and tests set 0
    breaker = Breaker(settings)
    breaker.hold = launch.hold_for
    slots = SlotPool(settings.paths.slots_root, settings.scheduler.slots)
    slots.ensure()
    exp = load_expected_init(CONFIGS_DIR / "expected_init.json", settings)
    allow = load_log_allowlists(CONFIGS_DIR / "log_allowlists.json")
    allow["env"] = env_allowlist(CONFIGS_DIR / "env_allowlist.json")
    key = FAKE_KEY if settings.fake else secrets.load_api_key(settings.paths.key_file)
    spent = budget.global_spent()
    now = time.time()

    def reg(c: sqlite3.Connection) -> None:
        c.execute("INSERT OR IGNORE INTO prompt_versions(prompt_version, system_path, system_sha256, "
                  "user_template_sha256, rubric_sha256, deviation_ref, created_at) VALUES (?,?,?,?,?,?,?)",
                  (pr.version, str(spath), pr.system_sha256, pr.user_template_sha256, pr.rubric_sha256, deviation,
                   now))
        c.execute("INSERT OR IGNORE INTO judge_runs(run_id, command, args_json, started_at, src_tree_sha, "
                  "manifest_path, model, k, spent_before_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                  (run_id, command, json.dumps(args or {}, sort_keys=True, default=str), now,
                   guards.src_tree_sha(settings),
                   manifest_path or str(Path(settings.paths.manifests_dir) / f"{run_id}.json"), model, k, spent))

    jdb.write(reg)
    return JudgeContext(settings, jdb, main_db, run_id, pr, spath, exp, allow, slots, KGate(k), launch, breaker,
                        budget, Path(settings.paths.runs_dir) / "judge" / "calls", model,
                        float(settings.models.budget_for(model)), _api_key=key)


def close_run(ctx: JudgeContext, exit_code: int) -> None:
    spent = ctx.budget.global_spent()
    ctx.jdb.write(lambda c: c.execute(
        "UPDATE judge_runs SET ended_at=?, exit_code=?, spent_after_usd=? WHERE run_id=? AND ended_at IS NULL",
        (time.time(), exit_code, spent, ctx.run_id)))


def register_batch(ctx: JudgeContext, items: list[Item], *, kind: str, tier: str | None, subtier: str | None,
                   seed: int | None = None) -> str:
    bid = uuid.uuid4().hex

    def fn(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO batches(batch_id, run_id, kind, tier, subtier, prompt_version, seed, n_items, "
                  "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (bid, ctx.run_id, kind, tier, subtier, ctx.prompt.version, seed, len(items), time.time()))
        for pos, it in enumerate(items):
            c.execute("INSERT OR IGNORE INTO batch_items(batch_id, item_id, position) VALUES (?,?,?)",
                      (bid, it.item_id, pos))

    ctx.jdb.write(fn)
    return bid


def build_request(ctx: JudgeContext, item: Item, call_id: str) -> TurnRequest:
    s = ctx.settings
    user = prompt_mod.user_message(ctx.prompt, item_type=item.item_type, item_id=item.item_id,
                                   item_text=item.text, board_lines=item.context_text)
    return TurnRequest(
        turn_id=call_id, episode_id=0, attempt=0, turn_type=TurnType.JUDGE, round=None, slot=None, handle=None,
        model=ctx.model, system_path=ctx.system_path, system_sha=ctx.prompt.system_sha256,
        prompt=RenderedPrompt(user, sha256_text(user), (), (), ()), toolset=None, identity=None,
        max_turns=s.max_turns.debrief, max_budget_usd=ctx.max_budget_usd, timeout_s=s.timeouts.turn_s,
        audit_bodies=False, settings_path=Path(s.paths.cc_settings),
        turn_dir=ctx.calls_dir / call_id[:2] / call_id)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _settled_status(ask_no: int, cls: TurnClass, parsed: ParsedOutput | None) -> str | None:
    if cls == TurnClass.REFUSAL:
        return "refused"
    if cls == TurnClass.OK and parsed is not None and parsed.ok:
        return "valid"
    if ask_no == 2 and cls in (TurnClass.OK, TurnClass.MAX_TURNS, TurnClass.MAX_BUDGET):
        return "invalid"
    return None


async def run_call(ctx: JudgeContext, item: Item, *, batch_id: str, ask_no: int, try_no: int,
                   cancel: asyncio.Event) -> CallOutcome:
    """One claude -p call for one item. Raises BudgetBinds (nothing launched) when the launch guard fails.
    Persists with ONE write at the end: UPDATE calls, INSERT judge_ledger, INSERT judge_labels when settled."""
    s = ctx.settings
    call_id = uuid.uuid4().hex
    req = build_request(ctx, item, call_id)
    turn_dir = Path(req.turn_dir)
    turn_dir.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = turn_dir / "stream.jsonl", turn_dir / "stderr.txt"
    argv = invocation.build_argv(s, req, None)
    argv_redacted = invocation.redact_argv(argv)
    model = ctx.model
    pres: ProcResult | None = None
    never_launched = False
    force: tuple[TurnClass, FailClass, str] | None = None
    slot = None
    reserved = False
    inserted = False
    launch_wall = time.time()
    try:
        async with ctx.kgate:
            if ctx.halt is not None or cancel.is_set():
                raise _NotLaunched()
            ctx.inflight += 1          # reserve the per-call cap BEFORE any await, so K coroutines cannot all pass
            reserved = True
            try:
                if not ctx.budget.can_launch(ctx.inflight - 1):
                    ctx.halt = "budget"
                    raise BudgetBinds("launch guard: global spend plus in-flight calls would pass the cap")
                go = await _launch_gate(ctx, cancel)
                if not go or ctx.halt is not None:
                    raise _NotLaunched()
                slot = ctx.slots.acquire()
            except BaseException:
                ctx.inflight -= 1
                raise
            try:
                env = invocation.build_env(s, slot, ctx._api_key, None)
                launch_wall = time.time()
                row = (call_id, ctx.run_id, batch_id, item.item_id, ctx.prompt.version, ask_no, try_no, model,
                       req.prompt.sha256, str(turn_dir), slot.index, json.dumps(argv_redacted),
                       json.dumps(sorted(env)), launch_wall)
                ctx.jdb.write(lambda c: c.execute(
                    "INSERT INTO calls(call_id, run_id, batch_id, item_id, prompt_version, ask_no, try_no, model, "
                    "user_sha256, turn_dir, slot_index, argv_redacted_json, env_names_json, launched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row))
                inserted = True
                stdout_path.write_bytes(b"")
                stderr_path.write_bytes(b"")
                try:
                    pres = await proc.run_process(
                        argv, env, s.paths.cwd, stdout_path, stderr_path, req.timeout_s, s.timeouts.grace_int_s,
                        s.timeouts.grace_term_s, rss_sampler=False, cancel=cancel)
                except OSError as exc:
                    never_launched = True
                    force = (TurnClass.TECHNICAL, FailClass.EXIT, f"spawn_oserror:{exc.errno}")
            finally:
                ctx.inflight -= 1
                if slot is not None:
                    ctx.slots.release(slot)
    except _NotLaunched:
        raise BudgetBinds("not launched: run is halting") if ctx.halt == "budget" else JudgeHalted()
    except BaseException as exc:
        if inserted:
            try:
                _finish(ctx, req, item, ask_no, try_no, call_id, None, launch_wall, False,
                        force=(TurnClass.TECHNICAL, FailClass.CANCELLED if isinstance(exc, asyncio.CancelledError)
                               else FailClass.STOPPED, f"{type(exc).__name__} during call"), never_launched=False)
            except Exception:  # noqa: BLE001 - the original exception matters more
                pass
        raise
    was_cancelled = bool(pres is not None and cancel.is_set() and pres.kill_stage is not None and not pres.timed_out
                         and not str(pres.kill_stage).startswith("straggler_"))
    return _finish(ctx, req, item, ask_no, try_no, call_id, pres, launch_wall, was_cancelled, force=force,
                   never_launched=never_launched)


class _NotLaunched(Exception):
    pass


class JudgeHalted(Exception):
    """A call was not launched because the run is stopping (cancel or STOP)."""


def _finish(ctx: JudgeContext, req: TurnRequest, item: Item, ask_no: int, try_no: int, call_id: str,
            pres: ProcResult | None, launch_wall: float, was_cancelled: bool, *,
            force: tuple[TurnClass, FailClass, str] | None, never_launched: bool) -> CallOutcome:
    s = ctx.settings
    turn_dir = Path(req.turn_dir)
    if pres is None:
        pres = ProcResult(None, None, False, None, 0, turn_dir / "stream.jsonl", turn_dir / "stderr.txt", 0, None)
    rec = parse_stream(pres.stdout_path)
    res = rec.result or {}
    init_v = validate.validate_init(rec, ctx.expected_init, req)
    result_v = validate.validate_result(rec, ctx.expected_init, req)
    refusal = validate.detect_refusal(rec, None)
    stderr_unexp, _mcp, _ms = validate.scan_logs(pres.stderr_path, [], ctx.allow)
    if force is not None:
        cls, fail, detail = force
    else:
        cls, fail, detail = classify_mod.classify(
            pres, rec, init_v=init_v, result_v=result_v, reply_v=[], reconcile_v=[], server_fault=False,
            cancelled=was_cancelled, refusal=refusal)
    cost = _num(res.get("total_cost_usd"))
    if cost is not None:
        cost_usd, est = cost, False
    elif never_launched:
        cost_usd, est = 0.0, False
    else:
        cost_usd, est = float(req.max_budget_usd), True

    final_text = res.get("result") if isinstance(res.get("result"), str) else ""
    out_text = answer_text(final_text, rec.assistant_texts) if cls in (
        TurnClass.OK, TurnClass.MAX_TURNS, TurnClass.MAX_BUDGET) else ""
    parsed: ParsedOutput | None = None
    quote_ok: bool | None = None
    if cls == TurnClass.OK:
        parsed = labels_mod.parse_output(out_text, item_id=item.item_id, item_type=item.item_type,
                                         item_text=item.text)
        quote_ok = (labels_mod.quote_ok(parsed.quote, item.text) if parsed.quote is not None else None)
    status = _settled_status(ask_no, cls, parsed)
    usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
    mu = res.get("modelUsage") if isinstance(res.get("modelUsage"), dict) else {}
    mu_one = mu.get(req.model) if isinstance(mu.get(req.model), dict) else {}
    now = time.time()
    cols: dict[str, Any] = {
        "ended_at": now, "wall_ms": pres.wall_ms, "exit_code": pres.exit_code, "term_signal": pres.term_signal,
        "timed_out": 1 if pres.timed_out else 0, "kill_stage": pres.kill_stage, "n_lines": rec.n_lines,
        "n_bad_json": rec.n_bad_json, "n_init": rec.n_init, "n_result": rec.n_result, "subtype": res.get("subtype"),
        "is_error": None if res.get("is_error") is None else int(bool(res["is_error"])),
        "stop_reason": res.get("stop_reason"), "api_error_status": _int(res.get("api_error_status")),
        "num_turns": _int(res.get("num_turns")), "duration_ms": _int(res.get("duration_ms")),
        "total_cost_usd": cost, "input_tokens": _int(usage.get("input_tokens")),
        "cache_creation_tokens": _int(usage.get("cache_creation_input_tokens")),
        "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")), "model_usage_json": json.dumps(mu, sort_keys=True),
        "cost_basis": mu_one.get("costBasis"), "canonical_model": mu_one.get("canonicalModel"),
        "class": cls.value, "fail_class": fail.value if fail is not None else None, "fail_detail": detail or None,
        "violations_json": json.dumps(init_v + result_v), "stderr_unexpected_json": json.dumps(stderr_unexp),
        "output_text": out_text, "parse_ok": None if parsed is None else int(parsed.ok),
        "parse_errors_json": None if parsed is None else json.dumps(list(parsed.errors)),
        "quote_ok": None if quote_ok is None else int(quote_ok),
    }
    tier = item.tier

    def fn(conn: sqlite3.Connection) -> None:
        names = list(cols)
        conn.execute(f"UPDATE calls SET {', '.join(n + '=?' for n in names)} WHERE call_id=?", [cols[n] for n in names] + [call_id])
        conn.execute("INSERT INTO judge_ledger(call_id, run_id, model, tier, item_type, cost_usd, estimated, ts) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (call_id, ctx.run_id, req.model, tier, item.item_type.value, cost_usd, int(est), now))
        if status is not None:
            lab = parsed.labels if (status == "valid" and parsed is not None) else None
            conn.execute(
                "INSERT INTO judge_labels(item_id, prompt_version, status, final_call_id, n_asks, labels_json, "
                "quote, confidence, uncertain, justification, escalation_returned, escalation_mismatch, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.item_id, ctx.prompt.version, status, call_id, ask_no,
                 json.dumps(dict(lab), sort_keys=True) if lab is not None else None,
                 parsed.quote if lab is not None else None, parsed.confidence if lab is not None else None,
                 parsed.uncertain if lab is not None else None, parsed.justification if lab is not None else None,
                 parsed.escalation_returned if lab is not None else None,
                 int(parsed.escalation_mismatch) if lab is not None else None, now))

    ctx.jdb.write(fn)
    return CallOutcome(call_id, item.item_id, ask_no, try_no, cls.value, fail.value if fail is not None else None,
                       detail, out_text, parsed, quote_ok, cost_usd, est)
