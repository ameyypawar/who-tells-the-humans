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

from wtth.analysis import exp1, final_review, humanread, hypotheses, posthoc, register, secondary, text
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


def _hqa_keys(settings: Any) -> set | None:
    """Reviewed H-QA rows: ann_queue rows whose reasons_json contains "H-QA" and that have a review."""
    jpath = Path(settings.paths.runs_dir) / "judge" / "judge.sqlite"
    if not jpath.exists():
        return None
    conn = sqlite3.connect(f"file:{jpath}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT i.source_id FROM ann_queue q JOIN items i ON i.item_id=q.item_id JOIN v_review_latest r "
            "ON r.ann_id=q.ann_id WHERE i.source_table='notes' AND q.reasons_json LIKE '%\"H-QA\"%' "
            "AND q.ann_id NOT IN (SELECT ann_id FROM ann_queue_void)").fetchall()
    except sqlite3.OperationalError:       # a database from before D-019 has no ann_queue_void: nothing voided
        try:
            rows = conn.execute(
                "SELECT i.source_id FROM ann_queue q JOIN items i ON i.item_id=q.item_id JOIN v_review_latest r "
                "ON r.ann_id=q.ann_id WHERE i.source_table='notes' AND q.reasons_json LIKE '%\"H-QA\"%'").fetchall()
        except sqlite3.Error:
            return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {("notes", str(r[0])) for r in rows}


