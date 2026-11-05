"""python -I -m wtth.analysis {set,run,power}  (plan 47 3.3)

Exit codes: 0 ok, 2 startup / configuration (missing configs/analysis.yaml, bad arguments), 3 the analysis cannot
proceed (analysis set needs a decision, strict labels incomplete, gates not met, private text in an export).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from wtth.analysis.seeds import AnalysisConfigError, MissingAnalysisConfig

EXIT_OK, EXIT_STARTUP, EXIT_STOP = 0, 2, 3


def _settings(args: argparse.Namespace) -> Any:
    from wtth.config import load_settings

    ov: dict[str, Any] = {}
    if args.runs_dir:
        ov["paths.runs_dir"] = str(Path(args.runs_dir).resolve())
    for item in args.set or []:
        k, _, v = item.partition("=")
        ov[k] = yaml.safe_load(v)
    return load_settings(Path(args.config) if args.config else None, ov)


def cmd_set(args: argparse.Namespace) -> int:
    from wtth.analysis.sets import analysis_set, snapshot_main_db
    from wtth.db import Db

    settings = _settings(args)
    snap = snapshot_main_db(settings)
    db = Db(snap.path)
    print(f"snapshot {snap.path} sha256={snap.sha256}")
    for phase in args.phase:
        a = analysis_set(settings, db, phase, prefer=args.prefer)
        print(f"[{phase}] model={a.model} s_c={a.s_c} episodes={len(a.episodes)} above={len(a.above)} "
              f"failed_permanent={list(a.failed_permanent)} missing={len(a.missing)} cut={dict(a.cut)}")
        print(f"[{phase}] planned: " + ", ".join(f"{c}:{len(s)}" for c, s in sorted(a.planned.items())))
        if a.missing:
            print(f"[{phase}] missing (first 10): {list(a.missing)[:10]}")
        print(f"[{phase}] fingerprint {a.fingerprint}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    from wtth.analysis.outcomes import LabelsIncomplete
    from wtth.analysis.run import PrivateLeakError, run_analysis
    from wtth.analysis.seeds import load_analysis_seeds
    from wtth.analysis.sets import AnalysisSetError

    settings = _settings(args)
    seeds = load_analysis_seeds(settings, Path(args.analysis_yaml) if args.analysis_yaml else None)
    try:
        out = run_analysis(settings, strict=bool(args.strict), out_root=Path(args.out) if args.out else None,
                           export_paper=Path(args.export_paper) if args.export_paper else None, seeds=seeds,
                           prefer=args.prefer, phases=args.phase or None)
    except (LabelsIncomplete, AnalysisSetError, PrivateLeakError) as exc:
        print(f"STOP: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_STOP
    print(json.dumps({"status": out.status, "out_dir": str(out.out_dir), "numbers_json": str(out.numbers_json),
                      "macros": str(out.macros), "n_figures": len(out.figures), "n_tables": len(out.tables)}, indent=1))
    return EXIT_OK


def cmd_power(args: argparse.Namespace) -> int:
    from wtth.analysis import hypotheses
    from wtth.analysis.outcomes import witness_outcomes
    from wtth.analysis.power import icc1, pilot_p1, power_table
    from wtth.analysis.run import _gate_rows, default_label_source
    from wtth.analysis.sets import analysis_set, snapshot_main_db
    from wtth.db import Db

    settings = _settings(args)
    snap = snapshot_main_db(settings)
    db = Db(snap.path)
    a = analysis_set(settings, db, args.phase, prefer=args.prefer)
    n_by_cell: dict[str, int] = {}
    for ep in a.episodes:
        n_by_cell[ep.cell] = n_by_cell.get(ep.cell, 0) + 1
    p = pilot_p1(_gate_rows(db), settings.frozen_level)
    out: dict[str, Any] = {"phase": args.phase, "n_by_cell": n_by_cell, "p_pilot": p}
    p_use = p if p is not None else 0.5
    out["rho_0.3"] = power_table(p_use, 0.3, n_by_cell)
    w = witness_outcomes(db, a, default_label_source(settings, strict=False), strict=False)
    pooled, _ = icc1(hypotheses.witness_cells(w))
    out["icc_pooled_observed"] = None if pooled != pooled else pooled
    if pooled == pooled and pooled >= 0:
        out["rho_observed"] = power_table(p_use, pooled, n_by_cell)
    print(json.dumps(out, indent=1, default=str))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -I -m wtth.analysis")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to experiment.yaml")
    common.add_argument("--runs-dir", help="runs directory (default: <repo>/runs)")
    common.add_argument("--set", action="append", metavar="KEY=VALUE", help="config override")
    common.add_argument("--prefer", choices=["core", "mvp"], help="which of core / mvp wins if both have done episodes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", parents=[common], help="print the analysis set (s_c, failed_permanent, missing, above)")
    s.add_argument("--phase", action="append", choices=["core", "mvp", "sonnet"], required=True)
    r = sub.add_parser("run", parents=[common], help="run the analysis and write numbers.json and the macros")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--strict", action="store_true", help="needs the completeness gates; writes macros.tex")
    g.add_argument("--provisional", action="store_true", help="any labels; writes macros.provisional.tex only")
    r.add_argument("--phase", action="append", choices=["core", "mvp", "sonnet"])
    r.add_argument("--out", help="output root (default runs/analysis/out)")
    r.add_argument("--export-paper", help="copy the outputs here after the residual scan, e.g. paper/generated")
    r.add_argument("--analysis-yaml", help="path to analysis.yaml (default configs/analysis.yaml)")
    p = sub.add_parser("power", parents=[common], help="recompute the power table at F")
    p.add_argument("--phase", default="core", choices=["core", "mvp"])
    args = ap.parse_args(argv)
    try:
        return {"set": cmd_set, "run": cmd_run, "power": cmd_power}[args.cmd](args)
    except (MissingAnalysisConfig, AnalysisConfigError) as exc:
        print(f"startup error: {exc}", file=sys.stderr)
        return EXIT_STARTUP
    except FileNotFoundError as exc:
        print(f"startup error: {exc}", file=sys.stderr)
        return EXIT_STARTUP


if __name__ == "__main__":
    sys.exit(main())
