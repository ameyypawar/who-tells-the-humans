"""python -I -m wtth.annotate {queues,export,import-labels,blind-export,blind-import,blind-decline,report,import-review,status}

Exit codes: 0 ok, 2 bad input or configuration (message names ids and line numbers only, never private text),
3 private text survived masking (the offending file is deleted). Imports do not take runs/wtth.lock.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DOWNLOADS = Path.home() / "Downloads"


def _settings_and_db(args: argparse.Namespace) -> tuple[Any, Any]:
    from wtth.config import load_settings
    from wtth.judge.db import judge_db_path, open_judge_db

    ov: dict[str, Any] = {}
    if args.runs_dir:
        ov["paths.runs_dir"] = str(Path(args.runs_dir).resolve())
    settings = load_settings(Path(args.config) if args.config else None, ov)
    if not judge_db_path(settings).exists():
        raise SystemExit("judge database not found: run `python -I -m wtth.judge extract` first")
    return settings, open_judge_db(settings)


def _seeds(settings: Any) -> Any:
    from wtth.analysis.seeds import load_analysis_seeds

    return load_analysis_seeds(settings)


def _dump(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -I -m wtth.annotate")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to experiment.yaml")
    common.add_argument("--runs-dir", help="runs directory (default: <repo>/runs)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("queues", parents=[common], help="build annotation queues")
    p.add_argument("--stage", type=int, choices=(1, 2), required=True)
    p.add_argument("--kappa", type=float, default=None, help="override the model-model kappa on escalation")
    p = sub.add_parser("export", parents=[common], help="write masked annotator batches")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--out", type=Path, default=None, help="default: runs/annotate/export")
    p.add_argument("--redo-unlabeled", action="store_true", help="include exported items that still have no label")
    p = sub.add_parser("import-labels", parents=[common], help="import Claude label files")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--annotator", required=True)
    p = sub.add_parser("blind-export", parents=[common], help="write the author-blind sample")
    p.add_argument("--out", type=Path, default=DOWNLOADS)
    p.add_argument("-n", type=int, default=100)
    p = sub.add_parser("blind-import", parents=[common])
    p.add_argument("file", type=Path)
    sub.add_parser("blind-decline", parents=[common])
    p = sub.add_parser("report", parents=[common], help="write the author review report")
    p.add_argument("--out", type=Path, default=DOWNLOADS)
    p.add_argument("--include-reviewed", action="store_true")
    p.add_argument("--queues", default=None, help="comma-separated queues, e.g. H-1,H-3,H-4,H-5")
    p.add_argument("--max-per-queue", action="append", default=[], metavar="Q=N",
                   help="slice a queue to its first N rows by draw_rank, round-robin across strata (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="count the items; write nothing")
    p = sub.add_parser("void-h5-redraw", parents=[common], help="void the duplicate H-5 draw (D-019); idempotent")
    p.add_argument("--expect", type=int, default=None, help="refuse unless exactly this many rows are voided")
    p = sub.add_parser("import-review", parents=[common], help="import the DECISION lines of a review report")
    p.add_argument("file", type=Path)
    sub.add_parser("status", parents=[common], help="queue, review and gate progress; model-model agreement")
    args = ap.parse_args(argv)

    from wtth.annotate.masking import PrivateLeak

    try:
        settings, jdb = _settings_and_db(args)
        if args.cmd == "queues":
            from wtth.annotate import agreement, queues

            seeds = _seeds(settings)
            kappa, n = agreement.kappa_h5_escalation(jdb)
            if args.kappa is not None:
                kappa = args.kappa
            counts = queues.build_queues(settings, jdb, stage=args.stage, seeds=seeds, kappa_mm_escalation=kappa,
                                         kappa_n=n)
            _dump({"inserted": counts, "kappa_h5_escalation": kappa, "n_h5_pairs": n, "stage": args.stage})
        elif args.cmd == "export":
            from wtth.annotate import export

            out = args.out or (Path(settings.paths.runs_dir) / "annotate" / "export")
            d = export.export_annotator_batches(settings, jdb, out, batch_size=args.batch_size,
                                                seed=_seeds(settings).export_order,
                                                include_exported_unlabeled=args.redo_unlabeled)
            print(d)
        elif args.cmd == "import-labels":
            from wtth.annotate import importer

            res = importer.import_label_files(settings, jdb, args.files, annotator=args.annotator)
            _dump(res)
            return 0 if not res["invalid"] else 2
        elif args.cmd == "blind-export":
            from wtth.annotate import blind

            print(blind.export_blind_sample(settings, jdb, args.out, n=args.n, seed=_seeds(settings).blind_sample))
        elif args.cmd == "blind-import":
            from wtth.annotate import blind

            _dump(blind.import_blind_labels(settings, jdb, args.file))
        elif args.cmd == "blind-decline":
            from wtth.annotate import blind

            blind.decline_blind(jdb)
            print("blind sample declined")
        elif args.cmd == "report":
            from wtth.annotate import report

            path = report.report_path(args.out)
            mpq = {}
            for item in args.max_per_queue:
                k, _, v = item.partition("=")
                mpq[k.strip()] = int(v)
            stats = report.write_review_report(settings, jdb, path, include_reviewed=args.include_reviewed,
                                               seed=_seeds(settings).export_order,
                                               queues=[x.strip() for x in args.queues.split(",")] if args.queues else None,
                                               max_per_queue=mpq or None, dry_run=args.dry_run)
            _dump({"path": None if args.dry_run else str(path), "dry_run": args.dry_run, **stats})
        elif args.cmd == "void-h5-redraw":
            from wtth.annotate import void

            _dump(void.void_h5_redraw(jdb, expect=args.expect))
        elif args.cmd == "import-review":
            from wtth.annotate import report

            _dump(report.import_review(settings, jdb, args.file))
        elif args.cmd == "status":
            from wtth.annotate import agreement, final

            src = final.JudgeDbLabelSource(jdb, strict=True)
            _dump({"gates": src.gates(), "progress": final.review_progress(jdb),
                   "agreement": agreement.agreement_summary(jdb, seeds=_seeds(settings))})
    except PrivateLeak as e:
        print(f"PRIVATE LEAK: {e}", file=sys.stderr)
        return 3
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
