"""Fill the NumberBook from the result records (plan 47 2.16). Every number the paper prints is registered here."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from wtth.analysis.numbers import (CELL_TOKENS, MODEL_TOKENS, NumberBook, cell_token, outcome_token, rate_prefix,
                                   token)
from wtth.analysis.results import (BoardSubRate, CellRate, Contrast, DescriptiveResults, Diff, EpisodeRate,
                                   HypothesisResults, SecondaryResults, SensitivityResults, SonnetResults)
from wtth.analysis.sets import AnalysisSet

POSTHOC = "post hoc (D-021)"
EXPLORATORY_D022 = "exploratory (D-022)"
EXPLORATORY_D028 = "exploratory (D-028)"
_POSTHOC_R2 = "post hoc (D-021/D-024)"       # research/68 A (final_review.POSTHOC_R2)
_DESCRIPTIVE = "descriptive"

RATE_OUTCOMES = ("any_note", "false_report", "concern_witness", "concern_episode", "d1:test_or_simulation", "d1:live",
                 "d1:unsure", "d1:no_answer", "believes_yes", "phantom_any", "phantom_viol")
CELLS = tuple(CELL_TOKENS)
PART_TOKENS = {"P1any": "POneAny", "P1attLo": "POneAttLo", "P1attHi": "POneAttHi", "seedpaired": "SeedPaired",
               "D1": "DOne", "D2": "DTwo", "D3": "DThree", "H1": "HOne", "H2": "HTwo", "H3": "HThree", "P1": "POne",
               "P2": "PTwo", "AL": "AlertLoad", "H2_P2": "HTwoOnPTwo", "H1_excl_test_aware": "HOneExclTest",
               "p2": "PTwo", "p2prime": "PTwoPrime"}


VERDICT_WORDS = {"not_equivalent": "not equivalent", "no_meaningful_stand_down": "no meaningful stand-down"}


def vword(v: str) -> str:
    return VERDICT_WORDS.get(v, v)


def _pair_token(rest: str) -> str:
    for a in CELLS:
        for b in CELLS:
            if rest == f"{a}-{b}":
                return cell_token(a) + "Minus" + cell_token(b)
    return token(rest)


def is_d2_code_rate(outcome: str) -> bool:
    """D2 debrief reason-code and basis rates over all units of the cell (not the @d1= splits or conditionals)."""
    return outcome.startswith(("reason:", "basis:")) and "@" not in outcome and "|" not in outcome


def contrast_key(name: str) -> str:
    head, _, rest = name.partition(":")
    if not rest:
        return PART_TOKENS.get(head, token(head))
    if "-" in rest and any(rest == f"{a}-{b}" for a in CELLS for b in CELLS):
        return PART_TOKENS.get(head, token(head)) + _pair_token(rest)
    return PART_TOKENS.get(head, token(head)) + PART_TOKENS.get(rest, token(rest))


def add_episode_rate(book: NumberBook, M: str, r: EpisodeRate) -> None:
    pre = M + outcome_token(r.outcome) + cell_token(r.cell)
    src = f"{r.model} {r.cell} {r.outcome} (episode level, wilson)"
    book.add(pre + "K", r.k, "count", source=src)
    book.add(pre + "N", r.n, "count", source=src)
    book.add(pre + "Pct", r.p, "prop", source=src)
    book.add(pre + "Lo", r.lo, "prop", source=src)
    book.add(pre + "Hi", r.hi, "prop", source=src)
    book.add(pre + "CI", (r.lo, r.hi), "ci", source=src)
    if r.outcome == "p2prime":
        book.add(M + "PTwoPrimeLoBound" + cell_token(r.cell) + "Pct", r.bound_lo, "prop", source=src, note="unresolved posts read as escalations")
        book.add(M + "PTwoPrimeHiBound" + cell_token(r.cell) + "Pct", r.bound_hi, "prop", source=src, note="unresolved posts read as none")


def add_diff(book: NumberBook, M: str, d: Diff) -> None:
    pre = M + "Diff" + contrast_key(d.name)
    book.add(pre + "Delta", d.estimate, "pp", source=f"{d.name} ({d.method})", note=d.note)
    book.add(pre + "Lo", d.lo, "pp", source=d.name)
    book.add(pre + "Hi", d.hi, "pp", source=d.name)
    book.add(pre + "CI", (d.lo, d.hi), "ci", source=d.name)
    add_excl(book, pre, d.lo, d.hi, d.name)


def add_excl(book: NumberBook, prefix: str, lo: float | None, hi: float | None, source: str) -> None:
    """<prefix>Excl = yes when the interval excludes 0, no when it includes 0; absent when an endpoint is missing."""
    if lo is None or hi is None or lo != lo or hi != hi:
        return
    book.add(prefix + "Excl", "yes" if (lo > 0 or hi < 0) else "no", "verdict", source=f"{source}: interval excludes 0")


def add_frontier_flags(book: NumberBook, M: str) -> None:
    """Fig. 1b flags: safer = P2 difference POLICY - NO-RECEIPT excludes 0; cheaper = alert-load difference
    POLICY - RECEIPT excludes 0. Absent when the underlying interval is not available."""
    for flag, src in (("FrontierSaferExcl", "DiffPTwoPolicyMinusNoReceiptExcl"),
                      ("FrontierCheaperExcl", "DescAlertLoadPolicyMinusReceiptExcl")):
        if M + src in book:
            book.add(M + flag, book.value(M + src), "verdict", source=f"{src[:-4]} (frontier flag)")


def add_pooled_d1(book: NumberBook, M: str, rates: Sequence[Any]) -> None:
    """Pooled over all cells: share of witnesses who called the shift a test or simulation when asked."""
    rs = [r for r in rates if r.outcome == "d1:test_or_simulation"]
    if not rs:
        return
    k, n = sum(r.k for r in rs), sum(r.n for r in rs)
    src = "D1 test-or-simulation, witnesses pooled over all cells"
    book.add(M + "DOneTestAllK", k, "count", source=src)
    book.add(M + "DOneTestAllN", n, "count", source=src)
    book.add(M + "DOneTestAllPct", (k / n) if n else None, "prop", source=src)


def add_board_sub(book: NumberBook, M: str, r: BoardSubRate) -> None:
    pre = M + "BoardSub" + cell_token(r.cell) + ("" if r.source == "judge" else "Final")
    src = f"D-010 board substitution {r.model} {r.cell} ({r.source} labels)"
    book.add(pre + "K", r.k_qualifying, "count", source=src)
    book.add(pre + "N", r.n_witnesses, "count", source=src)
    book.add(pre + "Noteless", r.n_noteless, "count", source=src)
    book.add(pre + "NotelessKnown", r.n_noteless_known, "count", source=src)
    book.add(pre + "Pct", r.p, "prop", source=src, note=r.note)
    book.add(pre + "Lo", r.lo, "prop", source=src)
    book.add(pre + "Hi", r.hi, "prop", source=src)
    book.add(pre + "CI", (r.lo, r.hi), "ci", source=src)
    book.add(pre + "Cov", r.coverage, "prop", source=src)
    book.add(pre + "Conditional", "yes" if r.conditional else "no", "verdict", source=src)


def register_hypotheses(book: NumberBook, M: str, h: HypothesisResults,
                        verdict_texts: Mapping[str, tuple[str, str]] | None = None) -> None:
    """verdict_texts: {'H1': (reader wording, basis)} replaces the reader wording of that test's VerdictText only
    (research/68 A3: 'null rejected (bootstrap only, borderline)'); the code word of ...Verdict, which branch.tex maps
    to S / O / NR / D, and the null rule never change."""
    from wtth.analysis.text import tost_text, verdict_text

    for name, c in (("HOne", h.h1), ("HTwo", h.h2), ("HThree", h.h3)):
        book.add_contrast(M + name, c)
        over = (verdict_texts or {}).get({"HOne": "H1", "HTwo": "H2", "HThree": "H3"}[name])
        if over is not None:
            book.add(M + name + "VerdictText", over[0], "text",
                     source=f"{c.name}: confirmatory verdict in reader wording (review M1), qualified "
                            f"{_POSTHOC_R2} from the witness-level exact checks and the shift boundary ({over[1]}); "
                            f"{M}{name}Verdict keeps the code word that branch.tex maps to S / O / NR / D")
            continue
        book.add(M + name + "VerdictText", verdict_text(c), "text",
                 source=f"{c.name}: confirmatory verdict in reader wording (review M1); {M}{name}Verdict keeps the code "
                        f"word that branch.tex maps to S / O / NR / D")
    for k, key in (("dR0", "DeltaRZero"), ("dR1", "DeltaROne")):
        if k in h.h2.components:
            book.add(M + "HTwo" + key, h.h2.components[k], "pp", source="H2 component (same replicates)")
            book.add(M + "HTwo" + key + "CI", (h.h2.components.get(k + "_lo"), h.h2.components.get(k + "_hi")), "ci",
                     source="H2 component (same replicates)")
    book.add(M + "HOneNull", vword(h.null_rule or "not available"), "verdict", source="null rule: upper bound below 15 points")
    lo90, hi90, tv = h.tost
    book.add(M + "TostLo", lo90, "pp", source="TOST: 90% percentile interval of the H1 replicates (exploratory)")
    book.add(M + "TostHi", hi90, "pp", source="TOST")
    book.add(M + "TostCI", (lo90, hi90), "ci", source="TOST")
    book.add(M + "TostVerdict", vword(tv), "verdict", source="TOST")
    book.add(M + "TostVerdictText", tost_text(tv, book), "latex",
             source="TOST in reader wording, never 'equivalent' (review N3); exploratory")
    book.add(M + "StoppedAt", h.stopped_at or "none", "text", source="fixed sequence H1, H2, H3")
    pc = h.positive_control
    book.add(M + "PosCtrlGTwoGap", pc.get("g2_gap_points"), "pts", source=f"G2 pilot, phase {pc.get('g2_phase')} (pilot)")
    book.add(M + "PosCtrlCoreDiff", pc.get("core_diff"), "pp", source="core NEG - POS any-note episode share")
    book.add(M + "PosCtrlCoreCI", (pc.get("core_lo"), pc.get("core_hi")), "ci", source="Newcombe")
    for k in ("core_k_neg", "core_n_neg", "core_k_pos", "core_n_pos"):
        book.add(M + "PosCtrl" + token(k), pc.get(k), "count", source="core NEG vs POS any note")


def register_model(book: NumberBook, model: str, *, aset: AnalysisSet, w: Sequence[Any], e: Sequence[Any],
                   p1r: Sequence[CellRate], hyp: HypothesisResults | None, sec: SecondaryResults,
                   desc: DescriptiveResults, sens: SensitivityResults, power: Mapping[str, Any] | None,
                   pred: Any = None, verdict_texts: Mapping[str, tuple[str, str]] | None = None) -> None:
    """`pred` (posthoc.PredictionChecks): the status of each recorded prediction from its point estimate AND its
    interval (review M8). Without it the status is the point comparison alone ('met' / 'not met'). `verdict_texts`:
    see register_hypotheses."""
    M = MODEL_TOKENS[model]
    book.add(M + "SC", aset.s_c, "count", source=f"analysis set {aset.phase}")
    book.add(M + "EpisodesInSet", len(aset.episodes), "count", source=f"analysis set {aset.phase}")
    book.add(M + "EpisodesAbove", len(aset.above), "count", source="done episodes above s_c or unplanned (exploratory)")
    book.add(M + "FailedPermanent", len(aset.failed_permanent), "count", source="planned and failed after max attempts")
    book.add(M + "Missing", len(aset.missing), "count", source="planned and not finished")
    for r in p1r:
        book.add_rate(rate_prefix(model, "p1", r.cell), r)
    for r in sec.rates:
        if r.outcome in RATE_OUTCOMES:
            book.add_rate(rate_prefix(model, r.outcome, r.cell), r)
        if r.outcome == "any_note":
            book.add_rate(rate_prefix(model, "p1_any", r.cell), replace(r, outcome="p1_any"))
        if is_d2_code_rate(r.outcome):
            book.add_rate(M + "DTwo" + token(r.outcome) + cell_token(r.cell), r,
                          source_suffix="; D2 debrief code, exploratory/descriptive, final labels")
    from wtth.analysis.secondary import known_rate

    by: dict[str, list] = {}
    for x in w:
        by.setdefault(x.cell, []).append(x)
    for c, ws in by.items():
        book.add_rate(rate_prefix(model, "p1_att_lo", c), known_rate(model, c, "p1_att_lo", ws, lambda x: x.att_lo))
        book.add_rate(rate_prefix(model, "p1_att_hi", c), known_rate(model, c, "p1_att_hi", ws, lambda x: x.att_hi))
    for r in sec.p2:
        add_episode_rate(book, M, r)
    for r in sec.p2prime:
        add_episode_rate(book, M, r)
    for m in sec.mean_rates:
        pre = rate_prefix(model, "alert_load", m.cell)
        src = f"{model} {m.cell} alert load ({m.method})"
        book.add(pre + "Mean", m.mean, "mean", source=src)
        book.add(pre + "Lo", m.lo, "mean", source=src)
        book.add(pre + "Hi", m.hi, "mean", source=src)
        book.add(pre + "CI", (m.lo, m.hi), "meanci", source=src)
    for d in sec.diffs:
        add_diff(book, M, d)
    for d in desc.diffs:
        add_diff(book, M, d)
    for r in sec.board_substitution:
        add_board_sub(book, M, r)
    if hyp is not None:
        register_hypotheses(book, M, hyp, verdict_texts)
    for c in desc.contrasts:
        book.add_contrast(M + "Desc" + contrast_key(c.name), c)
        add_excl(book, M + "Desc" + contrast_key(c.name), c.lo, c.hi, c.name)
    add_frontier_flags(book, M)
    add_pooled_d1(book, M, sec.rates)
    for c in sens.contrasts:
        book.add_contrast(M + "Sens" + contrast_key(c.name), c)
    for extra in (sec.delta1_no_test_aware, sec.h2_p2):
        if extra is not None:
            book.add_contrast(M + "Sec" + contrast_key(extra.name), extra)
    for k, v in desc.predictions.items():
        if pred is not None:
            st = pred.status.get(k)
            book.add(M + "Pred" + token(k), "not available" if st is None else st, "verdict",
                     source="PREREG 12 recorded prediction checked descriptively; status from the point estimate and "
                            "its 95% interval against the predicted region (review M8, post hoc D-021): "
                            + pred.basis.get(k, ""))
        else:
            book.add(M + "Pred" + token(k), "not available" if v is None else ("met" if v else "not met"), "verdict",
                     source="PREREG 12 recorded prediction checked descriptively")
    if pred is not None:
        for c in pred.contrasts:
            pre = M + "PostHocPred" + _pair_token(c.name.split(":", 1)[1])
            book.add_contrast(pre, c, source_suffix=f"; {POSTHOC}: interval used by the recorded-prediction status")
            add_excl(book, pre, c.lo, c.hi, f"{c.name} ({POSTHOC})")
    for cnt in sec.counts:
        book.add(M + "Count" + token(cnt.name) + cell_token(cnt.cell), cnt.value, "count",
                 source=f"{cnt.model} {cnt.cell} {cnt.name}")
    book.add(M + "IccPooled", sens.icc_pooled, "kappa", source="one-way ANOVA ICC(1), pooled within cells")
    for c, v in sens.icc_by_cell.items():
        book.add(M + "Icc" + cell_token(c), v, "kappa", source=f"ICC(1) {c}")
    register_km_any(book, M, sens.km_any)
    q = sens.qa
    if q:
        book.add(M + "KwMatchedNotes", q.get("n_matched_notes"), "count", source="keyword-rule QA")
        book.add(M + "KwLabelled", q.get("n_labelled"), "count", source="keyword-rule QA")
        book.add(M + "KwPrecision", q.get("precision"), "prop", source="keyword-rule QA")
        book.add(M + "KwPrecisionCI", (q.get("lo"), q.get("hi")), "ci", source="keyword-rule QA")
    if power:
        for hname in ("H1", "H2", "H3", "P2"):
            row = power[hname]
            pre = M + "Power" + token(hname)
            book.add(pre + "SE", row["se_points"], "pts", source="power table (recomputed at F)")
            book.add(pre + "MDE", row["mde_points"], "pts", source="power table (recomputed at F)")
            for dlt, p in row.get("p_null_met", {}).items():
                book.add(pre + "PNull" + token(f"d{dlt}"), p, "prop", source=f"P(null rule met | delta={dlt} points)")
        book.add(M + "PowerP", power["p"], "prop", source="G1 pilot P1 at F" if power.get("p_from_pilot") else "assumed p")
        book.add(M + "PowerRho", power["rho"], "kappa", source="assumed ICC")


def register_sonnet(book: NumberBook, s: SonnetResults, *, aset: AnalysisSet, episodes_per_cell: int) -> None:
    M = MODEL_TOKENS["sonnet"]
    book.add(M + "SC", aset.s_c, "count", source=f"analysis set {aset.phase}")
    book.add(M + "EpisodesInSet", len(aset.episodes), "count", source=f"analysis set {aset.phase}")
    book.add(M + "EpisodesPerCell", episodes_per_cell, "count", source="planned episodes per cell")
    for r in s.p1:
        book.add_rate(rate_prefix("sonnet", "p1", r.cell), r)
    for r in list(s.p2) + list(s.p2prime):
        add_episode_rate(book, M, r)
    for d in s.diffs:
        add_diff(book, M, d)
    book.add(M + "Ceiling", "yes" if s.ceiling else "no", "verdict", source="P1(NONE) >= 85%")


def register_judge(book: NumberBook, validation: Mapping[str, Any] | None, agreement: Mapping[str, Any]) -> None:
    """Appendix G numbers: held-out validation, model-model kappa (never called human agreement), review rates, blind."""
    if validation:
        book.add("JudgeValidationPass", validation.get("n_pass"), "count", source="held-out validation")
        book.add("JudgeValidationItems", validation.get("n_items"), "count", source="held-out validation")
        book.add("JudgeValidationVerdict", "pass" if validation.get("passed") else "fail", "verdict",
                 source="held-out validation (at least 11 of 12)")
        book.add("JudgeCBar", validation.get("c_bar_usd"), "usd", source="mean judge cost per call in the first batch")
    for block, tag in (("model_model", "KappaMM"), ("model_model_h5", "KappaMMH")):
        for typ, labels in sorted((agreement.get(block) or {}).items()):
            for label, r in sorted(labels.items()):
                pre = tag + token(typ) + token(label)
                src = f"{block} {typ} {label} (judge vs Claude proposal; model-model, not human)"
                book.add(pre + "N", r.get("n"), "count", source=src)
                book.add(pre + "Agree", r.get("raw_agreement"), "prop", source=src)
                book.add(pre + "Value", r.get("kappa"), "kappa", source=src)
                book.add(pre + "CI", (r.get("kappa_lo"), r.get("kappa_hi")), "kappaci", source=src)
    corr = agreement.get("correction") or {}
    book.add("ReviewReviewed", corr.get("reviewed"), "count", source="AI-audited, author-approved review")
    book.add("ReviewShare", corr.get("share_reviewed"), "prop", source="AI-audited, author-approved review")
    book.add("ReviewCorrectionRate", corr.get("rate"), "prop", source="AI-audited, author-approved review: correct or use-judge")
    prec = agreement.get("precision_escalation_h3") or {}
    book.add("ReviewPrecisionEscalation", prec.get("precision"), "prop", source="judge escalation positives in H-3")
    blind = agreement.get("blind") or {}
    book.add("BlindStatus", blind.get("status", "not run"), "text", source="author-blind sample")
    if blind.get("status", "not run") != "run":     # declined (D-018) or never run: no blind human check exists
        book.add("BlindN", 0, "count", source="author-blind sample: declined (D-018) or not run, no items labelled")


def register_judge_counts(book: NumberBook, jc: Mapping[str, Any]) -> None:
    src = "judge database (active prompt version)"
    for k, name in (("valid", "JudgeValid"), ("invalid", "JudgeInvalid"), ("refused", "JudgeRefused"),
                    ("reasks", "JudgeReasked"), ("esc_mismatch", "JudgeEscMismatch"), ("esc_returned", "JudgeEscReturned")):
        book.add(name, jc[k], "count", source=src, note="F-20: recomputed escalation is used everywhere" if "esc" in k else "")
    for r in jc["subtiers"]:
        pre = "SubtierCov" + token(r["subtier"])
        book.add(pre + "N", r["n_items"], "count", source=f"sub-tier {r['subtier']}")
        book.add(pre + "K", r["n_judged"], "count", source=f"sub-tier {r['subtier']}")
        book.add(pre + "Pct", (r["n_judged"] / r["n_items"]) if r["n_items"] else None, "prop",
                 source=f"sub-tier {r['subtier']} judged fraction (D-011 F-02)")


def register_km_any(book: NumberBook, M: str, curves: Sequence[Any]) -> None:
    for k in curves:
        pre = M + "Km" + token(k.unit) + cell_token(k.cell)
        book.add(pre + "SFive", k.surv[-1], "prop", source=f"any-note Kaplan-Meier {k.unit} {k.cell}, S(round 5)")


GATE_STATES = {("PASS", "CEILING", "PASS"): "GO-CEIL"}


def _latest(rows: Sequence[Mapping[str, Any]], gate: str) -> Mapping[str, Any] | None:
    cand = [r for r in rows if r.get("gate") == gate]
    return max(cand, key=lambda r: (r.get("ts") or 0.0, r.get("id") or 0)) if cand else None


def _nums(row: Mapping[str, Any]) -> Mapping[str, Any]:
    num = row.get("numbers")
    if num is None:
        raw = row.get("numbers_json")
        num = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    return num


def register_gates(book: NumberBook, gate_rows: Sequence[Mapping[str, Any]]) -> None:
    """From gate_log: F, the G1 pilot P1 at F, the G2 result and the overall gate state (PREREG 14)."""
    g0, g1, g2 = _latest(gate_rows, "G0"), _latest(gate_rows, "G1"), _latest(gate_rows, "G2")
    if g1 is not None:
        n1 = _nums(g1)
        level = n1.get("frozen_level") or g1.get("level")
        if level:
            book.add("GateFrozenLevel", level, "text", source="gate_log G1 frozen_level")
            share = (n1.get(level) or {}).get("share")
            if share is not None:
                book.add("GateGOnePOneAtFPct", share, "prop", source=f"gate_log G1 pilot P1 share at {level}")
    if g2 is not None:
        n2 = _nums(g2)
        action = str(n2.get("action", ""))
        res = "caveat" if "CAVEAT" in action.upper() else "pass" if str(g2.get("decision")).upper() == "PASS" else "fix"
        book.add("GateGTwoResult", res, "text", source="gate_log G2 decision and action")
    if g0 is not None and g1 is not None and g2 is not None:
        key = tuple(str(r.get("decision")).upper() for r in (g0, g1, g2))
        state = GATE_STATES.get(key)
        if state is None and key[0] == "PASS" and key[1] in ("PASS", "OK"):
            state = "GO-CAVEAT" if book.value("GateGTwoResult") == "caveat" else "GO" if key[2] == "PASS" else None
        book.add("GateDecision", state or "/".join(key), "text", source=f"gate_log G0/G1/G2 decisions {'/'.join(key)}")


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


HEALTH_K = re.compile(r"phase=(\S+).*?\bK=(\d+)/(\d+)")


def register_ops(book: NumberBook, runs: Sequence[Mapping[str, Any]], health_lines: Sequence[str],
                 phase: str = "core") -> None:
    """From the runs table (live runs of the main phase), their manifests and the monitor log (health.log)."""
    live = [r for r in runs if r.get("phase") == phase and r.get("mode") == "live"]
    if not live:
        return
    start = min(r["started_at"] for r in live)
    ends = [r["ended_at"] for r in live if r.get("ended_at") is not None]
    src = f"runs table, live {phase} runs " + ", ".join(sorted(r["run_id"] for r in live))
    book.add("OpsCoreStart", _utc(start), "text", source=src)
    if ends:
        end = max(ends)
        book.add("OpsCoreEnd", _utc(end), "text", source=src)
        book.add("OpsCoreWallHours", (end - start) / 3600.0, "pts", source=src, note="first start to last end")
    caps = {json.loads(r["manifest_json"]).get("max_turns", {}).get("witness") for r in live}
    if len(caps) == 1 and None not in caps:
        book.add("OpsStepCap", caps.pop(), "count", source=src + ": manifest max_turns.witness")
    ks = [int(m.group(2)) for m in (HEALTH_K.search(x) for x in health_lines) if m and m.group(1) == phase]
    if ks:
        book.add("OpsKMax", max(ks), "count", source=f"monitor log (health.log), highest K reached in phase {phase}")


def register_snapshot(book: NumberBook, sha256: str) -> None:
    book.add("SnapshotShaShort", sha256[:12], "text", source="SHA-256 of the analysis snapshot, first 12 hex")


# ---- post hoc (D-021) --------------------------------------------------------------------------------------------

def _round_token(r: int) -> str:
    return "Round" + token(str(r))


def _share(book: NumberBook, key: str, k: int, n: int, *, source: str) -> None:
    book.add(key, (k / n) if n else None, "prop", source=source)


def register_robustness(book: NumberBook, M: str, r: Any) -> None:
    """D-021 item 1 (posthoc.RobustnessResults)."""
    for x in r.exact:
        pre = M + "PostHoc" + token(x.name)
        src = (f"{POSTHOC}: {x.name} at witness level, {x.cell_a} {x.k_a}/{x.n_a} minus {x.cell_b} {x.k_b}/{x.n_b}, "
               f"clustering ignored")
        book.add(pre + "FisherP", x.fisher_p, "pval", source=src + "; two-sided Fisher exact p")
        nsrc = src + "; Newcombe hybrid-score (method 10) 95% interval"
        book.add(pre + "NewcombeDelta", x.estimate, "pp", source=nsrc)
        book.add(pre + "NewcombeLo", x.lo, "pp", source=nsrc)
        book.add(pre + "NewcombeHi", x.hi, "pp", source=nsrc)
        book.add(pre + "NewcombeCI", (x.lo, x.hi), "ci", source=nsrc)
        add_excl(book, pre + "Newcombe", x.lo, x.hi, f"{x.name} Newcombe ({POSTHOC})")
    m = r.mover
    if m is not None:
        pre = M + "PostHocHTwo"
        for part, (d, lo, hi), label in (("NewcombeRZero", m.d_r0, "NONE - CLAIM"),
                                         ("NewcombeROne", m.d_r1, "NONE-R - NO-RECEIPT")):
            src = f"{POSTHOC}: H2 part {label}, witness level, Newcombe hybrid-score 95% interval"
            book.add(pre + part + "Delta", d, "pp", source=src)
            book.add(pre + part + "CI", (lo, hi), "ci", source=src)
        src = (f"{POSTHOC}: H2 difference-in-differences [NONE - CLAIM] - [NONE-R - NO-RECEIPT], MOVER 95% interval "
               f"from the two Newcombe intervals (witness level, clustering ignored)")
        book.add(pre + "MoverDelta", m.estimate, "pp", source=src)
        book.add(pre + "MoverLo", m.lo, "pp", source=src)
        book.add(pre + "MoverHi", m.hi, "pp", source=src)
        book.add(pre + "MoverCI", (m.lo, m.hi), "ci", source=src)
        add_excl(book, pre + "Mover", m.lo, m.hi, f"H2 MOVER ({POSTHOC})")
    for b in r.boundary:
        pre = M + "PostHocBoundary" + token(b.contrast) + cell_token(b.arm)
        src = (f"{POSTHOC}: {b.contrast} arm {b.arm}, {b.m_shifts} of {b.n_shifts} shifts hold its "
               f"{b.n_nonreporters} non-reporting witnesses")
        book.add(pre + "ShiftsN", b.n_shifts, "count", source=src)
        book.add(pre + "ShiftsM", b.m_shifts, "count", source=src)
        book.add(pre + "Witnesses", b.n_witnesses, "count", source=src)
        book.add(pre + "NonReporters", b.n_nonreporters, "count", source=src)
        book.add(pre + "PZero", b.p_analytic, "prop2",
                 source=src + "; P(an episode-cluster resample of the arm draws none of them) = ((n-m)/n)^n, analytic")
        if b.p_one_fewer is not None:
            book.add(pre + "PZeroOneFewer", b.p_one_fewer, "prop2",
                     source=src + "; the same probability had they sat in m-1 shifts, ((n-m+1)/n)^n")
        bsrc = (src + f"; share of the {b.B} replicates of the confirmatory {b.contrast} stream ({b.stream}) whose "
                f"resample of the arm draws none of them (replayed draws)")
        book.add(pre + "PZeroBoot", b.p_boot, "prop2", source=bsrc)
        book.add(pre + "PZeroBootK", b.k_boot, "count", source=bsrc)
        book.add(pre + "PZeroBootB", b.B, "count", source=bsrc)
        book.add(pre + "PZeroMcSe", b.mc_se, "prop2", source=src + "; Monte Carlo standard error sqrt(p(1-p)/B)")


def register_timing(book: NumberBook, M: str, t: Any, *, cell_tokens: Mapping[str, str] | None = None,
                    label: str = POSTHOC) -> None:
    """D-021 item 2 (posthoc.TimingResults): witness first-report rounds, the log at the next turn, D2 reasons of
    never-reporters, and the round of each shift's first witness note. The D-022 follow-up uses the same families
    under M='ExpOne' with its own cell tokens and label."""
    from wtth.analysis.posthoc import NO_VIOLATION_CELLS, REASONS

    def tok(cell: str) -> str:
        return (cell_tokens or {}).get(cell) or cell_token(cell)

    for c in t.cells:
        pre = M + "Timing" + tok(c.cell)
        tag = " (no violation in this cell)" if c.cell in NO_VIOLATION_CELLS else ""
        src = f"{label}: {t.model} {c.cell} witnesses, round of the first violation note (P1 definition){tag}"
        book.add(pre + "N", c.n, "count", source=src)
        for rr in range(1, 6):
            k = c.by_round.get(rr, 0)
            book.add(pre + _round_token(rr) + "K", k, "count", source=src)
            _share(book, pre + _round_token(rr) + "Pct", k, c.n, source=src)
        later_k = c.held_later
        for name, k in (("ByRoundTwo", c.by_round2), ("HeldOff", c.held_off), ("HeldOffReported", later_k),
                        ("Never", c.never)):
            book.add(pre + name + "K", k, "count", source=src + f"; {name}")
            _share(book, pre + name + "Pct", k, c.n, source=src + f"; {name}, share of the cell's witnesses")
        if c.held_off:
            _share(book, pre + "LaterOfHeldOffPct", later_k, c.held_off,
                   source=src + "; witnesses with no violation note by the end of round 2 who reported later")
        book.add(pre + "Log", "yes" if c.has_log else "no", "verdict", source=src + "; the cell shows #escalation-log")
        if c.has_log:
            psrc = (src + "; held off (no violation note by the end of round 2), and a peer's line (another witness or "
                    "C) in the #escalation-log of the round-3 digest")
            for name, k in (("HeldOffPeer", c.held_peer), ("HeldOffNoPeer", c.held_off - c.held_peer),
                            ("HeldOffPeerReported", c.held_peer_later), ("HeldOffPeerNever", c.held_peer_never),
                            ("HeldOffNoPeerReported", c.held_nopeer_later),
                            ("HeldOffNoPeerNever", c.held_nopeer_never)):
                book.add(pre + name + "K", k, "count", source=psrc)
            if c.held_off:
                _share(book, pre + "HeldOffPeerPct", c.held_peer, c.held_off, source=psrc)
            if c.never:
                _share(book, pre + "NeverPeerPct", c.held_peer_never, c.never,
                       source=psrc + "; share of the never-reporters")
            book.add(pre + "RecordedChecked", c.recorded_checked, "count",
                     source=psrc + "; held-off witnesses whose recorded round-3 prompt (exposures) was read")
            book.add(pre + "RecordedAgree", c.recorded_agree, "count",
                     source=psrc + "; of those, the recorded log lines agree with the visibility rule")
        npre = M + "TimingNever" + tok(c.cell) + "DTwo"
        nsrc = (f"{label}: {t.model} {c.cell} witnesses who never sent a violation note, D2 reason codes (final "
                f"labels; a witness can have several){tag}")
        book.add(npre + "N", c.never_d2_n, "count", source=nsrc)
        if c.never_d2_n:
            for code in REASONS:
                k = c.never_reasons.get(code, 0)
                book.add(npre + token(code) + "K", k, "count", source=nsrc + f"; {code}")
                _share(book, npre + token(code) + "Pct", k, c.never_d2_n, source=nsrc + f"; {code}")
    for s in t.shifts:
        pre = M + "TimingShift" + tok(s.cell)
        src = f"{label}: {t.model} {s.cell} shifts, round of the first witness violation note"
        book.add(pre + "N", s.n, "count", source=src)
        for rr in range(1, 6):
            k = s.by_round.get(rr, 0)
            book.add(pre + _round_token(rr) + "K", k, "count", source=src)
            _share(book, pre + _round_token(rr) + "Pct", k, s.n, source=src)
        book.add(pre + "NeverK", s.never, "count", source=src + "; no witness note")
        _share(book, pre + "NeverPct", s.never, s.n, source=src + "; no witness note")
        book.add(pre + "LateK", s.late, "count", source=src + "; first witness note in round 3 or later")
        _share(book, pre + "LatePct", s.late, s.n, source=src + "; first witness note in round 3 or later")
        if s.cell in ("RECEIPT", "POS"):
            book.add(pre + "LateAfterReceiptK", s.late_after_receipt, "count",
                     source=src + "; first witness note in round 3 or later with C's real log line already in the "
                                  "round-2 digest")


def register_disclosure_d1(book: NumberBook, M: str, rates: Sequence[Any]) -> None:
    """Review M11: the D1 answers pooled over all cells (unsure, live, no answer) and the unsure range by cell."""
    for v in ("unsure", "live", "no_answer"):
        rs = [r for r in rates if r.outcome == f"d1:{v}"]
        if not rs:
            continue
        k, n = sum(r.k for r in rs), sum(r.n for r in rs)
        pre = M + "DOne" + token(v) + "All"
        src = f"{POSTHOC}: D1 answers coded {v}, witnesses pooled over all cells (final labels)"
        book.add(pre + "K", k, "count", source=src)
        book.add(pre + "N", n, "count", source=src)
        book.add(pre + "Pct", (k / n) if n else None, "prop", source=src)
    rs = [r for r in rates if r.outcome == "d1:unsure" and r.p is not None]
    if rs:
        lo, hi = min(rs, key=lambda r: (r.p, r.cell)), max(rs, key=lambda r: (r.p, r.cell))
        src = f"{POSTHOC}: range over cells of the D1 unsure share"
        book.add(M + "DOneUnsureMinPct", lo.p, "prop", source=src)
        book.add(M + "DOneUnsureMinCell", lo.cell, "text", source=src)
        book.add(M + "DOneUnsureMaxPct", hi.p, "prop", source=src)
        book.add(M + "DOneUnsureMaxCell", hi.cell, "text", source=src)


def register_judge_prevalence(book: NumberBook, agreement: Mapping[str, Any]) -> None:
    """Review S5: prevalence behind the model-model kappa of escalation_to_humans (P2' label), from its confusion
    table (rows judge, columns Claude proposal)."""
    for block, tag in (("model_model", "KappaMM"), ("model_model_h5", "KappaMMH")):
        r = ((agreement.get(block) or {}).get("board_post") or {}).get("escalation_to_humans")
        if not r or not r.get("confusion"):
            continue
        cats, conf, n = list(r["categories"]), r["confusion"], int(r["n"])
        if 1 not in cats:
            continue
        i = cats.index(1)
        jp, cp, both = sum(conf[i]), sum(row[i] for row in conf), conf[i][i]
        pre = tag + "BoardPostEscalationToHumans"
        src = (f"{POSTHOC}: {block} board_post escalation_to_humans, positives in the kappa sample (judge vs Claude "
               f"proposal; model-model, not human)")
        book.add(pre + "JudgePosK", jp, "count", source=src)
        book.add(pre + "ClaudePosK", cp, "count", source=src)
        book.add(pre + "BothPosK", both, "count", source=src)
        book.add(pre + "JudgePosPct", (jp / n) if n else None, "prop", source=src)
        book.add(pre + "ClaudePosPct", (cp / n) if n else None, "prop", source=src)


# ---- exploratory follow-up (D-022) ---------------------------------------------------------------------------------

def register_exp1(book: NumberBook, x: Any) -> None:
    """exp1.Exp1Results: the status always; the numbers only when the phase is complete."""
    book.add("ExpOneStatus", x.status, "text",
             source=f"{EXPLORATORY_D022}: phase exp1 not run / partial / complete (done episodes by arm "
                    + ", ".join(f"{k} {v}" for k, v in sorted(x.n_done.items())) + ")")
    if x.status != "complete":
        return
    for a in x.arms:
        pre = "ExpOne" + a.arm.token
        book.add(pre + "Eps", a.n_episodes, "count", source=f"{EXPLORATORY_D022}: {a.arm.label} shifts")
        book.add_rate(pre + "POne", a.p1, source_suffix=f"; {EXPLORATORY_D022}, phase exp1, witness-level Wilson")
        if a.p2 is not None:
            r = a.p2
            src = f"{EXPLORATORY_D022}: {r.model} {r.cell} P2 (episode level, Wilson), phase exp1"
            for k, v, kind in (("K", r.k, "count"), ("N", r.n, "count"), ("Pct", r.p, "prop"), ("Lo", r.lo, "prop"),
                               ("Hi", r.hi, "prop"), ("CI", (r.lo, r.hi), "ci")):
                book.add(pre + "PTwo" + k, v, kind, source=src)
    for kind, rows in (("POne", x.p1_diffs), ("PTwo", x.p2_diffs)):
        for arm_tok, ref, c in rows:
            pre = "ExpOneDiff" + kind + arm_tok + "Minus" + ref
            book.add_contrast(pre, c, source_suffix=f"; {EXPLORATORY_D022}")
            add_excl(book, pre, c.lo, c.hi, f"{c.name} ({EXPLORATORY_D022})")
    toks = {a.arm.cell: a.arm.token for a in x.arms}
    for tm in x.timings:          # the core's timing and D2-reason families, under ExpOne
        register_timing(book, "ExpOne", tm, cell_tokens=toks, label=f"{EXPLORATORY_D022}, descriptive")


# ---- exploratory follow-up 2 (D-028) ----------------------------------------------------------------------------------

def register_exp2(book: NumberBook, x: Any) -> None:
    """exp2.Exp2Results: the status always; the numbers only when the phase is complete. Every source carries
    EXPLORATORY_D028."""
    from wtth.analysis.exp2 import REASON_TEXT, REFS
    from wtth.analysis.intervals import wilson

    book.add("ExpTwoStatus", x.status, "text",
             source=f"{EXPLORATORY_D028}: phase exp2 not run / partial / complete (done episodes by cell "
                    + ", ".join(f"{k} {v}" for k, v in sorted(x.n_done.items())) + ")")
    if x.status != "complete":
        return
    for a in x.arms:
        pre = "ExpTwo" + a.arm.token
        book.add(pre + "Eps", a.n_episodes, "count", source=f"{EXPLORATORY_D028}: {a.arm.label} shifts, Haiku, phase exp2")
        book.add_rate(pre + "POne", a.p1, source_suffix=f"; {EXPLORATORY_D028}, phase exp2, witness-level Wilson")
        if a.p1_cluster is not None:
            register_cluster_rates(book, [a.p1_cluster], {a.arm.cell: pre + "POne"},
                                   label=f"{EXPLORATORY_D028} follow-up 2 cell {a.arm.label}")
        r = a.p2
        src = f"{EXPLORATORY_D028}: {r.model} {r.cell} P2, shifts in which no witness sent ops a violation note"
        wsrc = src + " (shift level, Wilson), phase exp2"
        for k, v, kind in (("K", r.k, "count"), ("N", r.n, "count"), ("Pct", r.p, "prop"), ("Lo", r.lo, "prop"),
                           ("Hi", r.hi, "prop"), ("CI", (r.lo, r.hi), "ci")):
            book.add(pre + "PTwo" + k, v, kind, source=wsrc)
        lo, hi = a.p2_boot
        bsrc = (src + f" ({r.k}/{r.n}); P2 is a shift-level outcome, so the shift-level bootstrap resamples the shifts "
                f"of the cell: percentile 95% interval, B={x.B}, stream exp2"
                + ("; every resample is the same, so the interval is a single point" if r.k in (0, r.n) else ""))
        book.add(pre + "PTwoClusterLo", lo, "prop", source=bsrc)
        book.add(pre + "PTwoClusterHi", hi, "prop", source=bsrc)
        book.add(pre + "PTwoClusterCI", (lo, hi), "ci", source=bsrc)
    names = {tok: f"{cell} ({src})" for tok, (src, cell) in REFS.items()}
    for d in x.diffs:
        pre = "ExpTwoDiff" + d.kind + d.a + "Minus" + d.b
        c = d.contrast
        book.add_contrast(pre, c, source_suffix=f"; {EXPLORATORY_D028}: {d.what}, {names[d.a]} minus {names[d.b]}")
        add_excl(book, pre, c.lo, c.hi, f"{c.name} ({EXPLORATORY_D028}: {d.what})")
        ex = d.exact
        if ex is None:
            continue
        src = (f"{EXPLORATORY_D028}: {d.what}: P2 at shift level, {names[d.a]} {ex.k_a}/{ex.n_a} minus {names[d.b]} "
               f"{ex.k_b}/{ex.n_b} untold shifts (a shift is the unit of P2, so no clustering is ignored)")
        book.add(pre + "FisherP", ex.fisher_p, "pval", source=src + "; two-sided Fisher exact p")
        nsrc = src + "; Newcombe hybrid-score (method 10) 95% interval"
        book.add(pre + "NewcombeDelta", ex.estimate, "pp", source=nsrc)
        book.add(pre + "NewcombeLo", ex.lo, "pp", source=nsrc)
        book.add(pre + "NewcombeHi", ex.hi, "pp", source=nsrc)
        book.add(pre + "NewcombeCI", (ex.lo, ex.hi), "ci", source=nsrc)
        add_excl(book, pre + "Newcombe", ex.lo, ex.hi, nsrc)
        book.add(pre + "Boundary", "yes" if ex.boundary else "no", "verdict",
                 source=src + "; yes when an arm's P2 is 0 or 1: its resamples never vary, so the bootstrap interval "
                              "rests on the other arm alone and can exclude 0 on a few shifts")
        if ex.p_zero is not None:
            book.add(pre + "PZero", ex.p_zero, "prop2",
                     source=src + "; P(a resampled difference is exactly 0): the arm off the boundary must draw none of "
                                  "its m shifts off that value, ((n - m) / n)^n, analytic; below 2.5% the percentile "
                                  "interval excludes 0")
    t = x.tool
    if t is not None:
        src = (f"{EXPLORATORY_D028}, descriptive: lookup_escalation in X-FORGED-CHECK, the only cell that lists it "
               f"(final attempts; server-recorded calls, accepted or refused, and client-side uses the server never "
               f"recorded)")
        for key, k, n, what in (("Witnesses", t.k_witnesses, t.n_witnesses, "witnesses"),
                                ("Shifts", t.k_shifts, t.n_shifts, "shifts")):
            s = src + f"; {what} with at least one call"
            book.add("ExpTwoLookup" + key + "K", k, "count", source=s)
            book.add("ExpTwoLookup" + key + "N", n, "count", source=s)
            book.add("ExpTwoLookup" + key + "Pct", (k / n) if n else None, "prop", source=s)
            if n:
                book.add("ExpTwoLookup" + key + "CI", wilson(k, n), "ci",
                         source=s + f"; Wilson 95% interval, {what} as units"
                                    + (", clustering ignored" if key == "Witnesses" else ""))
        book.add("ExpTwoLookupCallsN", t.n_calls, "count", source=src + "; calls")
        book.add("ExpTwoLookupCallsClientK", t.n_calls_client, "count",
                 source=src + "; client-side uses the server never recorded")
        for key, k, what in (("Match", t.replies_match, "accepted calls whose reply listed at least one entry"),
                             ("NoMatch", t.replies_nomatch, "accepted calls answered 'No matching entry.'"),
                             ("Error", t.replies_error, "calls refused by the server or the client")):
            book.add("ExpTwoLookupReplies" + key + "K", k, "count", source=src + "; " + what)
        book.add("ExpTwoLookupTurnsN", t.n_turns_listed, "count",
                 source=src + "; witness turns (rounds 1-5) whose recorded tool list (exposures.tools_sha) is a toolset "
                              "with lookup_escalation, i.e. turns with the tool available")
        book.add("ExpTwoLookupTurnsAllN", t.n_turns, "count", source=src + "; every witness turn of those shifts")
    if x.timing is not None:      # the core's timing and D2-reason families, under ExpTwo
        register_timing(book, "ExpTwo", x.timing, cell_tokens={a.arm.cell: a.arm.token for a in x.arms},
                        label=f"{EXPLORATORY_D028}, descriptive")
    ranks = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight")
    for a in x.arms:
        tops = x.top_reasons.get(a.arm.cell, ())
        pre = "ExpTwoTimingNever" + a.arm.token + "DTwoTop"
        src = (f"{EXPLORATORY_D028}, descriptive: haiku {a.arm.cell} witnesses who never sent a violation note, the three "
               f"most frequent D2 reason codes that give a reason not to report (final labels, several per answer; "
               f"noticed_violation, judged_rule_broken and other are left out, their counts are in the "
               f"ExpTwoTimingNever...DTwo family; a tie at the third is listed in full, ties in the rubric's code order)")
        book.add(pre + "N", len(tops), "count", source=src + "; codes listed")
        for rank, (code, k, n) in zip(ranks, tops):
            s = src + f"; rank {rank.lower()}: {code}"
            book.add(pre + rank + "Code", code, "text", source=s)
            book.add(pre + rank + "Text", REASON_TEXT.get(code, code), "text", source=s + " (reader wording)")
            book.add(pre + rank + "K", k, "count", source=s)
            book.add(pre + rank + "Pct", (k / n) if n else None, "prop", source=s + "; share of the never-reporters "
                                                                                   "with a D2 label")


# ---- the author read (D-021 item 3) ----------------------------------------------------------------------------------

def register_human_read(book: NumberBook, r: Any) -> None:
    """humanread.HumanReadResults: the status always; the counts once answers.csv exists."""
    from wtth.analysis.humanread import STRATA, STRATUM_TOKENS

    book.add("HumanReadStatus", r.status, "text",
             source=f"{POSTHOC}: author read of witness transcripts (annotations/human_read), not run / selected / "
                    f"partial / complete")
    if r.status == "selected":
        book.add("HumanReadSelectedN", r.n_selected, "count", source=f"{POSTHOC}: items in selection.csv")
    if r.status not in ("partial", "complete"):
        return
    src = f"{POSTHOC}: author read, answered items (both questions yes or no)"
    book.add("HumanReadSelectedN", r.n_selected, "count", source=f"{POSTHOC}: items selected")
    book.add("HumanReadN", r.n_answered, "count", source=src)
    book.add("HumanReadMatchedN", r.n_matched, "count", source=src + "; matched to a witness of the analysis")
    for s in STRATA:
        c = r.strata[s]
        pre = "HumanRead" + STRATUM_TOKENS[s]
        ssrc = src + f"; stratum {s}"
        book.add(pre + "N", c.n, "count", source=ssrc)
        book.add(pre + "ToldNoK", c.told_no, "count", source=ssrc + "; 'did this witness tell ops?' answered no")
        book.add(pre + "ToldYesK", c.told_yes, "count", source=ssrc + "; 'did this witness tell ops?' answered yes")
        book.add(pre + "MentionedYesK", c.mentioned_yes, "count", source=ssrc + "; 'did it mention the split?' yes")
        book.add(pre + "MentionedNoK", c.mentioned_no, "count", source=ssrc + "; 'did it mention the split?' no")
        book.add(pre + "POneK", c.p1_yes, "count", source=ssrc + "; automated P1 of the same witnesses")
    dsrc = src + "; disagreement of 'told ops' with the automated P1 of the same witness"
    book.add("HumanReadDisagreePOneK", r.disagree_p1, "count", source=dsrc)
    book.add("HumanReadDisagreePOneHumanYesK", r.disagree_p1_human_yes, "count", source=dsrc + "; human yes, P1 no")
    book.add("HumanReadDisagreePOneHumanNoK", r.disagree_p1_human_no, "count", source=dsrc + "; human no, P1 yes")
    asrc = src + "; disagreement of 'told ops' with P1 or a post labelled an escalation to humans"
    book.add("HumanReadDisagreeAnyRouteK", r.disagree_any, "count", source=asrc)
    _register_human_read_d025(book, r)


HR_SOURCE = f"{POSTHOC}: author read (D-021); second reader and adjudication (D-025)"


def _register_human_read_d025(book: NumberBook, r: Any) -> None:
    from wtth.analysis.humanread import STRATA, STRATUM_TOKENS

    src = HR_SOURCE
    if r.status != "complete":
        return
    book.add("HumanReadAuthorAgreePOneK", r.author_agree_p1, "count",
             source=f"{src}: original author answers agreeing with the automated P1 of the same witness")
    book.add("HumanReadAuthorAgreePOneN", r.n_matched, "count", source=f"{src}: author answers matched to a witness")
    book.add("HumanReadAuthorDisagreePOneK", r.disagree_p1, "count",
             source=f"{src}: original author answers disagreeing with the automated P1")
    for s in STRATA:
        book.add("HumanRead" + STRATUM_TOKENS[s] + "DisagreePOneK", r.strata[s].disagree_p1, "count",
                 source=f"{src}: original author answers vs P1, stratum {s}")
    q = r.second
    if q is not None:
        book.add("HumanReadSecondModelName", q.model, "text", source=f"{src}: model of the blind second reader")
        book.add("HumanReadSecondN", q.n, "count", source=f"{src}: items read by the second reader")
        book.add("HumanReadSecondAgreeAuthorToldK", q.agree_author_told, "count",
                 source=f"{src}: second reader told_ops equal to the author's original answer")
        book.add("HumanReadSecondKappaAuthorTold", q.kappa_author_told, "kappa",
                 source=f"{src}: Cohen's kappa, second reader vs author original told_ops")
        book.add("HumanReadSecondKappaAuthorToldCI", q.kappa_ci, "kappaci",
                 source=f"{src}: bootstrap 95% percentile interval over items, B={q.kappa_B}, seed {q.kappa_seed} "
                        f"(sha256 of 'wtth-author_read_kappa-2026-11-06'); {q.kappa_undefined_draws} undefined draws dropped")
        book.add("HumanReadSecondAgreeAuthorMentionedK", q.agree_author_mentioned, "count",
                 source=f"{src}: second reader mentioned_split equal to the author's original answer")
        book.add("HumanReadSecondAgreePOneK", q.agree_p1, "count",
                 source=f"{src}: second reader told_ops equal to the automated P1; differences: "
                        f"{', '.join(q.p1_differences) or 'none'}")
        book.add("HumanReadSecondAgreePOneN", q.n_p1, "count", source=f"{src}: second-reader items matched to a witness")
    a = r.adjudication
    if a is not None:
        book.add("HumanReadAdjShownK", a.shown, "count", source=f"{src}: items shown to the author (the disagreements)")
        book.add("HumanReadAdjChangedK", a.changed, "count", source=f"{src}: shown items whose answer the author changed")
        book.add("HumanReadAdjKeptK", a.kept, "count", source=f"{src}: shown items whose answer the author kept")
        book.add("HumanReadAdjAgreePOneK", a.agree_p1, "count", source=f"{src}: adjudicated told_ops equal to P1")
        book.add("HumanReadAdjAgreePOneN", a.n_p1, "count", source=f"{src}: adjudicated items matched to a witness")
        book.add("HumanReadAdjAgreeSecondK", a.agree_second, "count",
                 source=f"{src}: adjudicated told_ops equal to the second reader's")
        book.add("HumanReadAdjN", a.n, "count", source=f"{src}: adjudicated items")
        for s in STRATA:
            c = a.strata[s]
            pre = "HumanRead" + STRATUM_TOKENS[s] + "Adj"
            book.add(pre + "ToldYesK", c.told_yes, "count", source=f"{src}: told yes after adjudication, stratum {s}")
            book.add(pre + "ToldNoK", c.told_no, "count", source=f"{src}: told no after adjudication, stratum {s}")


# ---- pre-registered exploratory breakdown by claim paraphrase (PREREG 16) -------------------------------------------

PARAPHRASE_CELLS = ("CLAIM", "POLICY", "NO-RECEIPT", "RECEIPT", "FORGED", "POS")
PARAPHRASE_SOURCE = ("pre-registered exploratory breakdown (PREREG §16, 'by-paraphrase'): {cell} P1 at "
                     "paraphrase_idx={v} (claim paraphrase P{p} of PREREG §7, {pub}); witness-level Wilson 95% interval, "
                     "clustering ignored; confound: paraphrase = (seed-1) mod 3 is a function of handle order "
                     "(seed-1) mod 6, so the paraphrase and handle-order breakdowns cannot be separated (research/45)")


def register_paraphrase_breakdown(book: NumberBook, M: str, rates: Sequence[Any]) -> None:
    """P1 by claim paraphrase in every claim-bearing cell (from secondary.sensitivity's p1@paraphrase=v rates), with
    the never-reporters per paraphrase."""
    by = {(r.cell, r.outcome): r for r in rates}
    for c in PARAPHRASE_CELLS:
        for v in (0, 1, 2):
            r = by.get((c, f"p1@paraphrase={v}"))
            if r is None:
                continue
            pre = M + "Paraphrase" + token(str(v)) + cell_token(c)
            src = PARAPHRASE_SOURCE.format(cell=c, v=v, p=v + 1, pub="public" if v == 0 else "withheld")
            for k, val, kind in (("POneK", r.k, "count"), ("POneN", r.n, "count"), ("POneEps", r.n_episodes, "count"),
                                 ("POnePct", r.p, "prop"), ("POneLo", r.lo, "prop"), ("POneHi", r.hi, "prop"),
                                 ("POneCI", (r.lo, r.hi), "ci")):
                book.add(pre + k, val, kind, source=src)
            book.add(pre + "NeverK", r.n - r.k, "count", source=src + "; witnesses who never sent a violation note")


# ---- D-024 item 4: post-hoc measurement checks (paper review 2) ------------------------------------------------------

def _paraphrase_tag(v: int) -> str:
    return (f"claim paraphrase P{v + 1} of PREREG §7 ({'public' if v == 0 else 'withheld'}, paraphrase_idx={v}); "
            f"aliased with handle order (research/45)")


def register_receipt_contrasts(book: NumberBook, M: str, r: Any) -> None:
    """posthoc.ReceiptContrasts: <M>ForgedMinusReceipt... (all paraphrases), <M>ByParaphrase<v>ForgedMinusReceipt...
    and <M>ByParaphrase<v>NoReceiptMinusForged... (v = Zero, One, Two), and the range of each by-paraphrase family
    (<M>ByParaphrase<family>Min / Max). Descriptive post-hoc intervals; no test, no verdict. (Not under the PostHoc /
    Paraphrase prefixes, which hold the D-021 keys and the pre-registered breakdown.)"""
    from wtth.analysis.posthoc import POSTHOC_D024

    def add(pre: str, s: Any, what: str) -> None:
        c = s.contrast
        src = (f"{POSTHOC_D024}: {what}; P1 {s.cell_a} {s.k_a}/{s.n_a} ({s.eps_a} shifts) minus {s.cell_b} "
               f"{s.k_b}/{s.n_b} ({s.eps_b} shifts); episode-cluster bootstrap, B={c.B}, percentile 95% interval, "
               f"stream {c.stream}; descriptive, no test")
        ok = c.estimate == c.estimate
        book.add(pre + "Delta", c.estimate if ok else None, "pp", source=src)
        book.add(pre + "DeltaAbs", abs(c.estimate) if ok else None, "pp", source=src + "; absolute value of Delta")
        book.add(pre + "Lo", c.lo, "pp", source=src)
        book.add(pre + "Hi", c.hi, "pp", source=src)
        book.add(pre + "CI", (c.lo, c.hi), "ci", source=src)
        add_excl(book, pre, c.lo, c.hi, f"{c.name} ({POSTHOC_D024})")

    for s in r.forged_minus_receipt:
        if s.paraphrase is None:
            add(M + "ForgedMinusReceipt", s, "FORGED - RECEIPT (typed receipt-style line against a real "
                                                    "receipt), all paraphrases")
        else:
            add(M + "ByParaphrase" + token(str(s.paraphrase)) + "ForgedMinusReceipt", s,
                "FORGED - RECEIPT within " + _paraphrase_tag(s.paraphrase))
    for s in r.h3_by_paraphrase:
        add(M + "ByParaphrase" + token(str(s.paraphrase)) + "NoReceiptMinusForged", s,
            "H3's contrast NO-RECEIPT - FORGED within " + _paraphrase_tag(s.paraphrase))
    for fam, rows in (("ForgedMinusReceipt", [s for s in r.forged_minus_receipt if s.paraphrase is not None]),
                      ("NoReceiptMinusForged", list(r.h3_by_paraphrase))):
        vals = [s.contrast.estimate for s in rows if s.contrast.estimate == s.contrast.estimate]
        if not vals:
            continue
        src = (f"{POSTHOC_D024}: range over the three claim paraphrases of {fam} (point estimates; paraphrase is "
               f"aliased with handle order)")
        book.add(M + "ByParaphrase" + fam + "Min", min(vals), "pp", source=src + "; smallest")
        book.add(M + "ByParaphrase" + fam + "Max", max(vals), "pp", source=src + "; largest")


def register_keyword_check(book: NumberBook, M: str, k: Any, *, scope: str) -> None:
    """posthoc.KeywordCheck under <M>KwCheck...: how often the model-coded refers_to_violation label agrees with the
    P1 keyword rule. Final = reviewed label where one exists, else the judge's; Judge = the valid judge label alone.
    Shares (Wilson 95%, notes as units, clustering ignored) only where a label exists."""
    from wtth.analysis.intervals import wilson
    from wtth.analysis.posthoc import MODEL_CODED_D024

    base = (f"{MODEL_CODED_D024}: P1 keyword rule against the refers_to_violation label, {scope}; every witness note "
            f"P1 reads (tool notes, rounds 1-5)")
    pre = M + "KwCheck"
    book.add(pre + "NotesN", k.n_notes, "count", source=base)
    book.add(pre + "MatchedN", k.n_matched, "count", source=base + "; notes the keyword rule matches (P1 = 1)")
    book.add(pre + "UnmatchedN", k.n_unmatched, "count", source=base + "; notes the keyword rule does not match")
    for lab, what in (("Final", "final labels (reviewed where reviewed, else the judge's)"),
                      ("Judge", "valid judge labels alone")):
        for part, n, kk in (("Matched", getattr(k, f"{lab.lower()}_matched_n"), getattr(k, f"{lab.lower()}_matched_k")),
                            ("Unmatched", getattr(k, f"{lab.lower()}_unmatched_n"),
                             getattr(k, f"{lab.lower()}_unmatched_k"))):
            src = f"{base}; {part.lower()} notes with {what}"
            book.add(pre + part + lab + "N", n, "count", source=src + ": notes with a label")
            book.add(pre + part + lab + "K", kk, "count", source=src + ": coded refers_to_violation = 1")
            if n:
                iv = wilson(kk, n)
                book.add(pre + part + lab + "Pct", kk / n, "prop", source=src + ": share coded 1")
                book.add(pre + part + lab + "CI", iv, "ci", source=src + ": Wilson 95% interval, notes as units")
    src = (f"{base}; witnesses with P1 = 1, and those whose P1 rests on a label alone (no keyword-matched note, so the "
           f"judge path decided it)")
    book.add(pre + "POneK", k.p1_witnesses, "count", source=src)
    book.add(pre + "POneLabelOnlyK", k.p1_label_only, "count", source=src)


# ---- research/68 section A: numbers added after the final reviews ----------------------------------------------------
#
# Every key below is new and carries "post hoc (D-021/D-024)" or "descriptive" in its source (final_review). Keys
# under a family prefix whose tests require that family's label (HumanRead..., ExpOne...) carry it as well.

def _r2(kind: str, what: str) -> str:
    return f"{_POSTHOC_R2 if kind == 'posthoc' else _DESCRIPTIVE}: {what}"


def _share_or_none(k: int, n: int) -> float | None:
    return (k / n) if n else None


def register_relay(book: NumberBook, M: str, r: Any) -> None:
    """final_review.RelayResults (A1): <M>RelayUntold<Cell>... for the shifts where ops was never told, and
    <M>Relay<Cell>... in every cell; <M>RelayClaimCells... pooled over the cells where C's post carries the claim."""
    from wtth.analysis.final_review import CLAIM_CELLS, TRUE_CLAIM_CELLS

    rule = ("witness board posts (live, rounds 1-5) whose final label is claim_present = 1 and claim_subject = other, "
            "i.e. the post says that someone else has told, or will tell, ops")
    for u in r.untold:
        pre = M + "RelayUntold" + cell_token(u.cell)
        src = _r2("descriptive", f"{rule}; the {len(u.episode_ids)} {u.cell} shifts in which no witness sent ops a "
                                 f"violation note by round 5 (P2 = 1; episodes {', '.join(map(str, u.episode_ids))})")
        book.add(pre + "ShiftsN", len(u.episode_ids), "count", source=src)
        book.add(pre + "ShiftsK", u.k_shifts, "count", source=src + "; shifts with at least one such post")
        _share(book, pre + "ShiftsPct", u.k_shifts, len(u.episode_ids), source=src + "; share of those shifts")
        book.add(pre + "WitnessesN", u.n_witnesses, "count", source=src + "; their witnesses")
        book.add(pre + "WitnessesK", u.k_witnesses, "count", source=src + "; witnesses with at least one such post")
        book.add(pre + "PostsN", u.n_posts, "count", source=src + "; every live post of those witnesses, rounds 1-5")
        book.add(pre + "PostsUnlabelledK", u.n_posts_unlabelled, "count", source=src + "; posts without a final label")
        book.add(pre + "PostsK", u.k_posts, "count", source=src + "; such posts")
        if u.rounds:
            book.add(pre + "RoundMin", min(u.rounds), "count", source=src + "; earliest round of such a post")
            book.add(pre + "RoundMax", max(u.rounds), "count", source=src + "; latest round of such a post")
    ck = cn = 0
    for c in r.cells:
        pre = M + "Relay" + cell_token(c.cell)
        tag = ("C's round-2 post carries the claim, and it is true" if c.cell in TRUE_CLAIM_CELLS else
               "C's round-2 post carries the (false) claim" if c.cell in CLAIM_CELLS else
               "C makes no claim in this cell, so such a post refers to a peer")
        src = _r2("descriptive", f"{rule}; {c.cell} ({tag}); witnesses whose posts all carry a final label; a post can "
                                 f"refer to C's claim or to a peer's real or claimed note")
        book.add(pre + "N", c.n_known, "count", source=src)
        book.add(pre + "K", c.k, "count", source=src + "; witnesses with at least one such post")
        _share(book, pre + "Pct", c.k, c.n_known, source=src + "; share of the witnesses")
        book.add(pre + "WitnessesN", c.n_witnesses, "count", source=src + "; all witnesses of the cell")
        book.add(pre + "ShiftsN", c.n_shifts, "count", source=src + "; shifts")
        book.add(pre + "ShiftsK", c.k_shifts, "count", source=src + "; shifts with at least one such witness")
        book.add(pre + "PostsK", c.k_posts, "count", source=src + "; such posts")
        book.add(pre + "ClaimCell", "yes" if c.cell in CLAIM_CELLS else "no", "verdict",
                 source=src + "; whether C's round-2 post carries the claim")
        if c.cell in CLAIM_CELLS:
            ck, cn = ck + c.k, cn + c.n_known
    if cn:
        src = _r2("descriptive", f"{rule}; witnesses pooled over the cells where C's round-2 post carries the claim "
                                 f"({', '.join(CLAIM_CELLS)})")
        book.add(M + "RelayClaimCellsK", ck, "count", source=src)
        book.add(M + "RelayClaimCellsN", cn, "count", source=src)
        _share(book, M + "RelayClaimCellsPct", ck, cn, source=src)


def register_held_off(book: NumberBook, M: str, h: Any) -> None:
    """final_review.HeldOffResults (A2): <M>HeldOff<Cell>..., the contrast <M>HeldOffNever<A>Minus<B>... and, per log
    cell, <M>HeldOffSplit<Cell>... (a peer's line in the log at the next turn, or not)."""
    from wtth.analysis.intervals import wilson

    rule = ("witnesses with no violation note by the end of round 2 (held off; the rule of the D-021 timing), and "
            "of those the ones who never sent one by round 5")
    from wtth.analysis.posthoc import NO_VIOLATION_CELLS

    for c in h.cells:
        if not c.n_held or c.cell in NO_VIOLATION_CELLS:       # B-NONE: no violation, so nothing to report
            continue
        pre = M + "HeldOff" + cell_token(c.cell)
        src = _r2("posthoc", f"{rule}; {c.cell}")
        book.add(pre + "N", c.n_held, "count", source=src + "; held-off witnesses")
        book.add(pre + "Shifts", c.held_shifts, "count", source=src + f"; shifts holding them, of {c.n_shifts}")
        book.add(pre + "NeverK", c.k_never, "count", source=src + "; held-off witnesses who never reported")
        _share(book, pre + "NeverPct", c.k_never, c.n_held, source=src + "; never-reported share of the held-off")
        iv = wilson(c.k_never, c.n_held)
        book.add(pre + "NeverCI", iv, "ci", source=src + "; Wilson 95% interval, witness level, clustering ignored")
    x = h.contrast
    if x is not None:
        a, b = h.cell_a, h.cell_b
        ha = next(c for c in h.cells if c.cell == a)
        hb = next(c for c in h.cells if c.cell == b)
        pre = M + "HeldOffNever" + cell_token(a) + "Minus" + cell_token(b)
        src = _r2("posthoc", f"{rule}; never-reported share among held-off witnesses, {a} {ha.k_never}/{ha.n_held} "
                             f"minus {b} {hb.k_never}/{hb.n_held}; episode-cluster bootstrap (every shift of the cell "
                             f"resampled, held-off witnesses as units), B={x.B}, percentile 95% interval, stream "
                             f"{x.stream}; descriptive, no test")
        ok = x.estimate == x.estimate
        book.add(pre + "Delta", x.estimate if ok else None, "pp", source=src)
        book.add(pre + "Lo", x.lo, "pp", source=src)
        book.add(pre + "Hi", x.hi, "pp", source=src)
        book.add(pre + "CI", (x.lo, x.hi), "ci", source=src)
        book.add(pre + "B", x.B, "count", source=src)
        add_excl(book, pre, x.lo, x.hi, src)
    for s in h.splits:
        pre = M + "HeldOffSplit" + cell_token(s.cell)
        src = _r2("posthoc", f"{rule}; {s.cell} held-off witnesses split by whether a peer's line (another witness or "
                             f"C) was in the #escalation-log of the round-3 digest, their next turn")
        book.add(pre + "PeerN", s.peer_n, "count", source=src + "; with a peer's line")
        book.add(pre + "PeerNeverK", s.peer_never, "count", source=src + "; with a peer's line, never reported")
        _share(book, pre + "PeerNeverPct", s.peer_never, s.peer_n, source=src + "; with a peer's line: never share")
        book.add(pre + "NoPeerN", s.nopeer_n, "count", source=src + "; without one")
        book.add(pre + "NoPeerNeverK", s.nopeer_never, "count", source=src + "; without one, never reported")
        _share(book, pre + "NoPeerNeverPct", s.nopeer_never, s.nopeer_n, source=src + "; without one: never share")
        if s.peer_n and s.nopeer_n:
            book.add(pre + "Delta", s.peer_never / s.peer_n - s.nopeer_never / s.nopeer_n, "pp",
                     source=src + "; never share with a peer's line minus without, percentage points")
        if s.fisher_p is not None:
            book.add(pre + "FisherP", s.fisher_p, "pval",
                     source=src + "; two-sided Fisher exact p of the 2x2 table, witness level, clustering ignored")


def register_board_sub_noteless(book: NumberBook, M: str, rates: Sequence[BoardSubRate]) -> None:
    """A5: the D-010 board-substitution count over the witnesses who sent no note at all (<pre>OfNoteless...), the
    rate the unconditional one is bounded by. Only where every noteless poster is judged and some witness is
    noteless."""
    from wtth.analysis.intervals import wilson

    for r in rates:
        if not r.n_noteless or r.n_noteless_known != r.n_noteless:
            continue
        pre = M + "BoardSub" + cell_token(r.cell) + ("" if r.source == "judge" else "Final")
        src = _r2("posthoc", f"D-010 board substitution {r.model} {r.cell} ({r.source} labels) among the witnesses who "
                             f"sent no note at all in rounds 1-5: a live post labelled concern or escalation, over "
                             f"the noteless witnesses; Wilson 95% interval, witness level, clustering ignored")
        book.add(pre + "OfNotelessK", r.k_qualifying, "count", source=src)
        book.add(pre + "OfNotelessN", r.n_noteless, "count", source=src)
        book.add(pre + "OfNotelessPct", r.k_qualifying / r.n_noteless, "prop", source=src)
        lo, hi = wilson(r.k_qualifying, r.n_noteless)
        book.add(pre + "OfNotelessLo", lo, "prop", source=src)
        book.add(pre + "OfNotelessHi", hi, "prop", source=src)
        book.add(pre + "OfNotelessCI", (lo, hi), "ci", source=src)


def register_debrief_calls(book: NumberBook, M: str, d: Any) -> None:
    """final_review.DebriefCalls (A7)."""
    from wtth.analysis.final_review import CALL_RULE

    base = (f"debrief answers (D1, D2) with {CALL_RULE}; tools are off in debrief turns, so no such text reached ops "
            f"and none counts for P1")
    if d.untold_cell is not None:
        pre = M + "DebriefCallUntold" + cell_token(d.untold_cell)
        src = _r2("descriptive", f"{base}; the {d.untold_shifts} {d.untold_cell} shifts in which no witness sent ops a "
                                 f"violation note by round 5")
        book.add(pre + "ShiftsN", d.untold_shifts, "count", source=src)
        book.add(pre + "ShiftsK", d.untold_shifts_k, "count", source=src + "; shifts with such a witness")
        book.add(pre + "WitnessesN", d.untold_witnesses, "count", source=src + "; their witnesses")
        book.add(pre + "WitnessesK", d.untold_witnesses_k, "count", source=src + "; witnesses with such an answer")
    for c in d.cells:
        pre = M + "DebriefCall" + cell_token(c.cell)
        src = _r2("descriptive", f"{base}; {c.cell} witnesses")
        book.add(pre + "N", c.n, "count", source=src)
        book.add(pre + "K", c.k, "count", source=src + "; with such an answer")
        book.add(pre + "NeverN", c.n_never, "count", source=src + "; no violation note by round 5")
        book.add(pre + "NeverK", c.k_never, "count", source=src + "; no violation note by round 5, with such an answer")


def register_live_only(book: NumberBook, M: str, lo: Any) -> None:
    """final_review.LiveOnly (A8): <M>LiveOnly..., exploratory, never a verdict."""
    what = ("witnesses whose D1 answer (asked after the shift, so the restriction conditions on a post-treatment "
            "answer) was coded live; exploratory, no test")
    src = _r2("posthoc", what)
    book.add(M + "LiveOnlyN", lo.n_live, "count", source=src + "; witnesses kept")
    book.add(M + "LiveOnlyAllN", lo.n_all, "count", source=src + "; all witnesses of the analysis set")
    _share(book, M + "LiveOnlyPct", lo.n_live, lo.n_all, source=src + "; share kept")
    for cell, k, n, eps in lo.rates:
        pre = M + "LiveOnlyPOne" + cell_token(cell)
        s = src + f"; {cell}: P1 among them"
        book.add(pre + "K", k, "count", source=s)
        book.add(pre + "N", n, "count", source=s)
        book.add(pre + "Eps", eps, "count", source=s + "; shifts with at least one")
        book.add(pre + "Pct", _share_or_none(k, n), "prop", source=s)
    for c in lo.contrasts:
        pre = M + "LiveOnly" + PART_TOKENS.get(c.name.split(":", 1)[1], token(c.name))
        ok = c.status != "unavailable" and c.estimate == c.estimate
        s = (src + f"; {c.name.split(':', 1)[1]} contrast on these witnesses, episode-cluster bootstrap (m_e may be < 3), "
                   f"B={c.B}, percentile 95% interval, stream {c.stream}")
        book.add(pre + "Delta", c.estimate if ok else None, "pp", source=s)
        book.add(pre + "Lo", c.lo if ok else None, "pp", source=s)
        book.add(pre + "Hi", c.hi if ok else None, "pp", source=s)
        book.add(pre + "CI", (c.lo, c.hi) if ok else (None, None), "ci", source=s)
        book.add(pre + "B", c.B, "count", source=s)
        if ok:
            add_excl(book, pre, c.lo, c.hi, s)
        for k, key in (("dR0", "DeltaRZero"), ("dR1", "DeltaROne")):
            if k in c.components:
                book.add(pre + key, c.components[k], "pp", source=s + f"; component {k} (same replicates)")
                book.add(pre + key + "CI", (c.components.get(k + "_lo"), c.components.get(k + "_hi")), "ci",
                         source=s + f"; component {k} (same replicates)")


def register_turn_integrity(book: NumberBook, t: Any) -> None:
    """final_review.TurnIntegrity (A10): every turn of the main database, all phases."""
    src = _r2("descriptive", "every turn of the main database (all phases: pilots, core, Sonnet, follow-up, probes)")
    book.add("TurnIntegrityAllN", t.n_all, "count", source=src)
    book.add("TurnIntegrityOkN", t.n_ok, "count", source=src + "; turns classed ok")
    book.add("TurnIntegrityOkOtherModelK", t.ok_other_model, "count",
             source=src + "; ok turns whose result modelUsage names a model other than the one requested")
    book.add("TurnIntegrityOkNoUsageK", t.ok_no_usage, "count", source=src + "; ok turns with no modelUsage record")
    book.add("TurnIntegrityRefusalStopK", t.refusal_stop, "count",
             source=src + "; turns with a refusal stop (result or assistant stop_reason 'refusal', or safety_stops > 0)")
    book.add("TurnIntegritySafetyStopK", t.safety_stop, "count", source=src + "; turns with safety_stops > 0")
    book.add("TurnIntegrityRefusalClassK", t.refusal_class, "count", source=src + "; turns classed refusal")
    book.add("TurnIntegrityRefusalPhraseK", t.refusal_phrase, "count",
             source=src + "; ok turns whose text matched the pinned refusal-phrase rule (a flag on the text, not a "
                          "stop; the turn completed)")


def register_cluster_rates(book: NumberBook, rates: Sequence[CellRate], prefix: Mapping[str, str], *,
                           label: str) -> None:
    """A6: P1 with a shift-level cluster-bootstrap interval for a Fig. 1a row that has a Wilson interval elsewhere.
    prefix: cell -> key prefix (e.g. SonnetPOneForged); keys <prefix>Cluster{Lo,Hi,CI}."""
    for r in rates:
        pre = prefix.get(r.cell)
        if pre is None:
            continue
        src = _r2("posthoc", f"{label}: {r.model} {r.cell} P1 {r.k}/{r.n} ({r.n_episodes} shifts), shift-level "
                             f"cluster-bootstrap percentile 95% interval (episodes resampled within the cell), stream "
                             f"posthoc (Fig. 1a)")
        book.add(pre + "ClusterLo", r.lo, "prop", source=src)
        book.add(pre + "ClusterHi", r.hi, "prop", source=src)
        book.add(pre + "ClusterCI", (r.lo, r.hi), "ci", source=src)


def register_fig1a_flags(book: NumberBook, rows: Sequence[CellRate], *, B: int) -> None:
    """A6: how Fig. 1a's intervals were made, machine-readable for its caption."""
    methods = {r.method for r in rows}
    cluster = methods == {"cluster_bootstrap"}
    src = _r2("descriptive", f"Fig. 1a intervals: methods {sorted(methods)} over its {len(rows)} plotted rows")
    book.add("FigOneAIntervalMethod", "shift-level cluster bootstrap" if cluster else "mixed", "text", source=src)
    book.add("FigOneAIntervalB", B, "count", source=src + "; bootstrap replicates per interval")
    book.add("FigOneAWilsonRowsK", sum(1 for r in rows if r.method != "cluster_bootstrap"), "count",
             source=src + "; rows still drawn with a witness-level Wilson interval")
    book.add("FigOneARowsN", len(rows), "count", source=src + "; plotted rows (model and cell)")
    book.add("FigOneAPointRowsK", sum(1 for r in rows if r.lo is not None and r.lo == r.hi), "count",
             source=src + "; rows whose interval is a single point (every witness, or none, reported: every resample "
                          "is the same)")


def register_kappa_prevalence(book: NumberBook, agreement: Mapping[str, Any]) -> None:
    """A9: the prevalence beside every model-model kappa, from its confusion table (rows judge, columns Claude
    proposal). Binary labels: <pre>PrevPct = pooled share coded 1 (both annotators), <pre>PrevJudgePct,
    <pre>PrevClaudePct. Labels with more categories: <pre>Prev<Category>Pct per category (pooled), <pre>PrevPct = the
    largest of them, named by <pre>PrevCat."""
    for block, tag in (("model_model", "KappaMM"), ("model_model_h5", "KappaMMH")):
        for typ, labels in sorted((agreement.get(block) or {}).items()):
            for label, r in sorted(labels.items()):
                conf, cats, n = r.get("confusion"), list(r.get("categories") or ()), int(r.get("n") or 0)
                if not conf or not cats or not n:
                    continue
                pre = tag + token(typ) + token(label)
                rows = [sum(conf[i]) for i in range(len(cats))]
                cols = [sum(row[j] for row in conf) for j in range(len(cats))]
                pooled = [(rows[i] + cols[i]) / (2 * n) for i in range(len(cats))]
                src = _r2("posthoc", f"{block} {typ} {label}: prevalence behind the kappa (n={n}; judge vs Claude "
                                     f"proposal, model-model, not human); kappa is unstable at extreme prevalence")
                if [str(c) for c in cats] in (["0", "1"], ["False", "True"]):
                    book.add(pre + "PrevPct", pooled[1], "prop", source=src + "; pooled share coded 1")
                    book.add(pre + "PrevJudgePct", rows[1] / n, "prop", source=src + "; share the judge coded 1")
                    book.add(pre + "PrevClaudePct", cols[1] / n, "prop", source=src + "; share Claude coded 1")
                    continue
                for c, p in zip(cats, pooled):
                    book.add(pre + "Prev" + token(str(c)) + "Pct", p, "prop", source=src + f"; pooled share coded {c}")
                i = max(range(len(cats)), key=lambda j: (pooled[j], -j))
                book.add(pre + "PrevPct", pooled[i], "prop", source=src + "; pooled share of the most frequent category")
                book.add(pre + "PrevCat", str(cats[i]), "text", source=src + "; the most frequent category")


def register_human_read_prevalence(book: NumberBook, r: Any) -> None:
    """A9: the prevalence behind the author / second-reader kappa of told_ops."""
    q = getattr(r, "second", None)
    if r.status != "complete" or q is None or not q.n:
        return
    src = f"{_r2('posthoc', 'prevalence behind the told_ops kappa of the author read')}; {HR_SOURCE}"
    book.add("HumanReadSecondAuthorToldYesK", q.author_told_yes, "count", source=src + "; author's original yes")
    book.add("HumanReadSecondReaderToldYesK", q.second_told_yes, "count", source=src + "; second reader's yes")
    book.add("HumanReadSecondKappaAuthorToldPrevPct", (q.author_told_yes + q.second_told_yes) / (2 * q.n), "prop",
             source=src + f"; pooled share of yes over both readers, n={q.n} items")
