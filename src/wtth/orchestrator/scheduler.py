"""Queue building, resume, scheduling loop (research/41 section 2.7, research/43 B1-B5). Owner: WP4."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wtth import db as dbmod
from wtth import stimuli
from wtth.config import ConfigError
from wtth.orchestrator import alerts
from wtth.orchestrator import episode as episode_mod
from wtth.runner import proc as proc_mod
from wtth.runner import stream as stream_mod
from wtth.textnorm import canon_json
from wtth.types import EpisodeSpec

if TYPE_CHECKING:
    from wtth.db import Db
    from wtth.orchestrator.context import RunContext

EXIT_OK, EXIT_STARTUP, EXIT_STOP, EXIT_CAP, EXIT_LOCK, EXIT_OPERATOR = 0, 2, 3, 4, 5, 130
PHASE_BASE = 1_000_000


# ---- queue ----------------------------------------------------------------------------------------------

def permutation(settings: Any) -> list[str]:
    """PERM = Random(permutation_seed).sample(core_cells, len(core_cells)) (PREREG section 8; logged in the manifest)."""
    return random.Random(settings.permutation_seed).sample(list(settings.core_cells), len(settings.core_cells))


def _cut(settings: Any, phase: str, cell: str, seeds: list[int]) -> list[int]:
    if not seeds:
        return seeds
    c = settings.cuts
    if phase == "core" and c.tier != "full":
        n = c.tiers[c.tier].get(cell)
        if n is None:
            return []
        seeds = [s for s in seeds if s < seeds[0] + n]
    if cell in c.max_seed_by_cell:
        seeds = [s for s in seeds if s <= c.max_seed_by_cell[cell]]
    return seeds


def _resolve_level(settings: Any, arm_level: str | None, cli_level: str | None, phase: str) -> str:
    if arm_level == "F":
        lvl = cli_level or settings.frozen_level
        if lvl is None:
            raise ConfigError(f"phase {phase} needs the frozen level: set frozen_level or pass --level")
        return lvl
    if arm_level is None:
        if cli_level is None:
            raise ConfigError(f"phase {phase} needs --level")
        return cli_level
    return arm_level


def build_queue(settings: Any, phase: str, level: str | None) -> list[EpisodeSpec]:
    """Arms in config order; inside an arm seed-major, cells in PERM order (cells outside PERM follow in config order).

    queue_pos = 1e6 * phase ordinal + index, so a rerun keeps positions. Skips the G0 deny-probe episode in fake mode.
    """
    if phase not in settings.phases:
        raise ConfigError(f"unknown phase {phase!r}")
    ph = settings.phases[phase]
    perm = permutation(settings)
    base = ph.ordinal * PHASE_BASE
    specs: list[EpisodeSpec] = []

    def add(ph_name: str, scenario: str, model: str, cell: str, lvl: str, seed: int) -> None:
        prefix = settings.scenarios[scenario].handle_prefix
        specs.append(stimuli.episode_spec(ph_name, scenario, model, settings.cells[cell], lvl, seed,
                                          base + len(specs), prefix))

    if ph.deny_probe is not None and not settings.fake:
        d = ph.deny_probe
        add(phase + "probe", "main", d.model, d.cell, d.level, d.seed)
    for arm in ph.arms:
        lvl = _resolve_level(settings, arm.level, level, phase)
        seeds_of: dict[str, list[int]] = {}
        order: list[str] = []
        for ac in arm.cells:
            seeds_of.setdefault(ac.cell, [])
            seeds_of[ac.cell] += list(ac.seeds)
            if ac.cell not in order:
                order.append(ac.cell)
        for cell in list(seeds_of):
            seeds_of[cell] = _cut(settings, phase, cell, seeds_of[cell])
        ordered = [c for c in perm if c in seeds_of] + [c for c in order if c not in perm]
        for seed in sorted({s for v in seeds_of.values() for s in v}):
            for cell in ordered:
                if seed in seeds_of[cell]:
                    add(phase, arm.scenario, arm.model, cell, lvl, seed)
    return specs


def enqueue(db: "Db", specs: list[EpisodeSpec]) -> int:
    """INSERT OR IGNORE by episode_key (status queued). Returns the number of new rows."""
    def fn(c: sqlite3.Connection) -> int:
        n = 0
        now = time.time()
        for sp in specs:
            cur = c.execute(
                "INSERT OR IGNORE INTO episodes(episode_key, phase, scenario, model, cell, level, seed, world, "
                "handle_idx, ticket_idx, paraphrase_idx, handles_json, ticket_sets_json, queue_pos, status, "
                "created_at, updated_at, runbook, c2_line_ref, c_real_note, forged_line_ref) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?)",
                (sp.episode_key, sp.phase, sp.scenario, sp.model, sp.cell.name, sp.level, sp.seed,
                 sp.cell.world.value, sp.handle_idx, sp.ticket_idx, sp.paraphrase_idx, canon_json(list(sp.handles)),
                 canon_json(list(sp.ticket_sets)), sp.queue_pos, now, now, sp.cell.runbook, sp.cell.c2,
                 int(sp.cell.c_note), "FORGED_LINE" if "FORGED" in sp.cell.c2 else None))
            n += cur.rowcount
        return n
    return db.write(fn)


# ---- resume ---------------------------------------------------------------------------------------------

def resume_recover(ctx: "RunContext") -> None:
    """Close attempts left 'running' by a dead orchestrator (41 section 2.7 'Resume' steps 3-4)."""
    db, s = ctx.db, ctx.settings
    runs = db.read(lambda c: c.execute(
        "SELECT a.episode_id, a.attempt, e.phase, e.model FROM attempts a JOIN episodes e USING(episode_id) "
        "WHERE a.status='running'").fetchall())
    for r in runs:
        turns = db.read(lambda c: c.execute(
            "SELECT turn_id, turn_dir, model FROM turns WHERE episode_id=? AND attempt=? AND ended_at IS NULL",
            (r["episode_id"], r["attempt"])).fetchall())
        charges = []
        for t in turns:
            cost, est = s.models.budget_for(t["model"]), 1
            sp = Path(t["turn_dir"]) / "stream.jsonl"
            if sp.exists():
                try:
                    rec = stream_mod.parse_stream(sp)
                    if rec.result and rec.result.get("total_cost_usd") is not None:
                        cost, est = float(rec.result["total_cost_usd"]), 0
                except (NotImplementedError, OSError):
                    pass
            charges.append((t["turn_id"], cost, est))

        def fn(c: sqlite3.Connection, r=r, charges=charges) -> None:
            now = time.time()
            for tid, cost, est in charges:
                c.execute("UPDATE turns SET class='technical', fail_class='orch_restart', ended_at=? WHERE turn_id=?", (now, tid))
                c.execute("INSERT OR IGNORE INTO cost_ledger(turn_id, run_id, phase, model, cost_usd, estimated, ts) "
                          "VALUES (?,?,?,?,?,?,?)", (tid, ctx.run_id, r["phase"], r["model"], cost, est, now))
            c.execute("UPDATE attempts SET status='abandoned', finished_at=?, fail_class='orch_restart' "
                      "WHERE episode_id=? AND attempt=? AND status='running'", (now, r["episode_id"], r["attempt"]))
            c.execute("UPDATE episodes SET status='queued', not_before=NULL, updated_at=? WHERE episode_id=? AND status='running'",
                      (now, r["episode_id"]))
            c.execute("INSERT INTO events(ts, run_id, level, kind, episode_id, attempt, detail_json) VALUES (?,?,?,?,?,?,?)",
                      (now, ctx.run_id, "warn", "resume", r["episode_id"], r["attempt"], canon_json({"turns": len(charges)})))
        db.write(fn)

    def stragglers(c: sqlite3.Connection) -> None:
        c.execute("UPDATE episodes SET status='queued', updated_at=? WHERE status='running' AND NOT EXISTS ("
                  "SELECT 1 FROM attempts a WHERE a.episode_id=episodes.episode_id AND a.status='running')", (time.time(),))
    db.write(stragglers)


# ---- scheduler ------------------------------------------------------------------------------------------

def _default_pressure() -> int:
    try:
        return int(proc_mod.memory_pressure_level())
    except (NotImplementedError, OSError, ValueError):
        return 1


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class Scheduler:
    def __init__(self, ctx: "RunContext", phase: str, *, level: str | None = None, stop_at: str | None = None,
                 pressure_fn: Callable[[], int] | None = None, clock: Callable[[], float] = time.monotonic,
                 out: Callable[[str], Any] = lambda line: print(line, flush=True), tick: float = 0.05,
                 rss_fn: Callable[[], float] | None = None) -> None:
        self.ctx, self.phase, self.level = ctx, phase, level
        self.stop_at = stop_at
        self.pressure_fn = pressure_fn or _default_pressure
        self.clock, self.out, self.tick = clock, out, tick
        self.rss_fn = rss_fn or self._total_rss_mb
        self.running: dict[int, asyncio.Task] = {}
        self.specs: dict[str, EpisodeSpec] = {}
        self.finished = 0
        self.ramp_e = 1
        self.pressure = 1
        self.exit_code: int | None = None
        self.drain_reason: str | None = None
        self.kill_reason: str | None = None
        self.operator = False
        self.page_info: tuple[str, dict, int] | None = None
        self.max_e_seen = 0
        self._last_ok = self.clock()
        self._slot_hashes: dict[str, str | None] = {}
        self._max_probed = False
        self._rate_paged: set[str] = set()

    # -- helpers ----
    def _total_rss_mb(self) -> float:
        s = self.ctx.settings
        try:
            out = subprocess.run(["/bin/ps", "-axo", "rss=,command="], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return 0.0
        total = 0
        for ln in out.splitlines():
            if str(s.paths.binary) in ln and str(s.paths.stimuli_dir) in ln:
                try:
                    total += int(ln.split(None, 1)[0])
                except ValueError:
                    pass
        return total / 1024.0

    def _e_target(self) -> int:
        return max(1, self.ctx.kgate.k // self.ctx.settings.scheduler.episodes_per_k)

    def _next_candidates(self) -> list[sqlite3.Row]:
        now = time.time()
        rows = self.ctx.db.read(lambda c: c.execute(
            "SELECT episode_id, episode_key, model, phase FROM episodes WHERE phase IN (?,?) AND status='queued' "
            "AND (not_before IS NULL OR not_before<=?) ORDER BY queue_pos LIMIT 40",
            (self.phase, self.phase + "probe", now)).fetchall())
        return [r for r in rows if r["episode_id"] not in self.running]

    def _counts(self) -> dict[str, int]:
        rows = self.ctx.db.read(lambda c: c.execute(
            "SELECT status, COUNT(*) n FROM episodes WHERE phase IN (?,?) GROUP BY status", (self.phase, self.phase + "probe")).fetchall())
        d = {r["status"]: r["n"] for r in rows}
        d["total"] = sum(d.values())
        return d

    def _stop(self, reason: str, detail: dict, code: int, kill: bool) -> None:
        if self.exit_code is not None and self.drain_reason is None and self.kill_reason is None:
            return
        if kill:
            self.kill_reason = self.kill_reason or reason
        else:
            self.drain_reason = self.drain_reason or reason
        if self.page_info is None:
            self.page_info = (reason, detail, code)
        self.exit_code = self.exit_code if self.exit_code is not None else code

    # -- main loop ----
    async def run(self) -> int:
        ctx, s = self.ctx, self.ctx.settings
        specs = build_queue(s, self.phase, self.level)
        self.specs = {sp.episode_key: sp for sp in specs}
        for d in (s.paths.faults_dir, s.paths.stimuli_dir, s.paths.turns_dir, s.paths.manifests_dir,
                  s.paths.backups_dir, s.paths.reports_dir, s.paths.cwd):
            d.mkdir(parents=True, exist_ok=True)
        enqueue(ctx.db, specs)
        ctx.slots.ensure()
        self._sweep_orphans()
        resume_recover(ctx)
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._on_signal)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        t0 = self.clock()
        last_ramp = last_pressure = last_grow = last_health = last_backup = t0
        self._cut_at: float | None = None
        critical_since: float | None = None
        paged_critical = False
        stop_at_ts = self._parse_stop_at()
        self.out(f"[{self._utc()}] start phase={self.phase} run={ctx.run_id} episodes={len(specs)} K={ctx.kgate.k}")
        try:
            while True:
                now = self.clock()
                self._reap()
                self._breaker_poll()
                self._remote_settings_check()
                if stop_at_ts and time.time() >= stop_at_ts and self.drain_reason is None and self.kill_reason is None:
                    self._stop("stop_at", {}, EXIT_OK, kill=False)
                    self.page_info = None
                if self.operator and self.kill_reason is None:
                    self.kill_reason = "operator"
                    self.exit_code = EXIT_OPERATOR
                    self.page_info = None
                if self.kill_reason:
                    await self._kill_all()
                    break
                # K policy
                if now - last_pressure >= s.scheduler.pressure_interval_s:
                    last_pressure = now
                    self.pressure = self.pressure_fn()
                    if self.pressure >= 4:
                        critical_since = critical_since or now
                        ctx.kgate.resize(max(s.scheduler.k_min, ctx.kgate.k // 2))
                        if not paged_critical and now - critical_since > s.scheduler.pressure_high_stop_s:
                            paged_critical = True
                            alerts.page(s, ctx.db, ctx.run_id, self.phase, "memory_pressure_critical", {}, 0)
                    else:
                        critical_since, paged_critical = None, False
                        if self.pressure == 2 and ctx.kgate.k > s.scheduler.k_target:
                            ctx.kgate.resize(s.scheduler.k_target)
                if self.pressure == 1 and now - last_grow >= s.scheduler.k_grow_interval_s and \
                        ctx.breaker.seconds_since_trip() >= s.scheduler.breaker_resume_min * 60:
                    last_grow = now
                    recent = ctx.breaker.seconds_since_trip() < 15 * 60
                    limit = s.scheduler.k_target if recent else s.scheduler.k_max
                    if ctx.kgate.k < limit:
                        ctx.kgate.resize(ctx.kgate.k + 1)
                if now - last_ramp >= s.scheduler.ramp_interval_s:
                    last_ramp = now
                    self.ramp_e = min(self.ramp_e + 1, self._e_target())
                # launches
                if self.drain_reason is None and self.kill_reason is None:
                    self._launch_more(now)
                self.max_e_seen = max(self.max_e_seen, len(self.running))
                if now - last_health >= s.scheduler.health_interval_s:
                    last_health = now
                    self._health()
                    self._rates_check()
                if now - last_backup >= s.scheduler.backup_interval_s:
                    last_backup = now
                    self._backup()
                # exit?
                if not self.running:
                    if self.drain_reason is not None:
                        break
                    counts = self._counts()
                    if counts.get("queued", 0) == 0:
                        self.exit_code = EXIT_OK
                        break
                if self.running:
                    await asyncio.wait(list(self.running.values()), timeout=self.tick, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(self.tick)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    asyncio.get_running_loop().remove_signal_handler(sig)
                except Exception:
                    pass
        self._reap()
        code = self.exit_code if self.exit_code is not None else EXIT_OK
        if self.page_info is not None:
            reason, detail, pcode = self.page_info
            alerts.page(s, ctx.db, ctx.run_id, self.phase, reason, detail, pcode)
        self._health()
        self._backup()
        return code

    # -- pieces ----
    def _sweep_orphans(self) -> None:
        ctx, s = self.ctx, self.ctx.settings
        try:
            pids = proc_mod.orphan_sweep(s.paths.binary, s.paths.runs_dir, set())
        except (NotImplementedError, OSError):
            return
        for pid in pids or []:
            episode_mod.log_event(ctx.db, ctx.run_id, "warn", "orphan_killed", detail={"pid": pid})

    def _rates_check(self) -> None:
        """Page (no STOP) when max_turns exceeds 2% or max_budget 0.5% of a phase's turns (41 section 2.6 rows 17-18)."""
        ctx = self.ctx
        rows = ctx.db.read(lambda c: c.execute(
            "SELECT t.class, COUNT(*) n FROM turns t JOIN episodes e USING(episode_id) WHERE e.phase IN (?,?) "
            "GROUP BY t.class", (self.phase, self.phase + "probe")).fetchall())
        d = {r[0]: r[1] for r in rows}
        total = sum(d.values())
        if total < 100:
            return
        for cls, limit in (("max_turns", 0.02), ("max_budget", 0.005)):
            if d.get(cls, 0) / total > limit and cls not in self._rate_paged:
                self._rate_paged.add(cls)
                alerts.page(ctx.settings, ctx.db, ctx.run_id, self.phase, f"{cls}_rate_high",
                            {"count": d.get(cls, 0), "turns": total}, 0)

    def _utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    def _parse_stop_at(self) -> float | None:
        if not self.stop_at:
            return None
        hh, mm = (int(x) for x in self.stop_at.split(":"))
        now = datetime.now()
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now:
            t = t.replace(day=t.day) if t > now else t
            if t <= now:
                t = t + (datetime.fromtimestamp(86400) - datetime.fromtimestamp(0))
        return t.timestamp()

    def _on_signal(self) -> None:
        self.operator = True

    def _launch_more(self, now: float) -> None:
        ctx, s = self.ctx, self.ctx.settings
        if ctx.launch.hold_until > now:
            return
        limit = min(self.ramp_e, self._e_target())
        if len(self.running) >= limit:
            return
        if self.rss_fn() > s.scheduler.max_total_rss_mb:
            return
        for cand in self._next_candidates():
            if len(self.running) >= limit:
                break
            if not ctx.cost.can_launch(cand["model"]):
                self._stop("cap", {"spent": ctx.cost.spent(), "cap": s.budget.cap_usd}, EXIT_CAP, kill=False)
                return
            soft = s.budget.phase_soft_usd.get(self.phase)
            if soft is not None and ctx.cost.phase_spent(self.phase) > soft:
                self._stop("phase_soft_budget", {"phase": self.phase, "soft_usd": soft}, EXIT_CAP, kill=False)
                return
            if ctx.cost.over_warn() and not ctx.cost.warned:
                ctx.cost.warned = True
                episode_mod.log_event(ctx.db, ctx.run_id, "warn", "cost_warn", detail={"spent": ctx.cost.spent()})
            spec = self.specs.get(cand["episode_key"])
            if spec is None:
                continue
            ensure = getattr(ctx, "_ensure_run", None)
            if ensure is not None:
                ctx._ensure_run = None
                ensure()
            fn = episode_mod.run_probe if cand["phase"].endswith("probe") else episode_mod.run_attempt
            self.running[cand["episode_id"]] = asyncio.ensure_future(fn(ctx, spec, cand["episode_id"]))

    def _reap(self) -> None:
        ctx = self.ctx
        for eid, task in list(self.running.items()):
            if not task.done():
                continue
            del self.running[eid]
            try:
                res = task.result()
            except asyncio.CancelledError:
                continue
            except Exception as exc:
                episode_mod.log_event(ctx.db, ctx.run_id, "error", "episode_task_exception", episode_id=eid,
                                      detail={"error": f"{type(exc).__name__}: {exc}"})
                continue
            self.finished += 1
            if res.status == "done":
                self._last_ok = self.clock()
            row = ctx.db.read(lambda c: c.execute("SELECT status, phase FROM episodes WHERE episode_id=?", (eid,)).fetchone())
            if row and row["status"] == "failed":
                alerts.page(ctx.settings, ctx.db, ctx.run_id, self.phase, "episode_failed",
                            {"episode_id": eid, "fail": res.fail.value if res.fail else None, "detail": res.detail}, 0)
            if self.finished % ctx.settings.scheduler.checkpoint_every_episodes == 0:
                self._checkpoint()
            self._projection_check()

    def _projection_check(self) -> None:
        ctx, s = self.ctx, self.ctx.settings
        if self.drain_reason or self.finished < s.budget.projection_after_episodes or self._max_probed:
            return
        counts = self._counts()
        done = counts.get("done", 0)
        if done < s.budget.projection_after_episodes:
            return
        self._max_probed = True
        phase_cost = ctx.cost.phase_spent(self.phase)
        remaining = counts.get("queued", 0) + counts.get("running", 0)
        projected = ctx.cost.spent() + phase_cost / max(1, done) * remaining
        if projected > s.budget.cap_usd:
            self._stop("cost_projection", {"projected_usd": projected, "cap_usd": s.budget.cap_usd}, EXIT_CAP, kill=False)

    def _breaker_poll(self) -> None:
        ctx, s = self.ctx, self.ctx.settings
        for note in ctx.breaker.notices:
            alerts.page(s, ctx.db, ctx.run_id, self.phase, note, {}, 0)
        ctx.breaker.notices.clear()
        p = ctx.breaker.take_pending()
        if p == "tripped":
            if ctx.kgate.k <= s.scheduler.k_min:
                self._stop("breaker_k_floor", {"k": ctx.kgate.k}, EXIT_STOP, kill=True)
            else:
                ctx.kgate.resize(max(s.scheduler.k_min, ctx.kgate.k // 2))
                self.ramp_e = min(self.ramp_e, self._e_target())
                episode_mod.log_event(ctx.db, ctx.run_id, "warn", "breaker_trip", detail={"k": ctx.kgate.k})
        elif p == "stop":
            self._stop(ctx.breaker.stop_reason or "stop", {"breaker": ctx.breaker.stop_reason}, EXIT_STOP, kill=True)

    def _remote_settings_check(self) -> None:
        root = self.ctx.slots.root
        for i in range(self.ctx.slots.n):
            cfg = root / f"w{i:02d}" / "config"
            rs = cfg / "remote-settings.json"
            if rs.exists():
                try:
                    if json.loads(rs.read_text(encoding="utf-8") or "{}") != {}:
                        self._stop("remote_settings_nonempty", {"slot": i}, EXIT_STOP, kill=True)
                except (OSError, ValueError):
                    self._stop("remote_settings_unreadable", {"slot": i}, EXIT_STOP, kill=True)
            pl = cfg / "policy-limits.json"
            h = _sha(pl)
            key = str(pl)
            if key in self._slot_hashes and self._slot_hashes[key] != h:
                episode_mod.log_event(self.ctx.db, self.ctx.run_id, "warn", "policy_limits_changed", detail={"slot": i})
            self._slot_hashes[key] = h

    async def _kill_all(self) -> None:
        tasks = list(self.running.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.running.clear()

    def _checkpoint(self) -> None:
        try:
            c = sqlite3.connect(str(self.ctx.settings.paths.db_path), timeout=5)
            c.execute("PRAGMA wal_checkpoint(PASSIVE)")
            c.close()
        except sqlite3.Error:
            pass

    def _backup(self) -> None:
        s = self.ctx.settings
        try:
            dest = s.paths.backups_dir / f"wtth-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite"
            dbmod.backup(s.paths.db_path, dest)
        except Exception:
            pass

    def _health(self) -> None:
        ctx, s = self.ctx, self.ctx.settings
        try:
            counts = self._counts()
            ph = (self.phase, self.phase + "probe")
            fails = ctx.db.read(lambda c: c.execute(
                "SELECT a.fail_class, COUNT(*) n FROM attempts a JOIN episodes e USING(episode_id) "
                "WHERE e.phase IN (?,?) AND a.status='failed' GROUP BY a.fail_class", ph).fetchall())
            tcls = ctx.db.read(lambda c: c.execute(
                "SELECT t.class, COUNT(*) n FROM turns t JOIN episodes e USING(episode_id) "
                "WHERE e.phase IN (?,?) GROUP BY t.class", ph).fetchall())
            td = {r[0]: r[1] for r in tcls}
            total_t = sum(td.values()) or 1
            spent = ctx.cost.spent()
            done = counts.get("done", 0)
            per_ep = ctx.cost.phase_spent(self.phase) / done if done else 0.0
            fparts = ",".join(f"{r[0]}={r[1]}" for r in fails)
            nfail = sum(r[1] for r in fails)
            br = "closed"
            if ctx.launch.hold_until > self.clock():
                br = f"paused({int(ctx.launch.hold_until - self.clock())}s)"
            line = (f"[{self._utc()}] phase={self.phase} done={done}/{counts['total']} run={len(self.running)} "
                    f"q={counts.get('queued', 0)} att_fail={nfail}({fparts}) "
                    f"data(ref={td.get('refusal', 0)},maxT={td.get('max_turns', 0)},maxB={td.get('max_budget', 0)}) "
                    f"err_rate={100.0 * td.get('technical', 0) / total_t:.1f}% $={spent:.2f}/{s.budget.cap_usd:g} "
                    f"(ep≈${per_ep:.3f}) K={ctx.kgate.k}/{s.scheduler.k_max} procs={ctx.kgate.in_use} "
                    f"press={self.pressure} breaker={br} last_ok={int(self.clock() - self._last_ok)}s")
            self.out(line)
            s.paths.health_log.parent.mkdir(parents=True, exist_ok=True)
            with open(s.paths.health_log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # health output must never break the run
            self.out(f"health error: {type(exc).__name__}: {exc}")
