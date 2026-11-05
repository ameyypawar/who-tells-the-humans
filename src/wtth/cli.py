"""Command line: init-db, manifest, render, run, status, audit, gate, redact. Owner: WP4.

Exit codes: 0 ok, 2 startup problems / bad arguments, 3 STOP, 4 cost cap, 5 lock held, 130 operator interrupt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from wtth import db as dbmod

REPO = Path(__file__).resolve().parents[2]
# Composed at runtime so the literal key prefix never appears in the repo. Same value as tests.util.DUMMY_KEY.
FAKE_KEY = "sk-" + "ant-" + "TESTDUMMY0000"
PRODUCTION = {"slots": Path("/Users/amey/.local/wtth/slots"), "cwd": Path("/private/tmp/ws/run")}


def _settings(args: argparse.Namespace) -> Any:
    from wtth.config import load_settings

    ov: dict[str, Any] = {}
    fake = bool(getattr(args, "fake", None))
    if fake:
        fk = REPO / "runs" / "fake"
        ov.update({"fake": True, "paths.runs_dir": str(fk), "paths.slots_root": str(fk / "slots"),
                   "paths.cwd": str(fk / "cwd")})
    if getattr(args, "runs_dir", None):
        ov["paths.runs_dir"] = str(Path(args.runs_dir).resolve())
    if getattr(args, "db", None):
        p = Path(args.db).resolve()
        if p.name != "wtth.sqlite":
            raise SystemExit("--db must be a file named wtth.sqlite (the server derives --runs-root from its directory)")
        ov["paths.runs_dir"] = str(p.parent)
    if getattr(args, "k_init", None):
        ov["scheduler.k_init"] = int(args.k_init)
    for item in getattr(args, "max_seed", None) or []:
        cell, _, n = item.partition("=")
        ov.setdefault("cuts", {}).setdefault("max_seed_by_cell", {})[cell] = int(n)
    for item in getattr(args, "set", None) or []:
        k, _, v = item.partition("=")
        ov[k] = yaml.safe_load(v)
    return load_settings(Path(args.config) if getattr(args, "config", None) else None, ov)


def _guard_fake(settings: Any) -> list[str]:
    """Rule 7: fake mode never touches production paths."""
    bad = []
    if settings.paths.db_path == REPO / "runs" / "wtth.sqlite":
        bad.append("fake mode would use the production database runs/wtth.sqlite")
    if settings.paths.slots_root == PRODUCTION["slots"]:
        bad.append("fake mode would use the production slot dirs")
    if settings.paths.cwd == PRODUCTION["cwd"]:
        bad.append("fake mode would use the production cwd /private/tmp/ws/run")
    return bad


def _write_shim(settings: Any, behavior: Path) -> Path:
    d = settings.paths.runs_dir / "fake"
    d.mkdir(parents=True, exist_ok=True)
    shim = d / "claude"
    doc = json.loads(Path(behavior).read_text(encoding="utf-8"))
    if "contract" not in doc:  # the fake checks the argv/env/cwd/slot contract; default it to this run's paths
        from tests import fake_claude  # the contract stores only the key's sha256

        doc["contract"] = fake_claude.contract_for(settings, FAKE_KEY)
    beh = d / "behavior.json"
    beh.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    shim.write_text(f'#!/bin/sh\nexec {settings.paths.python} -I {REPO / "tests" / "fake_claude.py"} '
                    f'--behavior {beh} "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def make_context(settings: Any, mode: str, run_id: str, api_key: str) -> Any:
    """Build the RunContext (texts for every scenario the settings can use, private text, gates)."""
    from wtth import prereg
    from wtth.config import (CONFIGS_DIR, env_allowlist, load_expected_init, load_log_allowlists, load_toolsets)
    from wtth.orchestrator.context import (Breaker, CostTracker, KGate, LaunchGate, RunContext, SlotPool)

    db = dbmod.Db(settings.paths.db_path)
    launch = LaunchGate(settings.scheduler.launch_gap_ms)
    breaker = Breaker(settings)
    breaker.hold = launch.hold_for
    cost = CostTracker(settings, db)
    cost.run_id = run_id
    scenarios = sorted({a.scenario for p in settings.phases.values() for a in p.arms} | {"main"})
    texts = {sc: prereg.load_scenario_texts(sc, settings) for sc in scenarios}
    private = prereg.load_private(settings)
    exp = load_expected_init(CONFIGS_DIR / "expected_init.json")
    exp["cwd"] = str(settings.paths.cwd)  # the pinned value in production; the test cwd in fake runs
    allow = load_log_allowlists(CONFIGS_DIR / "log_allowlists.json")
    allow["env"] = env_allowlist(CONFIGS_DIR / "env_allowlist.json")
    return RunContext(settings, db, run_id, mode, load_toolsets(settings.paths.tools_yaml), texts, private,
                      SlotPool(settings.paths.slots_root, settings.scheduler.slots), KGate(min(settings.scheduler.k_init, settings.scheduler.slots)),
                      launch, cost, breaker, exp, allow, _api_key=api_key)


def _cmd_init_db(args: argparse.Namespace) -> int:
    s = _settings(args)
    dbmod.create_db(s.paths.db_path)
    print(f"database ready: {s.paths.db_path}")
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    from wtth.orchestrator import manifest

    s = _settings(args)
    mode = "fake" if s.fake else "live"
    if args.verify:
        problems = manifest.verify_startup(s, mode, args.phase)
        for p in problems:
            print(f"PROBLEM: {p}")
        if problems:
            return 2
    print(json.dumps(manifest.build_manifest(s, "manifest-preview", args.phase, mode), indent=2, sort_keys=True, default=str))
    return 0


def _private_seed(settings: Any, cell: str, seed: int) -> bool:
    c = settings.cells[cell]
    return ("FORGED" in c.c2 or c.c2 == "CLAIM") and (seed - 1) % 3 != 0 or "FORGED" in c.c2


def _cmd_render(args: argparse.Namespace) -> int:
    from wtth import prereg, render, stimuli
    from wtth.types import EpisodeState

    s = _settings(args)
    if args.cell not in s.cells:
        print(f"unknown cell {args.cell}", file=sys.stderr)
        return 2
    if _private_seed(s, args.cell, args.seed) and not args.unmask:
        print("refused: this cell/seed renders withheld text; pass --unmask to print it", file=sys.stderr)
        return 2
    level = args.level or s.frozen_level or "L2"
    texts = prereg.load_scenario_texts(args.scenario, s)
    spec = stimuli.episode_spec("render", args.scenario, s.models.haiku, s.cells[args.cell], level, args.seed, 0,
                                s.scenarios[args.scenario].handle_prefix)
    state = EpisodeState(spec, 0, 1, (), (), (), (), {})
    text = render.round_prompt(state, texts, args.round, args.slot).text
    if not args.unmask:
        text = prereg.mask_private(text, prereg.load_private(s))
    print(text)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from wtth import secrets
    from wtth.orchestrator import manifest
    from wtth.orchestrator.lock import LockHeld, RunLock
    from wtth.config import ConfigError
    from wtth.orchestrator.scheduler import Scheduler, build_queue

    if args.live and args.fake:
        print("choose --live or --fake, not both", file=sys.stderr)
        return 2
    s = _settings(args)
    mode = "fake" if args.fake else "live"
    if args.fake:
        bad = _guard_fake(s)
        if bad:
            for b in bad:
                print(f"PROBLEM: {b}")
            return 2
        shim = _write_shim(s, Path(args.fake))
        s = _rebuild_with_binary(args, shim)
    elif not args.live:
        print("pass --live or --fake BEHAVIOR.json", file=sys.stderr)
        return 2
    if args.phase not in s.phases:
        print(f"unknown phase {args.phase}", file=sys.stderr)
        return 2
    problems = manifest.verify_startup(s, mode, args.phase)
    if problems:
        for p in problems:
            print(f"PROBLEM: {p}")
        return 2
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:4]
    try:
        with RunLock(s.paths.lock_path, run_id):
            dbmod.create_db(s.paths.db_path)
            api_key = FAKE_KEY if args.fake else secrets.load_api_key(s.paths.key_file)
            man = manifest.build_manifest(s, run_id, args.phase, mode)
            (s.paths.manifests_dir).mkdir(parents=True, exist_ok=True)
            (s.paths.manifests_dir / f"{run_id}.json").write_text(json.dumps(man, indent=2, sort_keys=True, default=str))
            ctx = make_context(s, mode, run_id, api_key)

            def start(c: Any) -> None:
                c.execute("INSERT INTO runs(run_id, phase, mode, started_at, manifest_sha, manifest_json) VALUES (?,?,?,?,?,?)",
                          (run_id, args.phase, mode, time.time(), manifest.manifest_sha(man),
                           json.dumps(man, sort_keys=True, default=str)))
            ctx._ensure_run = lambda: ctx.db.write(start)  # runs row is written when the first episode launches
            if args.requeue_failed:
                _requeue_failed(ctx, args.phase)
            try:
                build_queue(s, args.phase, args.level)  # validates level/freeze rules before anything launches
            except ConfigError as exc:
                print(f"PROBLEM: {exc}")
                return 2
            sched = Scheduler(ctx, args.phase, level=args.level, stop_at=args.stop_at)
            code = asyncio.run(sched.run())
            ctx.db.write(lambda c: c.execute("UPDATE runs SET ended_at=?, exit_code=? WHERE run_id=?",
                                             (time.time(), code, run_id)))
            ctx.db.close()
            return code
    except LockHeld as exc:
        print(f"PROBLEM: {exc}")
        return 5


def _rebuild_with_binary(args: argparse.Namespace, shim: Path) -> Any:
    args2 = argparse.Namespace(**vars(args))
    args2.set = list(getattr(args, "set", None) or []) + [f"paths.binary={shim}"]
    return _settings(args2)


def _requeue_failed(ctx: Any, phase: str) -> None:
    def fn(c: Any) -> None:
        now = time.time()
        for r in c.execute("SELECT episode_id FROM episodes WHERE status='failed' AND phase IN (?,?)", (phase, phase + "probe")).fetchall():
            c.execute("INSERT INTO events(ts, run_id, level, kind, episode_id) VALUES (?,?,?,?,?)",
                      (now, ctx.run_id, "info", "manual_requeue", r[0]))
            c.execute("UPDATE episodes SET status='queued', not_before=NULL, updated_at=? WHERE episode_id=?", (now, r[0]))
    ctx.db.write(fn)


def _cmd_status(args: argparse.Namespace) -> int:
    s = _settings(args)
    if not s.paths.db_path.exists():
        print(f"no database at {s.paths.db_path}")
        return 0
    db = dbmod.Db(s.paths.db_path)
    rows = db.read(lambda c: c.execute(
        "SELECT phase, model, status, COUNT(*) n FROM episodes GROUP BY phase, model, status ORDER BY MIN(queue_pos)").fetchall())
    print("episodes:")
    for r in rows:
        print(f"  {r['phase']:<8} {r['model']:<18} {r['status']:<8} {r['n']}")
    att = db.read(lambda c: c.execute("SELECT status, COUNT(*) n FROM attempts GROUP BY status").fetchall())
    print("attempts: " + ", ".join(f"{r['status']}={r['n']}" for r in att))
    spent = db.read(lambda c: c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM cost_ledger").fetchone()[0])
    est = db.read(lambda c: c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM cost_ledger WHERE estimated=1").fetchone()[0])
    print(f"cost: ${spent + s.budget.prior_spend_usd:.4f} of ${s.budget.cap_usd:g} (estimated part ${est:.4f})")
    for r in db.read(lambda c: c.execute("SELECT ts, level, kind FROM events WHERE level IN ('page','error','warn') "
                                         "ORDER BY id DESC LIMIT 5").fetchall()):
        print(f"event: {datetime.fromtimestamp(r['ts']).strftime('%H:%M:%S')} {r['level']} {r['kind']}")
    if s.paths.alert_path.exists():
        print("ALERT: " + s.paths.alert_path.read_text(encoding="utf-8").replace("\n", " "))
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    from wtth import audit

    s = _settings(args)
    db = dbmod.Db(s.paths.db_path)
    report = audit.audit_g0(s, db, args.run_id)
    out = s.paths.reports_dir / f"g0_{args.run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    report.write_md(out)
    print(f"audit report: {out}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    from wtth import gates

    s = _settings(args)
    db = dbmod.Db(s.paths.db_path)
    if args.which == "g1":
        out = gates.g1_report(db, args.level or "L2")
    else:
        out = gates.g2_report(db, args.phase or "g2")
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def _cmd_redact(args: argparse.Namespace) -> int:
    from wtth import redact

    s = _settings(args)
    counts = redact.build_release(s, s.paths.runs_dir, Path(args.dest))
    print(json.dumps(counts, indent=2, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to experiment.yaml")
    common.add_argument("--runs-dir", help="runs directory (default: <repo>/runs)")
    common.add_argument("--db", help="database file (must be named wtth.sqlite)")
    common.add_argument("--set", action="append", metavar="KEY=VALUE", help="config override, e.g. paths.cwd=/x")
    ap = argparse.ArgumentParser(prog="wtth", description="Who Tells the Humans? harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", parents=[common]).set_defaults(fn=_cmd_init_db)
    m = sub.add_parser("manifest", parents=[common])
    m.add_argument("--phase", default="core")
    m.add_argument("--verify", action="store_true", help="run the startup checks first")
    m.set_defaults(fn=_cmd_manifest)
    r = sub.add_parser("render", parents=[common])
    r.add_argument("--scenario", default="main")
    r.add_argument("--cell", required=True)
    r.add_argument("--seed", type=int, required=True)
    r.add_argument("--round", type=int, required=True)
    r.add_argument("--slot", type=int, required=True)
    r.add_argument("--level")
    r.add_argument("--unmask", action="store_true")
    r.set_defaults(fn=_cmd_render)
    u = sub.add_parser("run", parents=[common])
    u.add_argument("--phase", required=True, choices=["g0", "g0dry", "g1", "g1b", "g2", "g2fix", "core", "sonnet",
                                                      "mvp", "pivotA", "pivotB", "backup"])
    u.add_argument("--level")
    u.add_argument("--live", action="store_true")
    u.add_argument("--fake", metavar="BEHAVIOR.json")
    u.add_argument("--k-init", type=int)
    u.add_argument("--max-seed", action="append", metavar="CELL=N")
    u.add_argument("--stop-at", metavar="HH:MM")
    u.add_argument("--requeue-failed", action="store_true")
    u.set_defaults(fn=_cmd_run)
    sub.add_parser("status", parents=[common]).set_defaults(fn=_cmd_status)
    a = sub.add_parser("audit", parents=[common])
    a.add_argument("--run-id", required=True)
    a.set_defaults(fn=_cmd_audit)
    g = sub.add_parser("gate", parents=[common])
    g.add_argument("which", choices=["g1", "g2"])
    g.add_argument("--level")
    g.add_argument("--phase")
    g.set_defaults(fn=_cmd_gate)
    d = sub.add_parser("redact", parents=[common])
    d.add_argument("--dest", required=True)
    d.set_defaults(fn=_cmd_redact)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise
