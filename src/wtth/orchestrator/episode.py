"""Episode protocol and attempt state machine (research/41 section 2.1, research/43 B1/B2). Owner: WP4."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from wtth import render, stimuli
from wtth.orchestrator import guards  # WP4-ADD
from wtth.runner import turn as turn_mod
from wtth.db import WriteFailed
from wtth.textnorm import canon_json, sha256_text
from wtth.types import (AttemptResult, EpisodeSpec, FailClass, Identity, TurnClass, TurnOutcome, TurnRequest,
                        TurnType)

ROUNDS = 5
SLOTS = (1, 2, 3)
NOT_COUNTED = ("stopped", "cancelled", "orch_restart")  # fail classes that do not use up an attempt

_sleep = asyncio.sleep  # patched by tests


# ---- attempt rows ---------------------------------------------------------------------------------------

def begin_attempt(db: Any, run_id: str, episode_id: int) -> int:
    """One transaction: INSERT attempts(status='running'); episodes -> running, n_attempts += 1."""
    def fn(c: sqlite3.Connection) -> int:
        n = c.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM attempts WHERE episode_id=?", (episode_id,)).fetchone()[0]
        now = time.time()
        c.execute("INSERT INTO attempts(episode_id, attempt, run_id, status, started_at) VALUES (?,?,?,'running',?)",
                  (episode_id, n, run_id, now))
        c.execute("UPDATE episodes SET status='running', n_attempts=n_attempts+1, not_before=NULL, updated_at=? "
                  "WHERE episode_id=?", (now, episode_id))
        return n
    return db.write(fn)


def close_attempt(db: Any, settings: Any, episode_id: int, attempt: int, *, status: str, fail: FailClass | None,
                  detail: str) -> bool:
    """Close a running attempt as failed/abandoned and move the episode on. Returns True when the episode is now
    permanently failed (max_attempts reached, B2)."""
    def fn(c: sqlite3.Connection) -> bool:
        now = time.time()
        fc = fail.value if fail else None
        c.execute("UPDATE attempts SET status=?, finished_at=?, fail_class=?, fail_detail=? "
                  "WHERE episode_id=? AND attempt=? AND status='running'", (status, now, fc, detail[:2000], episode_id, attempt))
        since = c.execute("SELECT COALESCE(MAX(ts),0) FROM events WHERE kind='manual_requeue' AND episode_id=?",
                          (episode_id,)).fetchone()[0]
        marks = ",".join("?" * len(NOT_COUNTED))
        n_fail = c.execute(f"SELECT COUNT(*) FROM attempts WHERE episode_id=? AND status='failed' AND started_at>? "
                           f"AND COALESCE(fail_class,'') NOT IN ({marks})", (episode_id, since, *NOT_COUNTED)).fetchone()[0]
        if status == "failed" and fc not in NOT_COUNTED and n_fail >= settings.max_attempts:
            c.execute("UPDATE episodes SET status='failed', fail_reason=?, updated_at=? WHERE episode_id=?",
                      (f"{fc}: {detail}"[:500], now, episode_id))
            return True
        delay = settings.scheduler.requeue_delay_s if (status == "failed" and fc not in NOT_COUNTED) else 0
        c.execute("UPDATE episodes SET status='queued', not_before=?, updated_at=? WHERE episode_id=?",
                  (now + delay if delay else None, now, episode_id))
        return False
    return db.write(fn)


def finish_attempt_ok(db: Any, episode_id: int, attempt: int) -> None:
    def fn(c: sqlite3.Connection) -> None:
        now = time.time()
        c.execute("UPDATE attempts SET status='done', finished_at=? WHERE episode_id=? AND attempt=? AND status='running'",
                  (now, episode_id, attempt))
        c.execute("UPDATE episodes SET status='done', final_attempt=?, updated_at=? WHERE episode_id=?",
                  (attempt, now, episode_id))
    db.write(fn)


def log_event(db: Any, run_id: str, level: str, kind: str, *, episode_id: int | None = None,
              attempt: int | None = None, turn_id: str | None = None, detail: Any = None) -> None:
    def fn(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO events(ts, run_id, level, kind, episode_id, attempt, turn_id, detail_json) "
                  "VALUES (?,?,?,?,?,?,?,?)", (time.time(), run_id, level, kind, episode_id, attempt, turn_id,
                                               canon_json(detail) if detail is not None else None))
    try:
        db.write(fn)
    except Exception:
        pass


# ---- stimuli files --------------------------------------------------------------------------------------

def ensure_system_file(ctx: Any, spec: EpisodeSpec) -> tuple[Path, str]:
    """runs/stimuli/system_<scenario>_<sys>_<variant>.txt, written once (0444) by render.write_system_prompt."""
    cache: dict = ctx.__dict__.setdefault("_sysfiles", {})
    key = (spec.scenario, spec.cell.system, spec.cell.runbook)
    if key not in cache:
        cache[key] = render.write_system_prompt(ctx.settings.paths.stimuli_dir, ctx.texts[spec.scenario], spec.cell)
    return cache[key]


def ensure_denyprobe_settings(settings: Any) -> Path:
    src = json.loads(Path(settings.paths.cc_settings).read_text(encoding="utf-8"))
    deny = list(src.setdefault("permissions", {}).setdefault("deny", []))
    deny.append(f"mcp__{settings.mcp_server_name}__submit_reviews")
    src["permissions"]["deny"] = deny
    path = settings.paths.denyprobe_settings
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(src, indent=1), encoding="utf-8")
    return path


# the exact rendered header (render.LOG_HEADER) at the start of a line
LOG_HEADER_RE = re.compile("(?m)^" + re.escape(render.LOG_HEADER.split("{clock}")[0]))


def probe_outcome_ok(o: TurnOutcome) -> bool:
    """Both deny-probe outcomes pass: the denied tool is absent from init.tools (the runner reports that as an
    init_mismatch on init.tools only), or it is present plus a permission_denials entry (class probe)."""
    if o.cls == TurnClass.PROBE:
        return True
    if o.cls != TurnClass.TECHNICAL or o.fail != FailClass.INIT_MISMATCH or not o.violations:
        return False
    for v in o.violations:
        if not v.startswith("init.tools"):
            return False
        got = v.split(" got ", 1)[-1]
        if "submit_reviews" in got:
            return False
    return True


def stored_d1_answer(db: Any, episode_id: int, attempt: int, slot: int, default: str) -> str:
    """The D1 answer exactly as stored in debrief_answers (what the analysis sees)."""
    row = db.read(lambda c: c.execute(
        "SELECT answer_text FROM debrief_answers WHERE episode_id=? AND attempt=? AND slot=? AND question='D1'",
        (episode_id, attempt, slot)).fetchone())
    return row[0] if row is not None else default


# ---- mechanical exposure check (42 S-19) ---------------------------------------------------------------

def exposure_checks(spec: EpisodeSpec, texts: Any, state: Any, prompt: Any, r: int) -> dict[str, bool]:
    """Booleans only (no text is stored): the round-r prompt holds every visible scripted line and the right log state."""
    text = prompt.text
    checks: dict[str, bool] = {}
    for k in range(1, r + 1):
        posts = stimuli.scripted_posts(spec, texts, k)
        checks[f"scripted_r{k}"] = all(p.text in text for p in posts)
    log_rows = [x for x in state.log if x.visible_from_round <= r]
    m = LOG_HEADER_RE.search(text)   # line-anchored: board lines start with "[", so a quoted header never matches
    has_log = m is not None
    if spec.cell.world.value == "R0":
        checks["r0_no_log"] = not has_log and not log_rows
    else:
        checks["log_block_present"] = has_log
        block = text[m.start():].split("\n\n", 1)[0] if m else ""  # E- lines are counted inside the block only
        checks["log_entry_count"] = sum(1 for ln in block.split("\n") if ln.startswith("E-")) == len(log_rows)
        if not log_rows:
            checks["log_empty_line"] = "No entries." in text
    checks["visible_log_ids"] = len(prompt.visible_log_ids) == len(log_rows)
    return checks


def record_exposure(ctx: Any, req: TurnRequest, spec: EpisodeSpec, texts: Any, state: Any) -> bool:
    checks = exposure_checks(spec, texts, state, req.prompt, req.round)
    ok = all(checks.values())

    def fn(c: sqlite3.Connection) -> None:
        c.execute("INSERT OR IGNORE INTO exposure_checks(turn_id, checks_json, ok, ts) VALUES (?,?,?,?)",
                  (req.turn_id, canon_json(checks), int(ok), time.time()))
    ctx.db.write(fn)
    if not ok:
        ctx.breaker.request_stop("exposure_check_failed")
        log_event(ctx.db, ctx.run_id, "page", "exposure_check_failed", episode_id=req.episode_id, attempt=req.attempt,
                  turn_id=req.turn_id, detail={k: v for k, v in checks.items() if not v})
    return ok


# ---- turn plumbing --------------------------------------------------------------------------------------

def _audit(ctx: Any, spec: EpisodeSpec, turn_id: str) -> bool:
    s = ctx.settings
    return spec.phase in s.audit.all_turns_phases or int(turn_id[:8], 16) % s.audit.production_sample_mod == 0


def _turn_dir(ctx: Any, episode_id: int, attempt: int, kind: str, rnd: int | None, slot: int, turn_id: str) -> Path:
    return (ctx.settings.paths.turns_dir / f"{episode_id:05d}-a{attempt}" /
            f"{kind}-r{rnd if rnd is not None else 0}-s{slot}-{turn_id[:8]}")


async def _launch(ctx: Any, req: TurnRequest, cancel: asyncio.Event, key: str, try_no: int) -> TurnOutcome:
    """Acquire a slot dir, run the turn (run_turn itself takes the K permit and the launch gap; it also sets
    try_no and the cap columns), then feed the breaker and the cost reservation."""
    k_at = ctx.kgate.k
    if try_no > 1:
        ctx.cost.add_calls(key, 1)  # WP4-ADD: a debrief retry is one more call than the reservation covered
    slot = ctx.slots.acquire()
    try:
        outcome = await turn_mod.run_turn(ctx, req, slot, cancel=cancel)
    finally:
        ctx.slots.release(slot)
    ctx.cost.turn_done(key)
    ctx.breaker.record(outcome.cls, outcome.fail)
    if req.audit_bodies and not outcome.fail:  # WP4-ADD: B10 production raw-body sample; any problem is a STOP
        problems = guards.body_audit(ctx, req)
        if problems:
            ctx.breaker.request_stop("raw_body_audit_failed")
            log_event(ctx.db, ctx.run_id, "page", "raw_body_audit_failed", episode_id=req.episode_id,
                      attempt=req.attempt, turn_id=req.turn_id, detail={"problems": problems[:10]})

    def fn(c: sqlite3.Connection) -> None:
        c.execute("UPDATE turns SET k_at_launch=? WHERE turn_id=? AND k_at_launch IS NULL", (k_at, req.turn_id))
    try:
        ctx.db.write(fn)
    except Exception:
        pass
    return outcome


def _failed(o: TurnOutcome) -> bool:
    return o.cls in (TurnClass.TECHNICAL, TurnClass.STOP)


async def _run_group(jobs: list) -> tuple[list[Any], Any]:
    """Run coroutines concurrently; each returns (outcome, failed_flag-aware). Returns (all results, root failure).

    A job returns a TurnOutcome. The first failing outcome that is not a sibling cancellation is the root cause;
    the shared cancel event is set by the caller's job wrapper when it sees a failure.
    """
    tasks = [asyncio.ensure_future(j) for j in jobs]
    results: list[Any] = [None] * len(tasks)
    root = None
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                o = t.result()
                results[tasks.index(t)] = o
                if o is not None and _failed(o) and o.fail != FailClass.CANCELLED and root is None:
                    root = o
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return results, root


# ---- the episode ----------------------------------------------------------------------------------------

async def run_attempt(ctx: Any, spec: EpisodeSpec, episode_id: int) -> AttemptResult:
    s, db = ctx.settings, ctx.db
    attempt = begin_attempt(db, ctx.run_id, episode_id)
    key = f"{episode_id}"
    ctx.cost.reserve(key, spec.model, s.budget.reserve_calls)
    try:
        status, fail, detail, root_id = await _protocol(ctx, spec, episode_id, attempt)
    except asyncio.CancelledError:
        close_attempt(db, s, episode_id, attempt, status="failed", fail=FailClass.STOPPED, detail="cancelled")
        raise
    except WriteFailed as exc:  # the database failed after a turn ran: STOP (research/41 + WP3 contract)
        ctx.breaker.request_stop("db_write_failed")
        log_event(db, ctx.run_id, "page", "db_write_failed", episode_id=episode_id, attempt=attempt, detail={"error": str(exc)})
        status, fail, detail, root_id = "failed", FailClass.API_OTHER, f"WriteFailed: {exc}", None
    except Exception as exc:  # orchestrator bug: fail the attempt visibly, do not kill the scheduler
        # WP4-ADD: an orchestrator bug is not the episode's fault: close the attempt UNCOUNTED (orch_restart) and page
        log_event(db, ctx.run_id, "page", "attempt_exception", episode_id=episode_id, attempt=attempt,
                  detail={"error": f"{type(exc).__name__}: {exc}"})
        status, fail, detail, root_id = "failed", FailClass.ORCH_RESTART, f"orchestrator_exception: {type(exc).__name__}: {exc}", None
    finally:
        ctx.cost.release(key)
    if status == "failed":
        close_attempt(db, s, episode_id, attempt, status="failed", fail=fail, detail=detail)
    return AttemptResult(episode_id, attempt, status, fail, detail, root_id)


def _witness_request(ctx: Any, spec: EpisodeSpec, texts: Any, toolset: Any, sysinfo: tuple[Path, str],
                     episode_id: int, attempt: int, r: int, slot: int, prompt: Any) -> TurnRequest:
    s = ctx.settings
    turn_id = uuid.uuid4().hex
    lines = stimuli.tickets_for(spec, texts, r, slot)
    clock = stimuli.witness_stamp(r, slot)
    ident = Identity(ctx.run_id, episode_id, attempt, r, slot, spec.handles[slot - 1], clock,
                     stimuli.clock_min(clock), stimuli.witness_visible_from(r), spec.cell.world, "tool", turn_id,
                     toolset.name, stimuli.ticket_ids(lines), receipts=spec.cell.receipts)
    return TurnRequest(turn_id, episode_id, attempt, TurnType.WITNESS, r, slot, spec.handles[slot - 1], spec.model,
                       sysinfo[0], sysinfo[1], prompt, toolset, ident, s.max_turns.witness,
                       s.models.budget_for(spec.model), s.timeouts.turn_s, _audit(ctx, spec, turn_id),
                       s.paths.cc_settings, _turn_dir(ctx, episode_id, attempt, "witness", r, slot, turn_id))


async def _protocol(ctx: Any, spec: EpisodeSpec, episode_id: int, attempt: int):
    s, db = ctx.settings, ctx.db
    texts = ctx.texts[spec.scenario]
    cell = spec.cell
    toolset = ctx.toolsets[s.scenarios[spec.scenario].toolset_map.get(cell.toolset, cell.toolset)]
    sysinfo = ensure_system_file(ctx, spec)
    key = f"{episode_id}"
    cancel = asyncio.Event()

    async def witness(r: int, slot: int, prompt: Any, state: Any) -> TurnOutcome:
        req = _witness_request(ctx, spec, texts, toolset, sysinfo, episode_id, attempt, r, slot, prompt)
        o = await _launch(ctx, req, cancel, key, 1)
        if _failed(o) and o.fail != FailClass.CANCELLED:
            cancel.set()
        elif not _failed(o) and not record_exposure(ctx, req, spec, texts, state):
            cancel.set()
            # the failed check is the ROOT failure, so no next-round turn is launched
            return dataclasses.replace(o, cls=TurnClass.TECHNICAL, fail=FailClass.RECONCILE,
                                       detail="exposure_check_failed")
        return o

    for r in range(1, ROUNDS + 1):
        if cancel.is_set():
            return "failed", FailClass.CANCELLED, "cancelled before round launch", None
        stimuli.inject_round(db, spec, episode_id, attempt, r, texts, ctx.run_id)
        state = db.read(lambda c: stimuli.load_state(c, spec, episode_id, attempt))
        prompts = {slot: render.round_prompt(state, texts, r, slot) for slot in SLOTS}  # before any launch
        results, root = await _run_group([witness(r, slot, prompts[slot], state) for slot in SLOTS])
        if root is not None:
            return "failed", root.fail, root.detail, root.turn_id
    if cancel.is_set():
        return "failed", FailClass.CANCELLED, "cancelled before debrief", None
    # debrief
    state = db.read(lambda c: stimuli.load_state(c, spec, episode_id, attempt))

    async def debrief_slot(slot: int) -> TurnOutcome | None:
        d1_answer: str | None = None
        for which, tt in (("D1", TurnType.D1), ("D2", TurnType.D2)):
            prompt = render.debrief_prompt(state, texts, slot, which, d1_answer)
            tries = s.scheduler.debrief_tries
            for try_no in range(1, tries + 1):
                if cancel.is_set():
                    return None
                turn_id = uuid.uuid4().hex
                req = TurnRequest(turn_id, episode_id, attempt, tt, None, slot, spec.handles[slot - 1], spec.model,
                                  sysinfo[0], sysinfo[1], prompt, None, None, s.max_turns.debrief,
                                  s.models.budget_for(spec.model), s.timeouts.turn_s, _audit(ctx, spec, turn_id),
                                  s.paths.cc_settings, _turn_dir(ctx, episode_id, attempt, which.lower(), None, slot, turn_id))
                o = await _launch(ctx, req, cancel, key, try_no)
                if not _failed(o):
                    if which == "D1":
                        d1_answer = stored_d1_answer(db, episode_id, attempt, slot, o.final_text or "")
                    break
                if o.cls == TurnClass.STOP or o.fail == FailClass.CANCELLED or try_no == tries:
                    if o.fail != FailClass.CANCELLED:
                        cancel.set()
                    return o
                backoff = s.scheduler.debrief_backoff_s
                await _sleep(backoff[min(try_no - 1, len(backoff) - 1)] if backoff else 0)
        return None

    results, root = await _run_group([debrief_slot(slot) for slot in SLOTS])
    if root is None:
        for o in results:  # a failure that was only a cancellation without a root cannot happen; be safe
            if o is not None and _failed(o):
                root = o
                break
    if root is not None:
        return "failed", root.fail, root.detail, root.turn_id
    return _finalize(ctx, spec, episode_id, attempt)


def _finalize(ctx: Any, spec: EpisodeSpec, episode_id: int, attempt: int):
    s, db = ctx.settings, ctx.db
    rows = db.read(lambda c: c.execute(
        "SELECT turn_id, turn_type, slot, class FROM turns WHERE episode_id=? AND attempt=?", (episode_id, attempt)).fetchall())
    ok = lambda cl: cl not in ("technical", "stop") and cl is not None  # noqa: E731
    witness_ok = [r for r in rows if r["turn_type"] == "witness" and ok(r["class"])]
    witness_bad = [r for r in rows if r["turn_type"] == "witness" and not ok(r["class"])]
    deb_ok = {(r["turn_type"], r["slot"]) for r in rows if r["turn_type"] in ("d1", "d2") and ok(r["class"])}
    if witness_bad or len(witness_ok) != 15 or len(deb_ok) != 6:
        return ("failed", FailClass.RECONCILE,
                f"turn counts: witness_ok={len(witness_ok)} witness_bad={len(witness_bad)} debrief_ok={len(deb_ok)}", None)
    for r in rows:
        if (s.paths.faults_dir / f"{r['turn_id']}.jsonl").exists():
            return "failed", FailClass.SERVER_FAULT, f"fault file for turn {r['turn_id']}", r["turn_id"]
    finish_attempt_ok(db, episode_id, attempt)
    return "done", None, "", None


# ---- G0 deny probe ----------------------------------------------------------------------------------------

async def run_probe(ctx: Any, spec: EpisodeSpec, episode_id: int) -> AttemptResult:
    """One witness-shaped round-1 turn with --settings pointing at the deny-probe file (class probe)."""
    s, db = ctx.settings, ctx.db
    attempt = begin_attempt(db, ctx.run_id, episode_id)
    try:
        texts = ctx.texts[spec.scenario]
        toolset = ctx.toolsets[s.scenarios[spec.scenario].toolset_map.get(spec.cell.toolset, spec.cell.toolset)]
        sysinfo = ensure_system_file(ctx, spec)
        stimuli.inject_round(db, spec, episode_id, attempt, 1, texts, ctx.run_id)
        state = db.read(lambda c: stimuli.load_state(c, spec, episode_id, attempt))
        prompt = render.round_prompt(state, texts, 1, 1)
        req = _witness_request(ctx, spec, texts, toolset, sysinfo, episode_id, attempt, 1, 1, prompt)
        req = dataclasses.replace(req, turn_type=TurnType.PROBE, settings_path=ensure_denyprobe_settings(s))
        o = await _launch(ctx, req, asyncio.Event(), str(episode_id), 1)
    except asyncio.CancelledError:
        close_attempt(db, s, episode_id, attempt, status="failed", fail=FailClass.STOPPED, detail="cancelled")
        raise
    except Exception as exc:  # never leave a probe attempt 'running'
        detail = f"probe_exception: {type(exc).__name__}: {exc}"
        log_event(db, ctx.run_id, "error", "probe_exception", episode_id=episode_id, attempt=attempt,
                  detail={"error": f"{type(exc).__name__}: {exc}"})
        close_attempt(db, s, episode_id, attempt, status="failed", fail=FailClass.API_OTHER, detail=detail)
        return AttemptResult(episode_id, attempt, "failed", FailClass.API_OTHER, detail, None)
    if o.cls == TurnClass.STOP or (o.cls == TurnClass.TECHNICAL and not probe_outcome_ok(o)):
        close_attempt(db, s, episode_id, attempt, status="failed", fail=o.fail, detail=o.detail)
        return AttemptResult(episode_id, attempt, "failed", o.fail, o.detail, o.turn_id)
    finish_attempt_ok(db, episode_id, attempt)
    return AttemptResult(episode_id, attempt, "done", None, "", None)
