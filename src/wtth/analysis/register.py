"""Fill the NumberBook from the result records (plan 47 2.16). Every number the paper prints is registered here."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from wtth.analysis.numbers import (CELL_TOKENS, MODEL_TOKENS, NumberBook, cell_token, outcome_token, rate_prefix,
                                   token)
from wtth.analysis.results import (BoardSubRate, CellRate, Contrast, DescriptiveResults, Diff, EpisodeRate,
                                   HypothesisResults, SecondaryResults, SensitivityResults, SonnetResults)
from wtth.analysis.sets import AnalysisSet

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


def register_hypotheses(book: NumberBook, M: str, h: HypothesisResults) -> None:
    for name, c in (("HOne", h.h1), ("HTwo", h.h2), ("HThree", h.h3)):
        book.add_contrast(M + name, c)
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
    book.add(M + "StoppedAt", h.stopped_at or "none", "text", source="fixed sequence H1, H2, H3")
    pc = h.positive_control
    book.add(M + "PosCtrlGTwoGap", pc.get("g2_gap_points"), "pts", source=f"G2 pilot, phase {pc.get('g2_phase')} (pilot)")
    book.add(M + "PosCtrlCoreDiff", pc.get("core_diff"), "pp", source="core NEG - POS any-note episode share")
    book.add(M + "PosCtrlCoreCI", (pc.get("core_lo"), pc.get("core_hi")), "ci", source="Newcombe")
    for k in ("core_k_neg", "core_n_neg", "core_k_pos", "core_n_pos"):
        book.add(M + "PosCtrl" + token(k), pc.get(k), "count", source="core NEG vs POS any note")


def register_model(book: NumberBook, model: str, *, aset: AnalysisSet, w: Sequence[Any], e: Sequence[Any],
                   p1r: Sequence[CellRate], hyp: HypothesisResults | None, sec: SecondaryResults,
                   desc: DescriptiveResults, sens: SensitivityResults, power: Mapping[str, Any] | None) -> None:
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
        register_hypotheses(book, M, hyp)
    for c in desc.contrasts:
        book.add_contrast(M + "Desc" + contrast_key(c.name), c)
    for c in sens.contrasts:
        book.add_contrast(M + "Sens" + contrast_key(c.name), c)
    for extra in (sec.delta1_no_test_aware, sec.h2_p2):
        if extra is not None:
            book.add_contrast(M + "Sec" + contrast_key(extra.name), extra)
    for k, v in desc.predictions.items():
        book.add(M + "Pred" + token(k), "not available" if v is None else ("met" if v else "not met"), "verdict",
                 source="PREREG 12 recorded prediction checked descriptively")
    for cnt in sec.counts:
        book.add(M + "Count" + token(cnt.name) + cell_token(cnt.cell), cnt.value, "count",
                 source=f"{cnt.model} {cnt.cell} {cnt.name}")
    book.add(M + "IccPooled", sens.icc_pooled, "kappa", source="one-way ANOVA ICC(1), pooled within cells")
    for c, v in sens.icc_by_cell.items():
        book.add(M + "Icc" + cell_token(c), v, "kappa", source=f"ICC(1) {c}")
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
    book.add("ReviewReviewed", corr.get("reviewed"), "count", source="author review")
    book.add("ReviewShare", corr.get("share_reviewed"), "prop", source="author review")
    book.add("ReviewCorrectionRate", corr.get("rate"), "prop", source="author review: correct or use-judge")
    prec = agreement.get("precision_escalation_h3") or {}
    book.add("ReviewPrecisionEscalation", prec.get("precision"), "prop", source="judge escalation positives in H-3")
    blind = agreement.get("blind") or {}
    book.add("BlindStatus", blind.get("status", "not run"), "text", source="author-blind sample")
