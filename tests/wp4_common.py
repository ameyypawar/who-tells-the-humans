"""Helpers for the WP4 tests: a RunContext over a temp database, with no processes."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from tests.util import make_db, seed_run, seed_turn, tmp_dir
from wtth import db as dbmod
from wtth import stimuli
from wtth.config import load_settings
from wtth.orchestrator.context import Breaker, CostTracker, KGate, LaunchGate, RunContext, SlotPool
from wtth.types import CellSpec, EpisodeSpec


def have_real_stimuli() -> bool:
    try:
        s = load_settings()
        stimuli.episode_spec("t", "main", s.models.haiku, s.cells["NONE"], "L2", 1, 0, "rd")
        return True
    except NotImplementedError:
        return False


def fake_episode_spec(phase: str, scenario: str, model: str, cell: CellSpec, level: str, seed: int, queue_pos: int,
                      prefix: str) -> EpisodeSpec:
    """Stand-in for stimuli.episode_spec when WP1 has not landed."""
    h = [f"{prefix}-{14 + 7 * i}" for i in range(3)]
    return EpisodeSpec(f"{phase}/{scenario}/{model}/{cell.name}/{level}/s{seed}", phase, scenario, model, cell, level,
                       seed, (seed - 1) % 6, ((seed - 1) // 6) % 6, (seed - 1) % 3, tuple(h), ("A", "B", "C"), queue_pos)


def patch_stimuli_if_needed(tc: Any) -> None:
    if not have_real_stimuli():
        orig = stimuli.episode_spec
        stimuli.episode_spec = fake_episode_spec
        tc.addCleanup(setattr, stimuli, "episode_spec", orig)


def make_ctx(name: str, overrides: dict | None = None, *, k: int | None = None) -> tuple[RunContext, Path]:
    """Settings with a temp runs dir/slots/cwd, a created database, and a RunContext with no texts/toolsets."""
    d = tmp_dir(f"{name}_{os.getpid()}")
    ov = {"fake": True, "paths.runs_dir": str(d / "runs"), "paths.slots_root": str(d / "slots"),
          "paths.cwd": str(d / "cwd"), "scheduler.requeue_delay_s": 0}
    ov.update(overrides or {})
    s = load_settings(overrides=ov)
    s.paths.runs_dir.mkdir(parents=True, exist_ok=True)
    dbmod.create_db(s.paths.db_path)
    db = dbmod.Db(s.paths.db_path)
    launch = LaunchGate(s.scheduler.launch_gap_ms)
    breaker = Breaker(s)
    breaker.hold = launch.hold_for
    cost = CostTracker(s, db)
    cost.run_id = "run-t"
    ctx = RunContext(s, db, "run-t", "fake", {}, {}, {}, SlotPool(s.paths.slots_root, s.scheduler.slots),
                     KGate(k or s.scheduler.k_init), launch, cost, breaker, {}, {})
    db.write(lambda c: seed_run(c, "run-t", "x"))
    return ctx, d


def ledger(ctx: RunContext, episode_id: int, attempt: int, cost: float, phase: str, model: str, turn_id: str) -> None:
    def fn(c: Any) -> None:
        seed_turn(c, turn_id, episode_id, attempt, run_id="run-t", model=model)
        c.execute("INSERT INTO cost_ledger(turn_id, run_id, phase, model, cost_usd, estimated, ts) VALUES (?,?,?,?,?,0,?)",
                  (turn_id, "run-t", phase, model, cost, time.time()))
    ctx.db.write(fn)
