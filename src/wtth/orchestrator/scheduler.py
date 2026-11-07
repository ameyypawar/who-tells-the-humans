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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wtth import db as dbmod
from wtth import stimuli
from wtth.config import ConfigError
from wtth.orchestrator import alerts, guards
from wtth.orchestrator import episode as episode_mod
from wtth.orchestrator.events import log_event
from wtth.runner import proc as proc_mod
from wtth.runner import stream as stream_mod
from wtth.textnorm import canon_json
from wtth.types import EpisodeSpec

if TYPE_CHECKING:
    from wtth.db import Db
    from wtth.orchestrator.context import RunContext

FROZEN_PHASES = ("core", "sonnet", "mvp")
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


class Queue(list):
    """build_queue result: a list of EpisodeSpec plus the keys of probe episodes (stored as episodes.probe)."""

    probe_keys: set


def _resolve_level(settings: Any, arm_level: str | None, cli_level: str | None, phase: str) -> str:
    if arm_level == "F":
        if phase in FROZEN_PHASES and cli_level and settings.frozen_level and cli_level != settings.frozen_level:
            raise ConfigError(f"--level {cli_level} differs from frozen_level {settings.frozen_level} (phase {phase})")
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
    specs = Queue()
    specs.probe_keys = set()

    def add(ph_name: str, scenario: str, model: str, cell: str, lvl: str, seed: int) -> None:
        prefix = settings.scenarios[scenario].handle_prefix
        specs.append(stimuli.episode_spec(ph_name, scenario, model, settings.cells[cell], lvl, seed,
                                          base + len(specs), prefix))

    if ph.deny_probe is not None and not settings.fake:
        d = ph.deny_probe
        add(phase + "probe", "main", d.model, d.cell, d.level, d.seed)
        specs.probe_keys.add(specs[-1].episode_key)
    for arm in ph.arms:
        lvl = _resolve_level(settings, arm.level, level, phase)
        seeds_of: dict[str, list[int]] = {}
        order: list[str] = []
        probe_cells = {(ac.cell, sd) for ac in arm.cells if ac.probe for sd in ac.seeds}
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
                    if (cell, seed) in probe_cells:
                        specs.probe_keys.add(specs[-1].episode_key)
    return specs


def enqueue(db: "Db", specs: list[EpisodeSpec]) -> int:
    """INSERT OR IGNORE by episode_key (status queued). Returns the number of new rows."""
    probes = getattr(specs, "probe_keys", set())

    def fn(c: sqlite3.Connection) -> int:
        n = 0
        now = time.time()
        for sp in specs:
            cur = c.execute(
                "INSERT OR IGNORE INTO episodes(episode_key, phase, scenario, model, cell, level, seed, world, "
                "handle_idx, ticket_idx, paraphrase_idx, handles_json, ticket_sets_json, queue_pos, status, "
                "created_at, updated_at, runbook, c2_line_ref, c_real_note, forged_line_ref, probe) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)",
                (sp.episode_key, sp.phase, sp.scenario, sp.model, sp.cell.name, sp.level, sp.seed,
                 sp.cell.world.value, sp.handle_idx, sp.ticket_idx, sp.paraphrase_idx, canon_json(list(sp.handles)),
                 canon_json(list(sp.ticket_sets)), sp.queue_pos, now, now, sp.cell.runbook, sp.cell.c2,
                 int(sp.cell.c_note), "FORGED_LINE" if "FORGED" in sp.cell.c2 else None,
                 int(sp.episode_key in probes or sp.phase.endswith("probe"))))
            n += cur.rowcount
        return n
    return db.write(fn)


# ---- resume ---------------------------------------------------------------------------------------------

