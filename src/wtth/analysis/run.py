"""End-to-end analysis: snapshot -> sets -> outcomes -> statistics -> NumberBook -> files (plan 47 2.16, 3.3).

strict:      needs the completeness gates of the label source (H-1 and H-3e reviewed); writes macros.tex.
provisional: any label source; writes only macros.provisional.tex (with \\wtthStatus{PROVISIONAL}).
"""
from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wtth.analysis import hypotheses, register, secondary, text
from wtth.analysis.numbers import MODEL_TOKENS, NumberBook, cell_token, rate_prefix
from wtth.analysis.outcomes import LabelsIncomplete, episode_outcomes, witness_outcomes
from wtth.analysis.power import icc1, pilot_p1, power_table
from wtth.analysis.seeds import AnalysisSeeds, load_analysis_seeds
from wtth.analysis.sets import AnalysisSet, AnalysisSetError, analysis_set, model_short, snapshot_main_db
from wtth.db import Db
from wtth.judge.types import DictLabelSource, LabelSource


class PrivateLeakError(RuntimeError):
    """An exported file contains a private string. The message names ids only, never the text."""


@dataclass(frozen=True, slots=True)
class RunOutputs:
    out_dir: Path
    status: str
    numbers_json: Path
    macros: Path
    figures: list[Any]
    tables: list[Any]


def model_label(model_id: str) -> str:
    m = re.match(r"claude-([a-z]+)-(\d+)-(\d+)$", model_id)
    return f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)}" if m else model_id


def default_label_source(settings: Any, *, strict: bool) -> LabelSource:
    """The judge database as a LabelSource (WP6-B's JudgeDbLabelSource); an empty source if there is no judge database
    and the run is provisional."""
    jpath = Path(settings.paths.runs_dir) / "judge" / "judge.sqlite"
    if not jpath.exists():
        if strict:
            raise AnalysisSetError(f"strict analysis needs the judge database {jpath}")
        return DictLabelSource({}, gates={"p1_final": False, "p2prime_final": False})
    from wtth.annotate.final import JudgeDbLabelSource
    from wtth.judge.db import open_judge_db

    return JudgeDbLabelSource(open_judge_db(settings), strict=strict)


def _phases_present(db: Db) -> dict[str, int]:
    rows = db.read(lambda c: c.execute(
        "SELECT phase, COUNT(*) FROM episodes WHERE probe=0 AND seed<9000 AND phase IN ('core','mvp','sonnet') "
        "GROUP BY phase").fetchall())
    return {r[0]: int(r[1]) for r in rows}


def _gate_rows(db: Db) -> list[dict[str, Any]]:
    rows = db.read(lambda c: c.execute(
        "SELECT id, ts, gate, run_id, phase, level, decision, numbers_json, detail_json FROM gate_log ORDER BY id").fetchall())
    out = []
    for r in rows:
        d = dict(r)
        d["numbers"] = json.loads(d["numbers_json"]) if d["numbers_json"] else {}
        out.append(d)
    return out


def _check_fingerprints(settings: Any, asets: Sequence[AnalysisSet], *, strict: bool, notes: list[str]) -> None:
    jpath = Path(settings.paths.runs_dir) / "judge" / "judge.sqlite"
    if not jpath.exists():
        return
    conn = sqlite3.connect(f"file:{jpath}?mode=ro", uri=True)
    try:
        try:
            rows = conn.execute("SELECT phase, analysis_set_sha256 FROM extractions").fetchall()
        except sqlite3.Error:
            return
    finally:
        conn.close()
    for a in asets:
        fps = {r[1] for r in rows if r[0] == a.phase}
        if fps and a.fingerprint not in fps:
            msg = (f"labels for phase {a.phase} were extracted from a different analysis set than the one in this "
                   f"snapshot (fingerprint {a.fingerprint[:12]}); re-extract")
            if strict:
                raise AnalysisSetError(msg)
            notes.append(msg)


