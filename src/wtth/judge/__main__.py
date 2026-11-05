"""python -I -m wtth.judge {extract,plan,validate,run,gate-g1,status,recover}

Exit codes: 0 ok, 2 startup problem or bad arguments, 3 STOP, 4 budget binds or cap, 5 lock held, 130 interrupt.
Every command that calls the model or writes call, ledger or label rows holds runs/wtth.lock.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from wtth.judge.types import BudgetBinds, ExtractionError, JudgeStop

FORBIDDEN_ENV = ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to experiment.yaml")
    common.add_argument("--runs-dir", help="runs directory (default: <repo>/runs)")
    common.add_argument("--db", help="main database file (must be named wtth.sqlite)")
    common.add_argument("--set", action="append", metavar="KEY=VALUE", help="config override")
    common.add_argument("--fake", metavar="BEHAVIOR.json", help="run against the fake claude (tests only)")
    ap = argparse.ArgumentParser(prog="wtth.judge", description="Who Tells the Humans? LLM judge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", parents=[common])
    e.add_argument("--phase", required=True, choices=["core", "sonnet", "mvp"])
    e.add_argument("--prefer", choices=["core", "mvp"])
    sub.add_parser("plan", parents=[common])
    v = sub.add_parser("validate", parents=[common])
    v.add_argument("--prompt-version", default="v1", choices=["v1", "v2"])
    v.add_argument("--deviation")
    v.add_argument("--level")
    v.add_argument("-k", type=int, default=8)
    r = sub.add_parser("run", parents=[common])
    r.add_argument("--tier", required=True, choices=["J1", "J2"])
    r.add_argument("--partial", action="store_true")
    r.add_argument("--subtier", action="append")
    r.add_argument("-k", type=int, default=8)
    g = sub.add_parser("gate-g1", parents=[common])
    g.add_argument("--level", required=True)
    g.add_argument("-k", type=int, default=6)
    sub.add_parser("status", parents=[common])
    sub.add_parser("recover", parents=[common])
    return ap


def _settings(args: argparse.Namespace) -> tuple[Any, str]:
    from wtth import cli as wcli

    s = wcli._settings(args)
    mode = "fake" if args.fake else "live"
    if args.fake:
        bad = wcli._guard_fake(s)
        if bad:
            raise SystemExit("; ".join(bad))
        shim = wcli._write_shim(s, Path(args.fake))
        s = wcli._rebuild_with_binary(args, shim)
    return s, mode


def _startup(settings: Any, mode: str) -> list[str]:
    problems = [f"{v} is set in the environment (run from a plain shell: scripts/run_judge.sh)"
                for v in FORBIDDEN_ENV if v in os.environ]
    from wtth.orchestrator import manifest

    problems += manifest.verify_startup(settings, mode, "judge")
    return problems


def _locked(args: argparse.Namespace, fn) -> int:
    from wtth.orchestrator import manifest
    from wtth.orchestrator.lock import LockHeld, RunLock

    s, mode = _settings(args)
    problems = _startup(s, mode)
    if problems:
        for p in problems:
            print(f"PROBLEM: {p}")
        return 2
    from wtth.judge.runner import new_run_id

    run_id = new_run_id()
    try:
        with RunLock(s.paths.lock_path, run_id):
            s.paths.manifests_dir.mkdir(parents=True, exist_ok=True)
            man = manifest.build_manifest(s, run_id, "judge", mode)
            (s.paths.manifests_dir / f"{run_id}.json").write_text(json.dumps(man, indent=2, sort_keys=True, default=str))
            return int(fn(s, run_id) or 0)
    except LockHeld as exc:
        print(f"PROBLEM: {exc}")
        return 5


def _cmd_extract(args: argparse.Namespace) -> int:
    def go(s: Any, run_id: str) -> int:
        from wtth.analysis import seeds as seeds_mod
        from wtth.analysis import sets
        from wtth.db import Db
        from wtth.judge import items as items_mod
        from wtth.judge.db import open_judge_db

        seeds = seeds_mod.load_analysis_seeds(s)
        snap = sets.snapshot_main_db(s)
        db = Db(snap.path)
        aset = sets.analysis_set(s, db, args.phase, prefer=args.prefer)
        jdb = open_judge_db(s, create=True)
        rep = items_mod.extract(s, db, jdb, aset, seeds=seeds, snapshot=snap)
        print(f"extraction {rep.extraction_id}: {rep.n_new} new items; s_c={aset.s_c}; "
              f"empty D answers={rep.n_empty_answers}; fingerprint={aset.fingerprint[:16]}")
        for sub, n in sorted(rep.by_subtier.items()):
            print(f"  {sub}: {n}")
        return 0

    return _locked(args, go)


def _cmd_validate(args: argparse.Namespace) -> int:
    def go(s: Any, run_id: str) -> int:
        from wtth.judge import runner

        vr = runner.run_first_batch(s, prompt_version=args.prompt_version, deviation=args.deviation, k=args.k,
                                    level=args.level, run_id=run_id)
        print(f"validation {vr.prompt_version}: {vr.n_pass}/{vr.n_items} held-out items pass; "
              f"c-bar ${vr.c_bar_usd:.4f}; {'PASSED' if vr.passed else 'FAILED'}")
        for k, ok in sorted(vr.per_item.items(), key=lambda kv: int(kv[0][1:])):
            print(f"  {k}: {'pass' if ok else 'FAIL'}")
        return 0 if vr.passed else 3

    return _locked(args, go)


def _cmd_run(args: argparse.Namespace) -> int:
    def go(s: Any, run_id: str) -> int:
        from wtth.judge import runner

        res = runner.run_tier(s, args.tier, subtiers=args.subtier, partial=args.partial, k=args.k, run_id=run_id)
        for r in res:
            print(f"batch {r.batch_id[:8]}: n={r.n_items} valid={r.n_valid} invalid={r.n_invalid} "
                  f"refused={r.n_refused} technical={r.n_technical} cost=${r.cost_usd:.4f} stopped={r.stopped}")
        return 0

    return _locked(args, go)


def _cmd_gate(args: argparse.Namespace) -> int:
    def go(s: Any, run_id: str) -> int:
        from wtth.judge import gate

        out = gate.g1_fallback(s, args.level, k=args.k, run_id=run_id)
        print(json.dumps({k: v for k, v in out.items() if k != "report"}, indent=2, sort_keys=True, default=str))
        return 0

    return _locked(args, go)


def _cmd_recover(args: argparse.Namespace) -> int:
    def go(s: Any, run_id: str) -> int:
        from wtth.judge import runner

        print(f"closed {runner.recover(s)} open call rows")
        return 0

    return _locked(args, go)


def _cmd_plan(args: argparse.Namespace) -> int:
    from wtth.db import Db
    from wtth.judge import items as items_mod
    from wtth.judge.budget import JudgeBudget
    from wtth.judge.db import judge_db_path, open_judge_db
    from wtth.judge.runner import FACTOR_J2, J1_ORDER, RESERVE_FACTOR_J1

    s, _mode = _settings(args)
    if not judge_db_path(s).exists():
        print("no judge database yet: run `extract` first")
        return 0
    jdb = open_judge_db(s)
    main = Db(s.paths.db_path) if s.paths.db_path.exists() else None
    b = JudgeBudget(s, main, jdb)
    try:
        from wtth.judge.runner import _seeds
        j2 = tuple(_seeds(s).j2_order)
    except Exception:  # noqa: BLE001 - the plan is useful without the seeds file
        j2 = tuple(x for x in items_mod.SUBTIERS if x.startswith("J2"))
    print(f"spent ${b.global_spent():.4f}; remaining ${b.remaining():.4f} (cap ${s.budget.cap_usd:g}, margin $0.50)")
    print(f"{'subtier':<8}{'N':>7}{'c-bar':>9}{'need':>10}{'remaining':>11}{'fits':>6}{'n_fit':>7}")
    for sub, f in [(x, RESERVE_FACTOR_J1) for x in J1_ORDER] + [(x, FACTOR_J2) for x in j2]:
        its = items_mod.load_items(jdb, subtier=sub, unsettled_only=True)
        p = b.project(its, factor=f)
        print(f"{sub:<8}{p.n_items:>7}{p.c_bar:>9.4f}{p.need_usd:>10.2f}{p.remaining_usd:>11.2f}"
              f"{str(p.fits):>6}{p.n_fit:>7}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from wtth.judge import items as items_mod
    from wtth.judge.budget import JudgeBudget
    from wtth.judge.db import active_version, get_meta, judge_db_path, open_judge_db

    s, _mode = _settings(args)
    if not judge_db_path(s).exists():
        print("no judge database yet")
        return 0
    jdb = open_judge_db(s)
    ver = active_version(jdb)
    print(f"prompt {ver}; validation_passed={get_meta(jdb, 'validation_passed')} "
          f"level={get_meta(jdb, 'validation_level')}")
    rows = jdb.read(lambda c: c.execute(
        "SELECT i.subtier, COUNT(*) n, SUM(l.status='valid') v, SUM(l.status='invalid') x, SUM(l.status='refused') r "
        "FROM items i LEFT JOIN judge_labels l ON l.item_id=i.item_id AND l.prompt_version=? GROUP BY i.subtier",
        (ver,)).fetchall())
    by = {r["subtier"]: r for r in rows}
    for sub in items_mod.SUBTIERS:
        if sub in by:
            r = by[sub]
            print(f"  {sub:<4} items={r['n']:<6} valid={r['v'] or 0:<6} invalid={r['x'] or 0:<4} refused={r['r'] or 0}")
    main = None
    try:
        from wtth.db import Db
        main = Db(s.paths.db_path) if s.paths.db_path.exists() else None
    except Exception:  # noqa: BLE001
        pass
    b = JudgeBudget(s, main, jdb)
    sp = jdb.read(lambda c: c.execute("SELECT usd, n_calls, n_estimated FROM v_judge_spend").fetchone())
    print(f"judge spend ${sp['usd']:.4f} over {sp['n_calls']} calls ({sp['n_estimated']} estimated); "
          f"global ${b.global_spent():.4f} of ${s.budget.cap_usd:g}")
    return 0


_CMDS = {"extract": _cmd_extract, "plan": _cmd_plan, "validate": _cmd_validate, "run": _cmd_run,
         "gate-g1": _cmd_gate, "status": _cmd_status, "recover": _cmd_recover}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(_CMDS[args.cmd](args))
    except JudgeStop as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 3
    except BudgetBinds as exc:
        print(f"BUDGET: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise
    except (ExtractionError, ValueError, FileNotFoundError, ImportError) as exc:
        print(f"PROBLEM: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