def charge_unfinished(ctx: "RunContext") -> int:
    """Safety net (D-005): every turn with ended_at IS NULL or class IS NULL gets a cost_ledger row and a terminal
    class, WHATEVER its attempt's status (turns killed on STOP/cap/operator stop never reach run_turn's persist).
    Cost: the stream's result if there is one, else the per-call cap as an estimate. Returns the number charged."""
    db, s = ctx.db, ctx.settings
    turns = db.read(lambda c: c.execute(
        "SELECT t.turn_id, t.turn_dir, t.model, e.phase FROM turns t JOIN episodes e USING(episode_id) "
        "WHERE t.ended_at IS NULL OR t.class IS NULL").fetchall())
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
        charges.append((t["turn_id"], t["phase"], t["model"], cost, est))
    if not charges:
        return 0

    def fn(c: sqlite3.Connection) -> None:
        now = time.time()
        for tid, phase, model, cost, est in charges:
            c.execute("UPDATE turns SET class='technical', fail_class=COALESCE(fail_class,'orch_restart'), "
                      "ended_at=COALESCE(ended_at,?) WHERE turn_id=? AND (class IS NULL OR ended_at IS NULL)", (now, tid))
            c.execute("INSERT OR IGNORE INTO cost_ledger(turn_id, run_id, phase, model, cost_usd, estimated, ts) "
                      "VALUES (?,?,?,?,?,?,?)", (tid, ctx.run_id, phase, model, cost, est, now))
        c.execute("INSERT INTO events(ts, run_id, level, kind, detail_json) VALUES (?,?,?,?,?)",
                  (now, ctx.run_id, "warn", "charged_unfinished", canon_json({"turns": len(charges)})))
    db.write(fn)
    return len(charges)