def _cost(settings: Any, db: Db) -> dict[str, Any]:
    rows = [dict(r) for r in db.read(lambda c: c.execute("SELECT * FROM v_cost ORDER BY phase, model").fetchall())]
    main = sum(float(r["total_usd"] or 0.0) for r in rows)
    judge_rows: list[dict[str, Any]] = []
    jpath = Path(settings.paths.runs_dir) / "judge" / "judge.sqlite"
    if jpath.exists():
        conn = sqlite3.connect(f"file:{jpath}?mode=ro", uri=True)
        try:
            judge_rows = [dict(zip(("tier", "item_type", "n_calls", "usd"), r)) for r in conn.execute(
                "SELECT tier, item_type, COUNT(*), COALESCE(SUM(cost_usd),0) FROM judge_ledger GROUP BY tier, item_type")]
        except sqlite3.Error:
            judge_rows = []
        finally:
            conn.close()
    judge = sum(r["usd"] for r in judge_rows)
    return {"by_phase_model": rows, "judge_by_tier": judge_rows, "main_usd": main, "judge_usd": judge,
            "prior_usd": float(settings.budget.prior_spend_usd), "total_usd": main + judge + float(settings.budget.prior_spend_usd)}


def _write_figures_tables(out_dir: Path, book: NumberBook, *, label: str, hyp: Any, sec: Any, desc: Any, p1r: Any,
                          gate_rows: Any, cost: Any, judge_pack: Any, strict: bool, notes: list[str]) -> tuple[list, list]:
    try:
        from wtth.analysis import figures as F
        from wtth.analysis import tables as T
    except ImportError:
        notes.append("figures and tables are not available (wtth.analysis.figures / tables not importable)")
        return [], []
    fdir, tdir = out_dir / "figures", out_dir / "tables"
    fdir.mkdir(parents=True, exist_ok=True)
    tdir.mkdir(parents=True, exist_ok=True)
    figs = [F.fig1a(sec.p2, sec.p2prime, fdir, model_label=label), F.fig1b(desc.frontier, fdir, model_label=label),
            F.fig_p1_by_cell(p1r, hyp, fdir), F.fig_km(list(sec.km), fdir)]
    tabs: list[Any] = [T.table_hypotheses(hyp, p1r, book, tdir), T.table_p2(sec.p2, sec.p2prime, sec.diffs, book, tdir),
                       T.table_counts(sec.counts, book, tdir)]
    tabs += list(T.table_secondary(sec, book, tdir))
    tabs.append(T.table_gate_log(gate_rows, book, tdir))
    tabs.append(T.table_cost(cost, book, tdir))
    if judge_pack is not None:
        tabs.append(T.table_judge(judge_pack[0], judge_pack[1], book, tdir))
        tabs.append(T.table_confusion(judge_pack[1], book, tdir))
    return figs, tabs


def _judge_pack(settings: Any, seeds: AnalysisSeeds, *, strict: bool, notes: list[str]) -> Any:
    jpath = Path(settings.paths.runs_dir) / "judge" / "judge.sqlite"
    if not jpath.exists():
        notes.append("judge validation and agreement tables skipped: no judge database")
        return None
    try:
        from wtth.annotate.agreement import agreement_summary
        from wtth.judge.db import open_judge_db

        jdb = open_judge_db(settings)
        rows = [dict(r) for r in jdb.read(lambda c: c.execute("SELECT v.* FROM validations v JOIN batches b ON b.batch_id=v.batch_id WHERE b.kind='first' ORDER BY v.ts").fetchall())]
        return (rows[-1] if rows else None), agreement_summary(jdb, seeds=seeds)
    except Exception as exc:     # noqa: BLE001 - provisional runs go on without the table
        if strict:
            raise
        notes.append(f"judge validation and agreement tables skipped: {type(exc).__name__}: {exc}")
        return None


def _scan_and_copy(files: Sequence[Path], dest: Path, private: Mapping[str, str], root: Path) -> list[Path]:
    from wtth.redact import _Redactor

    red = _Redactor(dict(private))
    for f in files:
        hits = red.residual(f.read_bytes())
        if hits:
            raise PrivateLeakError(f"private string ids {sorted(hits)} found in {f.name}; nothing was exported")
    out = []
    for f in files:
        target = dest / f.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.read_bytes())
        out.append(target)
    return out