EXP1_PHASES = ("exp1", "exp1probe")      # the D-022 follow-up and its integrity probes (D-023)


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    """judge_ledger of one judge database by tier [{tier, n_calls, usd, n_estimated}]. Read-only first; a WAL database
    whose -shm cannot be created read-only is read through a plain connection (SELECT only). A missing ledger table is
    no spend; any other failure raises, so spend is never dropped silently."""
    def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "judge_ledger" not in have:
            return []
        cols = {r[1] for r in conn.execute("PRAGMA table_info(judge_ledger)")}
        tier = "tier" if "tier" in cols else "'all'"
        est = "COALESCE(SUM(estimated),0)" if "estimated" in cols else "0"
        return [dict(zip(("tier", "n_calls", "usd", "n_estimated"), r)) for r in conn.execute(
            f"SELECT {tier}, COUNT(*), COALESCE(SUM(cost_usd),0), {est} FROM judge_ledger GROUP BY 1 ORDER BY 1")]

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return read(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        conn = sqlite3.connect(str(path))
        try:
            return read(conn)
        finally:
            conn.close()


def _cost(settings: Any, db: Db) -> dict[str, Any]:
    """Keys as tables.table_cost reads them: phase_model [{phase, model, n_calls, usd}], judge_tier [{tier, n_calls,
    usd, n_estimated}] (the confirmatory judge database, runs/judge) and judge_other [{db, tier, ...}] (every other
    runs/judge*/judge.sqlite, e.g. judge_exp1). judge_usd sums every judge database (D-023 item 4); exp1 splits out the
    D-022 follow-up (run, probes, its judge)."""
    rows = [{"phase": r["phase"], "model": r["model"], "n_calls": int(r["n_turns"]), "usd": float(r["total_usd"] or 0.0),
             "n_estimated": int(r["n_estimated"] or 0)}
            for r in db.read(lambda c: c.execute("SELECT * FROM v_cost ORDER BY phase, model").fetchall())]
    main = sum(r["usd"] for r in rows)
    runs = Path(settings.paths.runs_dir)
    core_path = runs / "judge" / "judge.sqlite"
    judge_rows = _ledger_rows(core_path) if core_path.exists() else []
    other: list[dict[str, Any]] = []
    for path in sorted(runs.glob("judge*/judge.sqlite")):
        if path == core_path:
            continue
        other += [{"db": path.parent.name, **r} for r in _ledger_rows(path)]
    judge_core = sum(float(r["usd"]) for r in judge_rows)
    judge = judge_core + sum(float(r["usd"]) for r in other)
    prior = float(settings.budget.prior_spend_usd)
    x_run = sum(r["usd"] for r in rows if r["phase"] == "exp1")
    x_probe = sum(r["usd"] for r in rows if r["phase"] == "exp1probe")
    x_judge = sum(float(r["usd"]) for r in other if r["db"] == "judge_exp1")
    total = main + judge + prior
    return {"phase_model": rows, "judge_tier": judge_rows, "judge_other": other, "main_usd": main, "judge_usd": judge,
            "judge_core_usd": judge_core, "prior_usd": prior, "total_usd": total,
            "exp1": {"run_usd": x_run, "probe_usd": x_probe, "judge_usd": x_judge,
                     "total_usd": x_run + x_probe + x_judge},
            "before_exp1_usd": total - (x_run + x_probe + x_judge)}


def _judge_counts(settings: Any) -> dict[str, Any] | None:
    """Judge mechanics numbers (F-20 mismatches, invalid / refused / re-asks, sub-tier coverage), read-only."""
    jpath = Path(settings.paths.runs_dir) / "judge" / "judge.sqlite"
    if not jpath.exists():
        return None
    conn = sqlite3.connect(f"file:{jpath}?mode=ro", uri=True)
    try:
        ver = conn.execute("SELECT value FROM jmeta WHERE key='active_prompt_version'").fetchone()
        ver = ver[0] if ver else "v1"
        st = {s: n for s, n in conn.execute("SELECT status, COUNT(*) FROM judge_labels WHERE prompt_version=? "
                                            "GROUP BY status", (ver,))}
        mism = conn.execute("SELECT COALESCE(SUM(escalation_mismatch),0), COUNT(escalation_returned) FROM judge_labels "
                            "WHERE prompt_version=?", (ver,)).fetchone()
        reasks = conn.execute("SELECT COUNT(*) FROM judge_labels WHERE prompt_version=? AND n_asks>1", (ver,)).fetchone()[0]
        cov = [{"subtier": a, "n_items": int(b), "n_judged": int(c)} for a, b, c in conn.execute(
            "SELECT i.subtier, COUNT(*), COUNT(l.item_id) FROM items i LEFT JOIN judge_labels l ON l.item_id=i.item_id "
            "AND l.prompt_version=? AND l.status='valid' WHERE i.tier IN ('J1','J2') GROUP BY i.subtier ORDER BY i.subtier",
            (ver,))]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {"valid": st.get("valid", 0), "invalid": st.get("invalid", 0), "refused": st.get("refused", 0),
            "reasks": int(reasks), "esc_mismatch": int(mism[0]), "esc_returned": int(mism[1]), "subtiers": cov}


def _fig1a_rows(p1r: Sequence[Any], son_cluster: Sequence[Any], x1: Any,
                x1_cluster: Mapping[str, Any]) -> tuple[list[Any], list[Any], list[Any]]:
    """(Haiku rows, Sonnet rows, follow-up FigRows) of Fig. 1a, every P1 with a shift-level cluster-bootstrap interval
    (research/68 A6): the Haiku cells from p1_rates (the intervals of Appendix D), the Sonnet cells and the follow-up
    arms from final_review.cluster_rates. An arm without a cluster rate keeps its Wilson rate (flagged by
    FigOneAWilsonRowsK)."""
    from wtth.analysis.figures import FigRow

    follow = []
    if x1 is not None and x1.status == exp1.COMPLETE:
        follow = [FigRow(a.arm.label, a.arm.model, x1_cluster.get(a.arm.token) or a.p1, a.p2) for a in x1.arms]
    return list(p1r), list(son_cluster), follow


def _write_figures_tables(out_dir: Path, book: NumberBook, *, label: str, hyp: Any, sec: Any, desc: Any, p1r: Any,
                          gate_rows: Any, cost: Any, judge_pack: Any, strict: bool, notes: list[str],
                          timing: Any = None, son: Any = None, son_label: str = "", fig1a_rows: Any = None
                          ) -> tuple[list, list]:
    try:
        from wtth.analysis import figures as F
        from wtth.analysis import tables as T
    except ImportError:
        notes.append("figures and tables are not available (wtth.analysis.figures / tables not importable)")
        return [], []
    fdir, tdir = out_dir / "figures", out_dir / "tables"
    fdir.mkdir(parents=True, exist_ok=True)
    tdir.mkdir(parents=True, exist_ok=True)
    h_rows, s_rows, follow = fig1a_rows if fig1a_rows is not None else (list(p1r), [], [])
    figs = [F.fig1a(h_rows, sec.p2, fdir, model_label=label, sonnet_p1=s_rows,
                    sonnet_p2=list(son.p2) if son is not None else (), sonnet_label=son_label or "Sonnet",
                    followup=follow)]
    if timing is not None:
        figs.append(F.fig1b(timing, fdir, model_label=label))
    figs += [F.fig_frontier(desc.frontier, fdir, model_label=label), F.fig_p1_by_cell(p1r, hyp, fdir),
             F.fig_km(list(sec.km), fdir)]
    # the Decision cells read the ...VerdictText macros, so Appendix D's table and Table 2 never disagree (A3)
    decisions = {h: book.value(f"Haiku{t}VerdictText") for h, t in (("H1", "HOne"), ("H2", "HTwo"), ("H3", "HThree"))
                 if f"Haiku{t}VerdictText" in book}
    tabs: list[Any] = [T.table_hypotheses(hyp, p1r, book, tdir, decisions=decisions),
                       T.table_p2(sec.p2, sec.p2prime, sec.diffs, book, tdir), T.table_counts(sec.counts, book, tdir)]
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
                 phases: Sequence[str] | None = None, human_read_dir: Path | None = None,
                 exp1_labels: LabelSource | None = None) -> RunOutputs:
    """human_read_dir: the author read (D-021 item 3), default annotations/human_read next to the runs directory
    (the repository root for the default runs directory). exp1_labels: labels of the D-022 follow-up, default the
    exp1 judge database (exp1.label_source)."""
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
    register.register_snapshot(book, snap.sha256)
    register.register_gates(book, gate_rows)
    runs_rows = [dict(r) for r in db.read(lambda c: c.execute("SELECT * FROM runs ORDER BY started_at").fetchall())]
    hpath = Path(settings.paths.runs_dir) / "health.log"
    health = hpath.read_text(encoding="utf-8", errors="replace").splitlines() if hpath.exists() else []
    register.register_ops(book, runs_rows, health, phase="core" if present.get("core") else "mvp")
    register.register_turn_integrity(book, final_review.turn_integrity(db))      # research/68 A10
    book.add("SnapshotEpisodes", sum(present.values()), "count", source=f"snapshot {snap.sha256[:12]}")
    summary: dict[str, Any] = {"status": status, "snapshot": {"path": str(snap.path), "sha256": snap.sha256},
                               "gates": dict(labels.gates()), "phases": {}}
    hyp = sec = desc = sens = p1r = None
    label_h = ""
    tim = son = None
    son_label = ""
    son_cluster: list[Any] = []
    w_by: dict[str, list[Any]] = {}
    e_by: dict[str, list[Any]] = {}
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
        w_by.setdefault(model, list(w))
        e_by.setdefault(model, list(e))
        if model == "haiku":
            gate_for_h = gate_rows
            hyp = hypotheses.run_hypotheses(w, e, seeds=seeds, gate_log=gate_for_h)
            sec = secondary.secondary(w, e, db, aset, seeds=seeds)
            desc = secondary.descriptive(w, e, seeds=seeds)
            sens = secondary.sensitivity(w, e, db, aset, seeds=seeds, qa_keys=_hqa_keys(settings))
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
            pred = posthoc.prediction_checks(w, hyp, desc, seeds=seeds)
            # post hoc (D-021): robustness, timing, disclosure; none of it enters a verdict. The robustness checks
            # (fresh generators, so their place in the run changes nothing) also qualify H1's reader wording (A3).
            rob = posthoc.robustness(w, seeds=seeds)
            h1_txt, h1_basis = final_review.h1_reader_verdict(hyp, rob)
            register.register_model(book, "haiku", aset=aset, w=w, e=e, p1r=p1r, hyp=hyp, sec=sec, desc=desc, sens=sens,
                                    power=power, pred=pred,
                                    verdict_texts={"H1": (h1_txt, h1_basis)} if h1_txt else None)
            M = MODEL_TOKENS["haiku"]
            register.register_robustness(book, M, rob)
            tim = posthoc.timing(w, e, db)
            register.register_timing(book, M, tim)
            # research/68 A (post hoc D-021/D-024 or descriptive): relay, held off, noteless, debrief, live only
            register.register_relay(book, M, final_review.claim_relay(w, e))
            register.register_held_off(book, M, final_review.held_off_never(w, tim, seeds=seeds))
            register.register_board_sub_noteless(book, M, sec.board_substitution)
            register.register_debrief_calls(book, M, final_review.debrief_calls(db, w, e))
            register.register_live_only(book, M, final_review.live_only(w, seeds=seeds))
            register.register_disclosure_d1(book, M, sec.rates)
            register.register_paraphrase_breakdown(book, M, sens.rates)
            # post hoc (D-024 item 4): FORGED - RECEIPT and H3 by paraphrase; the keyword rule against the labels
            rc = posthoc.receipt_contrasts(w, seeds=seeds)
            if rc is not None:
                register.register_receipt_contrasts(book, M, rc)
            register.register_keyword_check(book, M, posthoc.keyword_check(w, labels),
                                            scope=f"{label_h or model_label(aset.model)}, phase {aset.phase}")
            for key, sent_fn in (("PostHocRobustSentence", text.robustness_sentence),
                                 ("TimingSentence", text.timing_sentence)):
                s_txt = sent_fn(book, M)
                if s_txt is not None:
                    book.add(M + key, s_txt, "latex", source=f"{posthoc.POSTHOC}: template")
            summary["phases"][aset.phase]["posthoc"] = {
                "fisher_p": {x.name: x.fisher_p for x in rob.exact},
                "boundary": [[b.contrast, b.arm, b.m_shifts, b.n_shifts, b.p_analytic, b.p_boot] for b in rob.boundary],
                "timing_recorded_agree": {c.cell: [c.recorded_agree, c.recorded_checked] for c in tim.cells
                                          if c.has_log}}
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
            son, son_label = s, model_label(aset.model)
            n_cell = max((len(v) for v in aset.planned.values()), default=0)
            register.register_sonnet(book, s, aset=aset, episodes_per_cell=n_cell)
            son_cluster = final_review.cluster_rates("sonnet", w, seeds=seeds)        # research/68 A6 (Fig. 1a)
            register.register_cluster_rates(book, son_cluster, {r.cell: rate_prefix("sonnet", "p1", r.cell)
                                                                for r in son_cluster},
                                            label="exploratory Sonnet arm")
            book.add("SonnetSentence", text.sonnet_sentence(s, book, model_label=model_label(aset.model),
                                                            episodes_per_cell=n_cell), "latex",
                     source="template for Sonnet (exploratory)")
            register.register_keyword_check(book, MODEL_TOKENS["sonnet"], posthoc.keyword_check(w, labels),
                                            scope=f"{son_label}, phase {aset.phase} (exploratory arm)")
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
    # exploratory follow-up (D-022): its own episode list from phase exp1, never a confirmatory set
    x1_labels = exp1_labels if exp1_labels is not None else exp1.label_source(settings)
    x1 = exp1.analyse(db, x1_labels, seeds=seeds, ref_witnesses=w_by, ref_episodes=e_by)
    register.register_exp1(book, x1)
    x1_cluster: dict[str, Any] = {}          # research/68 A6: the follow-up rows of Fig. 1a, cluster bootstrap
    if x1.status == exp1.COMPLETE:
        for a in x1.arms:
            rs = final_review.cluster_rates(a.arm.model, [x for x in x1.witnesses if x.cell == a.arm.cell
                                                          and x.model == a.arm.model], seeds=seeds)
            if rs:
                x1_cluster[a.arm.token] = rs[0]
                register.register_cluster_rates(book, rs, {a.arm.cell: "ExpOne" + a.arm.token + "POne"},
                                                label=f"{register.EXPLORATORY_D022} follow-up arm {a.arm.label}")
    if x1.status == exp1.COMPLETE and x1.witnesses:
        register.register_keyword_check(book, "ExpOne", posthoc.keyword_check(x1.witnesses, x1_labels),
                                        scope="exploratory (D-022) follow-up, phase exp1, all three arms pooled, "
                                              "labels from the exp1 judge database")
    summary["exp1"] = {"status": x1.status, "n_done": dict(x1.n_done), "n_open": x1.n_open,
                       "n_pending": x1.n_pending, "notes": list(x1.notes)}
    notes.extend(x1.notes)
    # the author read (D-021 item 3)
    hr_dir = Path(human_read_dir) if human_read_dir is not None else \
        Path(settings.paths.runs_dir).parent / "annotations" / "human_read"
    hr = humanread.read_human_read(hr_dir, [x for ws in w_by.values() for x in ws],
                                 bootstrap_B=seeds.bootstrap_B)
    register.register_human_read(book, hr)
    register.register_human_read_prevalence(book, hr)             # research/68 A9
    summary["human_read"] = humanread.summary_note(hr)
    cost = _cost(settings, db)
    book.add("CostMainUsd", cost["main_usd"], "usd", source="cost_ledger of the main database")
    book.add("CostJudgeUsd", cost["judge_usd"], "usd",
             source="judge_ledger of every judge database (runs/judge*/judge.sqlite, D-023 item 4)")
    book.add("CostTotalUsd", cost["total_usd"], "usd", source="prior spend + main ledger + every judge ledger")
    x = cost["exp1"]
    xsrc = "exploratory (D-022) follow-up spend (D-023 item 1)"
    book.add("CostExpOneRunUsd", x["run_usd"], "usd", source=f"{xsrc}: main ledger, phase exp1")
    book.add("CostExpOneProbeUsd", x["probe_usd"], "usd", source=f"{xsrc}: main ledger, phase exp1probe")
    book.add("CostExpOneJudgeUsd", x["judge_usd"], "usd", source=f"{xsrc}: judge_ledger of runs/judge_exp1")
    book.add("CostExpOneTotalUsd", x["total_usd"], "usd", source=f"{xsrc}: run + probes + judge")
    book.add("CostBeforeExpOneUsd", cost["before_exp1_usd"], "usd",
             source="spend without the D-022 follow-up: pilots, core, Sonnet arm, their judge (CostTotalUsd minus "
                    "CostExpOneTotalUsd)")
    judge_pack = _judge_pack(settings, seeds, strict=strict, notes=notes)
    jc = _judge_counts(settings)
    if jc is not None:
        register.register_judge_counts(book, jc)
    if judge_pack is not None:
        register.register_judge(book, judge_pack[0], judge_pack[1])
        register.register_judge_prevalence(book, judge_pack[1])
        register.register_kappa_prevalence(book, judge_pack[1])        # research/68 A9
    fig1a_rows = None
    if hyp is not None:
        fig1a_rows = _fig1a_rows(p1r, son_cluster, x1, x1_cluster)
        register.register_fig1a_flags(book, list(fig1a_rows[0]) + list(fig1a_rows[1]) + [r.p1 for r in fig1a_rows[2]],
                                      B=seeds.bootstrap_B)

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
                                           notes=notes, timing=tim, son=son, son_label=son_label,
                                           fig1a_rows=fig1a_rows)
    numbers_json = out_dir / "numbers.json"
    macros = out_dir / ("macros.tex" if strict else "macros.provisional.tex")
    book.write_json(numbers_json)
    book.write_macros(macros, status=status)
    summary["judge_counts"] = jc
    summary["notes"] = notes
    summary["n_numbers"] = len(book)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    runs = Path(settings.paths.runs_dir).resolve()
    outside = runs not in out_dir.resolve().parents
    if export_paper is not None or outside:
        if private is None:
            from wtth.prereg import load_private

            private = load_private(settings)
        if outside:     # files written outside runs/ pass the residual scan (summary.json included)
            from wtth.redact import _Redactor

            red = _Redactor(dict(private))
            for f in sorted(p for p in out_dir.rglob("*") if p.is_file()):
                hits = red.residual(f.read_bytes())
                if hits:
                    import shutil

                    shutil.rmtree(out_dir, ignore_errors=True)
                    raise PrivateLeakError(f"private string ids {sorted(hits)} found in {f.name}; output removed")
        if export_paper is not None:
            files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.name != "summary.json")
            _scan_and_copy(files, Path(export_paper), private, out_dir)
    return RunOutputs(out_dir, status, numbers_json, macros, figs, tabs)