def resume_recover(ctx: "RunContext") -> None:
    """Charge every unfinished turn, then close attempts left 'running' by a dead orchestrator (41 section 2.7)."""
    db = ctx.db
    charge_unfinished(ctx)
    runs = db.read(lambda c: c.execute(
        "SELECT a.episode_id, a.attempt FROM attempts a WHERE a.status='running'").fetchall())
    for r in runs:
        def fn(c: sqlite3.Connection, r=r) -> None:
            now = time.time()
            c.execute("UPDATE attempts SET status='abandoned', finished_at=?, fail_class='orch_restart' "
                      "WHERE episode_id=? AND attempt=? AND status='running'", (now, r["episode_id"], r["attempt"]))
            c.execute("UPDATE episodes SET status='queued', not_before=NULL, updated_at=? WHERE episode_id=? AND status='running'",
                      (now, r["episode_id"]))
            c.execute("INSERT INTO events(ts, run_id, level, kind, episode_id, attempt, detail_json) VALUES (?,?,?,?,?,?,?)",
                      (now, ctx.run_id, "warn", "resume", r["episode_id"], r["attempt"], None))
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
        self._last_ok = self.clock()
        self._slot_hashes: dict[str, str | None] = {}
        self._max_probed = False
        self._rate_paged: set[str] = set()
        self._rss_at, self._rss_val = -1e18, 0.0
        self._orch_exc = 0
        self._cfg_fp: str | None = None
        self._rate_deadlines: list[float] = []
        self._last_halve = -1e18

    # -- helpers ----
    def _total_rss_mb(self) -> float:
        now = self.clock()
        if now - self._rss_at < 2.0:  # `ps` over all processes is ~17 ms: read it at most every 2 s
            return self._rss_val
        self._rss_at = now
        self._rss_val = self._read_rss_mb()
        return self._rss_val

    def _read_rss_mb(self) -> float:
        s = self.ctx.settings
        try:
            out = subprocess.run(["/bin/ps", "-axo", "rss=,command="], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return 0.0
        total = 0
        for ln in out.splitlines():
            if (str(s.paths.binary) in ln and str(s.paths.stimuli_dir) in ln) or \
                    ("-m wtth.mcp_server" in ln and f"--runs-root {s.paths.runs_dir}" in ln):  # CLI and MCP children
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
            "AND (not_before IS NULL OR not_before<=?) ORDER BY queue_pos",
            (self.phase, self.phase + "probe", now)).fetchall())
        # only episodes in the current queue (a cut applied on resume leaves the others queued but out of scope)
        return [r for r in rows if r["episode_id"] not in self.running and r["episode_key"] in self.specs]

    def _counts(self) -> dict[str, int]:
        rows = self.ctx.db.read(lambda c: c.execute(
            "SELECT episode_key, status FROM episodes WHERE phase IN (?,?)", (self.phase, self.phase + "probe")).fetchall())
        d: dict[str, int] = {}
        for r in rows:
            if r["episode_key"] in self.specs:
                d[r["status"]] = d.get(r["status"], 0) + 1
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
        self._resize(ctx.kgate.k)  # K never exceeds the slot count
        self._record_cut(specs)
        self._sweep_orphans()
        resume_recover(ctx)
        self._remote_settings_check()  # baseline hashes; later changes are warnings
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(sig, self._on_signal)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        t0 = self.clock()
        last_ramp = last_pressure = last_grow = last_health = last_backup = t0
        critical_since: float | None = None
        paged_critical = False
        stop_at_ts = self._parse_stop_at()
        self._cfg_fp = guards.config_fingerprint(s)
        self._rate_deadlines = self._error_rate_deadlines()
        last_rs = last_drift = t0
        self._say(f"[{self._utc()}] start phase={self.phase} run={ctx.run_id} episodes={len(specs)} K={ctx.kgate.k}")
        try:
            while True:
                now = self.clock()
                self._reap()
                self._breaker_poll()
                if now - last_rs >= s.scheduler.remote_check_interval_s:
                    last_rs = now
                    self._remote_settings_check()
                if now - last_drift >= s.scheduler.drift_check_interval_s:
                    last_drift = now
                    self._drift_check()
                self._error_rate_check()
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
                        if now - self._last_halve >= 60.0:  # halve at most once a minute
                            self._last_halve = now
                            self._resize(max(s.scheduler.k_min, ctx.kgate.k // 2))
                        if not paged_critical and now - critical_since > s.scheduler.pressure_high_stop_s:
                            paged_critical = True
                            alerts.page(s, ctx.db, ctx.run_id, self.phase, "memory_pressure_critical", {}, 0)
                    else:
                        critical_since, paged_critical = None, False
                        if self.pressure == 2 and ctx.kgate.k > s.scheduler.k_target:
                            self._resize(s.scheduler.k_target)
                if self.pressure == 1 and now - last_grow >= s.scheduler.k_grow_interval_s:
                    last_grow = now
                    # after a breaker trip K regrows toward k_target at once; only growth above k_target waits 15 min
                    limit = s.scheduler.k_max if ctx.breaker.seconds_since_trip() >= 15 * 60 else s.scheduler.k_target
                    if ctx.kgate.k < limit:
                        self._resize(ctx.kgate.k + 1)
                if now - last_ramp >= s.scheduler.ramp_interval_s:
                    last_ramp = now
                    self.ramp_e = min(self.ramp_e + 1, self._e_target())
                # launches
                if self.drain_reason is None and self.kill_reason is None:
                    self._launch_more(now)
                if now - last_health >= s.scheduler.health_interval_s:
                    last_health = now
                    self._health()
                    self._rates_check()
                if now - last_backup >= s.scheduler.backup_interval_s:
                    last_backup = now
                    await asyncio.to_thread(self._backup)
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
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                try:
                    asyncio.get_running_loop().remove_signal_handler(sig)
                except Exception:
                    pass
        self._reap()
        try:
            charge_unfinished(ctx)  # turns killed mid-flight are charged (estimate) before we report
        except Exception as exc:
            print(f"wtth: charge_unfinished failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
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
            log_event(ctx.db, ctx.run_id, "warn", "orphan_killed", detail={"pid": pid})

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

    def _drift_check(self) -> None:
        """configs/* or the public pre-registered text changed since startup: STOP."""
        try:
            fp = guards.config_fingerprint(self.ctx.settings)
        except OSError as exc:
            self._stop("config_unreadable", {"error": str(exc)}, EXIT_STOP, kill=True)
            return
        if fp != self._cfg_fp:
            self._stop("config_drift", {"was": self._cfg_fp, "now": fp}, EXIT_STOP, kill=True)

    def _error_rate_deadlines(self) -> list[float]:
        sc = self.ctx.settings.scheduler
        out = [time.time() + sc.error_rate_check_after_s]
        if sc.error_rate_abs_check:
            t = datetime.fromisoformat(sc.error_rate_abs_check).timestamp()
            if t > time.time():
                out.append(t)
        return sorted(out)

    def _error_rate_check(self) -> None:
        """D-005 / 42 A-21: at launch + 2 h and at T0 + 10.5 h, STOP + page if technical failures per attempt >= 2%."""
        if not self._rate_deadlines or time.time() < self._rate_deadlines[0]:
            return
        self._rate_deadlines.pop(0)
        ctx, sc = self.ctx, self.ctx.settings.scheduler
        row = ctx.db.read(lambda c: c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(a.status='failed' AND COALESCE(a.fail_class,'') NOT IN "
            "('stopped','cancelled','orch_restart')),0) f FROM attempts a JOIN episodes e USING(episode_id) "
            "WHERE e.phase IN (?,?) AND a.status != 'running'", (self.phase, self.phase + "probe")).fetchone())
        n, f = row["n"], row["f"]
        rate = f / n if n else 0.0
        log_event(ctx.db, ctx.run_id, "info", "error_rate_check", detail={"attempts": n, "failed": f, "rate": rate})
        if n >= sc.error_rate_min_attempts and rate >= sc.error_rate_stop:
            self._stop("error_rate", {"attempts": n, "failed": f, "rate": rate}, EXIT_STOP, kill=True)

    def _say(self, line: str) -> None:
        """Output that survives a dead pipe (tee killed by Ctrl-C): never raises."""
        try:
            self.out(line)
        except (OSError, ValueError):
            pass

    def _resize(self, k: int) -> None:
        self.ctx.kgate.resize(max(1, min(int(k), self.ctx.slots.n)))

    def _record_cut(self, specs: list) -> None:
        """Persist the cut in force (insert-only events row kind='cut'), once per distinct cut spec."""
        ctx, s = self.ctx, self.ctx.settings
        excluded = ctx.db.read(lambda c: [r["episode_key"] for r in c.execute(
            "SELECT episode_key FROM episodes WHERE phase IN (?,?) ORDER BY queue_pos",
            (self.phase, self.phase + "probe")).fetchall() if r["episode_key"] not in self.specs])
        spec = {"phase": self.phase, "tier": s.cuts.tier, "max_seed_by_cell": s.cuts.max_seed_by_cell,
                "n_queue": len(specs), "n_excluded": len(excluded), "excluded_sample": excluded[:20]}
        last = ctx.db.read(lambda c: c.execute("SELECT detail_json FROM events WHERE kind='cut' ORDER BY id DESC").fetchall())
        for r in last:
            d = json.loads(r[0] or "{}")
            if d.get("phase") == self.phase:
                if canon_json(d) == canon_json(spec):
                    return
                break
        log_event(ctx.db, ctx.run_id, "info", "cut", detail=spec)

    def _utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    def _parse_stop_at(self) -> float | None:
        """Next local HH:MM (today if still ahead, else tomorrow) as a unix time."""
        if not self.stop_at:
            return None
        hh, mm = (int(x) for x in self.stop_at.split(":"))
        now = datetime.now()
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now:
            t = t + timedelta(days=1)
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
                log_event(ctx.db, ctx.run_id, "warn", "cost_warn", detail={"spent": ctx.cost.spent()})
            spec = self.specs.get(cand["episode_key"])
            if spec is None:
                continue
            ensure = getattr(ctx, "_ensure_run", None)
            if ensure is not None:
                ctx._ensure_run = None
                ensure()
            # the G0 deny probe is the only episode under '<phase>probe' of the running phase; a phase whose own
            # name ends in 'probe' (D-022 exp1probe: integrity probes) runs full episodes
            fn = episode_mod.run_probe if cand["phase"] == self.phase + "probe" else episode_mod.run_attempt
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
                log_event(ctx.db, ctx.run_id, "error", "episode_task_exception", episode_id=eid,
                                      detail={"error": f"{type(exc).__name__}: {exc}"})
                continue
            self.finished += 1
            if res.status == "done":
                self._last_ok = self.clock()
                self._orch_exc = 0
            elif res.fail is not None and res.fail.value == "orch_restart" and res.detail.startswith("orchestrator_exception"):
                self._orch_exc += 1  # uncounted attempts: guard against a systematic bug looping forever
                if self._orch_exc >= 5:
                    self._stop("orchestrator_exception_loop", {"detail": res.detail[:200]}, EXIT_STOP, kill=True)
            row = ctx.db.read(lambda c: c.execute("SELECT status, phase FROM episodes WHERE episode_id=?", (eid,)).fetchone())
            if row and row["status"] == "failed":
                alerts.page(ctx.settings, ctx.db, ctx.run_id, self.phase, "episode_failed",
                            {"episode_id": eid, "fail": res.fail.value if res.fail else None, "detail": res.detail}, 0)
            if self.finished % ctx.settings.scheduler.checkpoint_every_episodes == 0:
                self._checkpoint()
            self._projection_check()

    def _model_counts(self) -> dict[str, dict[str, int]]:
        """_counts() split by model: {model: {status: n}} over the episodes of the current queue."""
        rows = self.ctx.db.read(lambda c: c.execute(
            "SELECT episode_key, model, status FROM episodes WHERE phase IN (?,?)",
            (self.phase, self.phase + "probe")).fetchall())
        d: dict[str, dict[str, int]] = {}
        for r in rows:
            if r["episode_key"] in self.specs:
                m = d.setdefault(r["model"], {})
                m[r["status"]] = m.get(r["status"], 0) + 1
        return d

    def _projection_check(self) -> None:
        """spent + per model (phase cost of the model / its done episodes) x its remaining episodes, once.
        For a one-model phase this is exactly spent + phase_cost / done x remaining. D-022: a mixed-model phase is
        projected per model, and the check waits until every model with episodes left has a finished one (a phase
        average would price the cheap model's episodes at the dear model's cost, or the reverse)."""
        ctx, s = self.ctx, self.ctx.settings
        if self.drain_reason or self.finished < s.budget.projection_after_episodes or self._max_probed:
            return
        counts = self._counts()
        done = counts.get("done", 0)
        if done < s.budget.projection_after_episodes:
            return
        by_model = self._model_counts()
        cost_rows = ctx.db.read(lambda c: c.execute(
            "SELECT model, COALESCE(SUM(cost_usd),0) FROM cost_ledger WHERE phase=? GROUP BY model",
            (self.phase,)).fetchall())
        cost_of = {r[0]: float(r[1]) for r in cost_rows}
        extra = 0.0
        for model, c in by_model.items():
            rem = c.get("queued", 0) + c.get("running", 0)
            if rem == 0:
                continue
            if c.get("done", 0) == 0:
                return   # no finished episode of this model yet: project later
            extra += cost_of.get(model, 0.0) / c["done"] * rem
        self._max_probed = True
        projected = ctx.cost.spent() + extra
        # D-028: budget.projection_stop_usd (exp2: $94.90) when set, else the cap, as before
        stop = s.budget.projection_stop_usd if getattr(s.budget, "projection_stop_usd", None) is not None \
            else s.budget.cap_usd
        if getattr(s.budget, "projection_stop_usd", None) is not None:
            log_event(ctx.db, ctx.run_id, "info", "cost_projection",
                      detail={"projected_usd": projected, "stop_usd": stop})
        if projected > stop:
            self._stop("cost_projection", {"projected_usd": projected, "cap_usd": s.budget.cap_usd, "stop_usd": stop},
                       EXIT_CAP, kill=False)

    def _breaker_poll(self) -> None:
        ctx, s = self.ctx, self.ctx.settings
        for note in ctx.breaker.notices:
            alerts.page(s, ctx.db, ctx.run_id, self.phase, note, {}, 0)
        ctx.breaker.notices.clear()
        p = ctx.breaker.take_pending()
        if p == "tripped":
            if ctx.kgate.k // 2 < s.scheduler.k_min:  # B3: halving would fall below k_min
                self._stop("breaker_k_floor", {"k": ctx.kgate.k}, EXIT_STOP, kill=True)
            else:
                self._resize(max(s.scheduler.k_min, ctx.kgate.k // 2))
                self.ramp_e = min(self.ramp_e, self._e_target())
                log_event(ctx.db, ctx.run_id, "warn", "breaker_trip", detail={"k": ctx.kgate.k})
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
            prev = self._slot_hashes.get(key)
            if prev is not None and h != prev:  # first sight of a fresh slot's file is not a change
                log_event(self.ctx.db, self.ctx.run_id, "warn", "policy_limits_changed", detail={"slot": i})
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
        except Exception as exc:
            print(f"wtth: backup failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            log_event(self.ctx.db, self.ctx.run_id, "warn", "backup_failed", detail={"error": str(exc)[:200]})

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
            self._say(line)
            s.paths.health_log.parent.mkdir(parents=True, exist_ok=True)
            with open(s.paths.health_log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # health output must never break the run
            self._say(f"health error: {type(exc).__name__}: {exc}")