def run_analysis(settings: Any, *, strict: bool, labels: LabelSource | None = None, out_root: Path | None = None,
                 export_paper: Path | None = None, seeds: AnalysisSeeds | None = None,
                 private: Mapping[str, str] | None = None, prefer: str | None = None,
                 phases: Sequence[str] | None = None) -> RunOutputs:
    seeds = seeds or load_analysis_seeds(settings)
    status = "FINAL" if strict else "PROVISIONAL"
    notes: list[str] = []
    snap = snapshot_main_db(settings)
    db = Db(snap.path)
    if labels is None:
        labels = default_label_source(settings, strict=strict)
    if strict:
        gates = labels.gates()
        failing = sorted(k for k, v in gates.items() if not v)
        if failing or not gates:
            raise LabelsIncomplete([f"gate:{k}" for k in (failing or ["p1_final", "p2prime_final"])])
    present = _phases_present(db)
    if phases is None:
        main_phase = "core" if present.get("core") else "mvp" if present.get("mvp") else None
        if main_phase is None:
            raise AnalysisSetError("no confirmatory Haiku episodes (core or mvp) in the snapshot")
        phases = [main_phase] + (["sonnet"] if present.get("sonnet") else [])
    asets = [analysis_set(settings, db, p, prefer=prefer) for p in phases]
    _check_fingerprints(settings, asets, strict=strict, notes=notes)
    gate_rows = _gate_rows(db)
    book = NumberBook()
    book.add("ProtocolLevel", 95, "plain", source="PREREG 16: 95 percent intervals")
    book.add("ProtocolMargin", 15, "plain", source="PREREG 11: null margin in percentage points")
    book.add("BootstrapB", seeds.bootstrap_B, "count", source="configs/analysis.yaml")
    book.add("SnapshotEpisodes", sum(present.values()), "count", source=f"snapshot {snap.sha256[:12]}")
    summary: dict[str, Any] = {"status": status, "snapshot": {"path": str(snap.path), "sha256": snap.sha256},
                               "gates": dict(labels.gates()), "phases": {}}
    hyp = sec = desc = sens = p1r = None
    label_h = ""
    sonnet_sentence = None
    for aset in asets:
        w = witness_outcomes(db, aset, labels, strict=strict)
        e = episode_outcomes(w, db, aset, labels, strict=strict)
        model = w[0].model if w else model_short(aset.model)
        pending = sum(1 for x in w if x.p1_pending)
        unresolved = sum(x.n_posts_unresolved for x in e)
        summary["phases"][aset.phase] = {
            "model": aset.model, "s_c": aset.s_c, "n_episodes": len(aset.episodes), "n_above": len(aset.above),
            "failed_permanent": [list(x) for x in aset.failed_permanent], "missing": [list(x) for x in aset.missing],
            "fingerprint": aset.fingerprint, "cut": dict(aset.cut), "n_witnesses": len(w),
            "n_p1_pending_witnesses": pending, "n_p2_posts_unresolved": unresolved}
        if pending or unresolved:
            notes.append(f"{aset.phase}: {pending} witness(es) with an unresolved note and {unresolved} unresolved "
                         f"post(s) in P2 episodes; provisional values treat them as no qualifying note / bounds")
        if model == "haiku":
            gate_for_h = gate_rows
            hyp = hypotheses.run_hypotheses(w, e, seeds=seeds, gate_log=gate_for_h)
            sec = secondary.secondary(w, e, db, aset, seeds=seeds)
            desc = secondary.descriptive(w, e, seeds=seeds)
            sens = secondary.sensitivity(w, e, db, aset, seeds=seeds)
            p1r = secondary.p1_rates(w, seeds=seeds)
            label_h = model_label(aset.model)
            # power, recomputed at F
            n_by_cell = {}
            for ep in aset.episodes:
                n_by_cell[ep.cell] = n_by_cell.get(ep.cell, 0) + 1
            power = None
            p_pilot = pilot_p1(gate_rows, settings.frozen_level)
            pooled, per = icc1(hypotheses.witness_cells(w))
            need = ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT", "FORGED")
            if all(n_by_cell.get(c) for c in need):
                p_use = p_pilot if p_pilot is not None else 0.5
                power = dict(power_table(p_use, 0.3, n_by_cell))
                power["p_from_pilot"] = p_pilot is not None
                summary["power_rho_0.3"] = power
                if pooled == pooled and pooled >= 0:
                    summary["power_rho_observed"] = dict(power_table(p_use, pooled, n_by_cell))
                    summary["power_rho_observed"]["rho_observed"] = pooled
            register.register_model(book, "haiku", aset=aset, w=w, e=e, p1r=p1r, hyp=hyp, sec=sec, desc=desc, sens=sens,
                                    power=power)
            sent = text.sentences(hyp, book, model_label=label_h)
            for k, v in sent.items():
                book.add(MODEL_TOKENS["haiku"] + k + "Sentence", v, "latex", source=f"template for {k}")
            for c in desc.contrasts:
                book.add(MODEL_TOKENS["haiku"] + "Desc" + register.contrast_key(c.name) + "Sentence",
                         text.descriptive_sentence(c, book, label=c.name), "latex", source=f"descriptive {c.name}")
            summary["phases"][aset.phase]["hypotheses"] = {
                "stopped_at": hyp.stopped_at, "null_rule": hyp.null_rule, "tost": list(hyp.tost)}
        else:
            s = secondary.sonnet(w, e, seeds=seeds)
            n_cell = max((len(v) for v in aset.planned.values()), default=0)
            register.register_sonnet(book, s, aset=aset, episodes_per_cell=n_cell)
            book.add("SonnetSentence", text.sonnet_sentence(s, book, model_label=model_label(aset.model),
                                                            episodes_per_cell=n_cell), "latex",
                     source="template for Sonnet (exploratory)")
        if aset.above:
            above = dataclasses.replace(aset, episodes=aset.above, above=())
            wa = witness_outcomes(db, above, labels, strict=False)
            M = MODEL_TOKENS.get(model, model)
            book.add(M + "AboveEpisodesTotal", len(aset.above), "count", source="done episodes above s_c (exploratory)")
            by: dict[str, list] = {}
            for x in wa:
                by.setdefault(x.cell, []).append(x)
            for c, ws in by.items():
                r = secondary.known_rate(model, c, "p1", ws, lambda x: x.p1)
                book.add_rate(M + "Above" + rate_prefix(model, "p1", c)[len(M):], r)
    cost = _cost(settings, db)
    book.add("CostMainUsd", cost["main_usd"], "usd", source="cost_ledger of the main database")
    book.add("CostJudgeUsd", cost["judge_usd"], "usd", source="judge_ledger")
    book.add("CostTotalUsd", cost["total_usd"], "usd", source="prior spend + main ledger + judge ledger")
    judge_pack = _judge_pack(settings, seeds, strict=strict, notes=notes)
    if judge_pack is not None:
        register.register_judge(book, judge_pack[0], judge_pack[1])

    stamp = datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y%m%dT%H%M%S")
    root = Path(out_root) if out_root is not None else Path(settings.paths.runs_dir) / "analysis" / "out"
    out_dir = root / f"{stamp}-{status.lower()}"
    n = 1
    while out_dir.exists():
        out_dir = root / f"{stamp}-{status.lower()}-{n}"
        n += 1
    out_dir.mkdir(parents=True)
    figs, tabs = ([], [])
    if hyp is not None:
        figs, tabs = _write_figures_tables(out_dir, book, label=label_h, hyp=hyp, sec=sec, desc=desc, p1r=p1r,
                                           gate_rows=gate_rows, cost=cost, judge_pack=judge_pack, strict=strict,
                                           notes=notes)
    numbers_json = out_dir / "numbers.json"
    macros = out_dir / ("macros.tex" if strict else "macros.provisional.tex")
    book.write_json(numbers_json)
    book.write_macros(macros, status=status)
    summary["notes"] = notes
    summary["n_numbers"] = len(book)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    if export_paper is not None:
        if private is None:
            from wtth.prereg import load_private

            private = load_private(settings)
        files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.name != "summary.json")
        _scan_and_copy(files, Path(export_paper), private, out_dir)
    return RunOutputs(out_dir, status, numbers_json, macros, figs, tabs)
