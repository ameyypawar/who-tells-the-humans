"""LaTeX (booktabs) and CSV tables (plan 47 section 2.17). No matplotlib import here.

Every table is a bare `tabular` (the paper wraps it in its own table environment and needs \\usepackage{booktabs}).
Plain text is LaTeX-escaped; numbers are formatted by the helpers below (ROUND_HALF_UP; proportions as percent with one
decimal; differences in percentage points; \\ensuremath{-} for negatives; {,} thousands separators; kappa with two
decimals; USD as \\$x.xx). CSV files carry the same cells as plain text.

LEAD-REVIEW: the `book` argument of the table functions is accepted for the pinned signature but the formatting is
done here (NumberBook did not exist when this was written); the helpers follow plan 47 section 2.16 exactly.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from wtth.analysis.results import (BoardSubRate, CellRate, Contrast, CountRow, Diff, EpisodeRate, HypothesisResults,
                                   MeanRate, SecondaryResults)

CELL_ORDER = ("NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS", "B-NONE")
MINUS = r"\ensuremath{-}"
_TEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "<": r"\textless{}", ">": r"\textgreater{}",
        "|": r"\textbar{}", "−": MINUS, "′": r"\ensuremath{'}", "Δ": r"\ensuremath{\Delta}",
        "κ": r"\ensuremath{\kappa}", "–": "--", "—": "---", "·": r"\textperiodcentered{}"}


def latex_escape(s: str) -> str:
    return "".join(_TEX.get(ch, ch) for ch in str(s))


@dataclass(frozen=True, slots=True)
class Cell:
    tex: str
    csv: str


def text(s: Any) -> Cell:
    s = "" if s is None else str(s)
    return Cell(latex_escape(s), s)


def _dec(x: float, nd: int) -> str:
    q = Decimal(1).scaleb(-nd)
    d = Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP)
    if d == 0:
        d = abs(d)  # no negative zero
    return f"{d:.{nd}f}"


def _signed(plain: str) -> Cell:
    if plain.startswith("-"):
        return Cell(MINUS + plain[1:], plain)
    return Cell(plain, plain)


def pct(x: float | None, nd: int = 1) -> Cell:
    return Cell("--", "") if x is None else _signed(_dec(x * 100.0, nd))


def pp(x: float | None, nd: int = 1) -> Cell:
    return Cell("--", "") if x is None else _signed(_dec(x * 100.0, nd))


def ci(lo: float | None, hi: float | None, nd: int = 1, scale: float = 100.0) -> Cell:
    if lo is None or hi is None:
        return Cell("--", "")
    a, b = _signed(_dec(lo * scale, nd)), _signed(_dec(hi * scale, nd))
    return Cell(f"{a.tex} to {b.tex}", f"{a.csv} to {b.csv}")


def num(x: float | None, nd: int = 2) -> Cell:
    return Cell("--", "") if x is None else _signed(_dec(x, nd))


def integer(n: int | None) -> Cell:
    if n is None:
        return Cell("--", "")
    s = f"{int(n):,}"
    return Cell(s.replace(",", "{,}"), str(int(n)))


def kn(k: int | None, n: int | None) -> Cell:
    if k is None or n is None:
        return Cell("--", "")
    return Cell(f"{integer(k).tex}/{integer(n).tex}", f"{k}/{n}")


def usd(x: float | None) -> Cell:
    return Cell("--", "") if x is None else Cell(r"\$" + _dec(x, 2), "$" + _dec(x, 2))


@dataclass(frozen=True, slots=True)
class TableFiles:
    tex: Path
    csv: Path


def _tidy(rows: Sequence[Sequence[Cell] | None]) -> list[Sequence[Cell] | None]:
    """No leading, trailing or doubled rules (an empty section leaves no trace)."""
    out: list[Sequence[Cell] | None] = []
    for r in rows:
        if r is None and (not out or out[-1] is None):
            continue
        out.append(r)
    while out and out[-1] is None:
        out.pop()
    return out


def write_table(stem: Path, header: Sequence[str], rows: Sequence[Sequence[Cell] | None], *, align: str | None = None
                ) -> TableFiles:
    """Write <stem>.tex and <stem>.csv. A None row is a \\midrule (not written to the CSV)."""
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    ncol = len(header)
    align = align or ("l" + "r" * (ncol - 1))
    if len(align) != ncol:
        raise ValueError("align must have one letter per column")
    lines = [r"\begin{tabular}{" + align + "}", r"\toprule",
             " & ".join(latex_escape(h) for h in header) + r" \\", r"\midrule"]
    data = []
    rows = _tidy(rows)
    for r in rows:
        if r is None:
            lines.append(r"\midrule")
            continue
        if len(r) != ncol:
            raise ValueError(f"row has {len(r)} cells, header has {ncol}")
        lines.append(" & ".join(c.tex for c in r) + r" \\")
        data.append([c.csv for c in r])
    lines += [r"\bottomrule", r"\end{tabular}", ""]
    files = TableFiles(stem.with_suffix(".tex"), stem.with_suffix(".csv"))
    files.tex.write_text("\n".join(lines), encoding="utf-8")
    with open(files.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(list(header))
        w.writerows(data)
    return files


def cell_key(cell: str) -> int:
    return CELL_ORDER.index(cell) if cell in CELL_ORDER else len(CELL_ORDER)


# ---- hypotheses ------------------------------------------------------------------------------------------

_HYP_CELLS = {"H1": ("NONE", "CLAIM"), "H2": ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT"), "H3": ("NO-RECEIPT", "FORGED")}
_HYP_LABEL = {"H1": "NONE − CLAIM", "H2": "[NONE − CLAIM] − [NONE-R − NO-RECEIPT]",
              "H3": "NO-RECEIPT − FORGED"}


def decision_text(c: Contrast) -> str:
    """Reader wording (review M1), shared with the ...VerdictText macros: supported (null rejected), opposite to
    prediction (null rejected), not supported (null not rejected); no test outside the confirmatory sequence."""
    from wtth.analysis.text import verdict_text

    return verdict_text(c)


def tost_decision(verdict: str) -> str:
    """The exploratory TOST row: inside / not inside the margin, never 'equivalent' (review N3)."""
    if verdict == "equivalent":
        return "inside ±15 points (exploratory)"
    if verdict in ("not_equivalent", "not equivalent"):
        return "not inside ±15 points (exploratory)"
    return str(verdict).replace("_", " ")


def table_hypotheses(h: HypothesisResults, rates: Sequence[CellRate], book: Any, out_dir: Path) -> TableFiles:
    """H1-H3: cells (k/n, %), contrast in percentage points, 95% bootstrap interval, status, decision; then the
    H1 null rule and the exploratory TOST. `rates` are the P1 cell rates (outcome 'P1' when present)."""
    p1 = [r for r in rates if r.outcome == "P1"] or list(rates)
    by = {r.cell: r for r in p1}
    rows: list[Sequence[Cell] | None] = []
    for name, c in (("H1", h.h1), ("H2", h.h2), ("H3", h.h3)):
        cells = "; ".join(f"{k} {by[k].k}/{by[k].n} ({_dec(by[k].p * 100, 1)}%)" if k in by and by[k].p is not None
                          else f"{k} n/a" for k in _HYP_CELLS[name])
        ok = c.status != "unavailable"
        rows.append([text(name), text(_HYP_LABEL[name]), text(cells), pp(c.estimate) if ok else text("n/a"),
                     ci(c.lo, c.hi) if ok else text("n/a"), text(c.status), text(decision_text(c))])
    rows.append(None)
    nr = h.null_rule or "not applicable"
    rows.append([text("H1 null rule"), text("upper limit < 15 points"), text(""), text(""), text(""), text("confirmatory"),
                 text(nr.replace("_", " "))])
    lo90, hi90, verdict = h.tost
    rows.append([text("TOST"), text("90% interval within ±15 points"), text(""), text(""), ci(lo90, hi90),
                 text("exploratory"), text(tost_decision(str(verdict)))])
    return write_table(Path(out_dir) / "table_hypotheses",
                       ["Hypothesis", "Contrast", "Cells: k/n (P1)", "Diff. (pp)", "95% CI (pp)", "Status", "Decision"],
                       rows, align="lllrrll")


# ---- P2 / P2' --------------------------------------------------------------------------------------------

def table_p2(p2: Sequence[EpisodeRate], p2prime: Sequence[EpisodeRate], diffs: Sequence[Diff], book: Any,
             out_dir: Path) -> TableFiles:
    """P2 and P2' per cell (Wilson), a bracketed P2' shown as its bounds, then the Newcombe differences."""
    rows: list[Sequence[Cell] | None] = []
    pm = {r.cell: r for r in p2prime}
    for r in sorted(p2, key=lambda x: cell_key(x.cell)):
        rows.append([text(r.cell), text("P2"), kn(r.k, r.n), pct(r.p), ci(r.lo, r.hi)])
        q = pm.get(r.cell)
        if q is not None:
            if q.p is None and q.bound_lo is not None and q.bound_hi is not None:
                rows.append([text(r.cell), text("P2′ (bracketed)"), text(""), text("--"), ci(q.bound_lo, q.bound_hi)])
            else:
                rows.append([text(r.cell), text("P2′"), kn(q.k, q.n), pct(q.p), ci(q.lo, q.hi)])
    if diffs:
        rows.append(None)
        for d in diffs:
            rows.append([text(d.name), text(d.outcome), kn(d.k1, d.n1) if d.k1 is not None else text(""),
                         pp(d.estimate), ci(d.lo, d.hi)])
    return write_table(Path(out_dir) / "table_p2", ["Cell / contrast", "Outcome", "k/n", "% / diff. (pp)", "95% CI"],
                       rows, align="llrrr")


# ---- counts ----------------------------------------------------------------------------------------------

def table_counts(counts: Sequence[CountRow], book: Any, out_dir: Path) -> TableFiles:
    """Refusals, exclusions, reruns, timeouts and clips: one row per (model, count name), one column per cell."""
    cells = sorted({c.cell for c in counts}, key=cell_key)
    keys = sorted({(c.model, c.name) for c in counts})
    val = {(c.model, c.name, c.cell): c.value for c in counts}
    rows = [[text(m), text(n)] + [integer(val.get((m, n, c))) if (m, n, c) in val else text("0") for c in cells]
            for m, n in keys]
    return write_table(Path(out_dir) / "table_counts", ["Model", "Count"] + list(cells), rows,
                       align="ll" + "r" * len(cells))


# ---- secondary (Appendix D) ------------------------------------------------------------------------------

def table_secondary(sec: SecondaryResults, book: Any, out_dir: Path) -> list[TableFiles]:
    """Appendix D tables: cell rates, mean rates, D-010 board substitution, contrasts and differences."""
    d = Path(out_dir)
    out: list[TableFiles] = []
    rows = [[text(r.cell), text(r.outcome), kn(r.k, r.n), integer(r.n_episodes), pct(r.p), ci(r.lo, r.hi),
             text(r.method), pct(r.coverage, 0)] for r in sorted(sec.rates, key=lambda x: (x.outcome, cell_key(x.cell)))]
    out.append(write_table(d / "table_secondary_rates", ["Cell", "Outcome", "k/n", "Episodes", "%", "95% CI", "Method",
                                                          "Coverage (%)"], rows, align="llrrrrlr"))
    rows = [[text(r.cell), text(r.outcome), num(r.mean), ci(r.lo, r.hi, 2, 1.0), integer(r.n_episodes), text(r.method)]
            for r in sorted(sec.mean_rates, key=lambda x: (x.outcome, cell_key(x.cell)))]
    out.append(write_table(d / "table_secondary_means", ["Cell", "Outcome", "Mean per episode", "95% CI", "Episodes",
                                                          "Method"], rows, align="llrrrl"))
    rows = [[text(r.cell), text(r.source), integer(r.n_witnesses), integer(r.n_noteless), integer(r.n_noteless_known),
             integer(r.k_qualifying), pct(r.p), ci(r.lo, r.hi), text("conditional" if r.conditional else "overall"),
             pct(r.coverage, 0)] for r in sorted(sec.board_substitution, key=lambda x: (x.source, cell_key(x.cell)))]
    out.append(write_table(d / "table_secondary_board_substitution",
                           ["Cell", "Labels", "Witnesses", "No note", "No note, known", "Qualifying", "%", "95% CI",
                            "Rate", "Coverage (%)"], rows, align="llrrrrrrlr"))
    contrasts = [c for c in (sec.delta1_no_test_aware, sec.h2_p2) if c is not None]
    rows = [[text(c.name), pp(c.estimate) if c.status != "unavailable" else text("n/a"),
             ci(c.lo, c.hi) if c.status != "unavailable" else text("n/a"), text(c.status), text(decision_text(c))]
            for c in contrasts]
    out.append(write_table(d / "table_secondary_contrasts", ["Contrast", "Diff. (pp)", "95% CI (pp)", "Status",
                                                              "Decision"], rows, align="lrrll"))
    rows = [[text(x.name), text(x.outcome), pp(x.estimate), ci(x.lo, x.hi), text(x.method), text(x.note)]
            for x in sec.diffs]
    out.append(write_table(d / "table_secondary_diffs", ["Contrast", "Outcome", "Diff. (pp)", "95% CI (pp)", "Method",
                                                          "Note"], rows, align="llrrll"))
    return out


# ---- judge validation (Appendix G) -----------------------------------------------------------------------

def table_judge(validation: Mapping[str, Any] | None, agreement: Mapping[str, Any], book: Any, out_dir: Path
                ) -> TableFiles:
    """Appendix G. `validation`: {prompt_version, n_pass, n_items, passed, c_bar_usd?} (the validations row) or None.
    `agreement`: annotate.agreement.agreement_summary(). Model-model rows are labelled as such; the blind block reads
    "not run" or "declined" when there is no blind human check."""
    rows: list[Sequence[Cell] | None] = []
    if validation:
        rows.append([text("Held-out validation"), text(f"prompt {validation.get('prompt_version', '?')}"),
                     kn(validation.get("n_pass"), validation.get("n_items")),
                     text("passed" if validation.get("passed") else "FAILED"), text(""), text("")])
        rows.append(None)

    def kappas(section: str, block: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> None:
        for typ in sorted(block):
            for label, r in block[typ].items():
                rows.append([text(section), text(f"{typ}: {label}"), integer(r["n"]), pct(r["raw_agreement"]),
                             num(r["kappa"]), ci(r.get("kappa_lo"), r.get("kappa_hi"), 2, 1.0)])

    kappas("model–model (all pairs)", agreement.get("model_model", {}))
    rows.append(None)
    kappas("model–model (H-5)", agreement.get("model_model_h5", {}))
    rows.append(None)
    corr = agreement.get("correction", {})
    prec = agreement.get("precision_escalation_h3", {})
    rows.append([text("AI-audited, author-approved review"), text("share of queued items reviewed"), integer(corr.get("reviewed")),
                 pct(corr.get("share_reviewed")), text(""), text("")])
    rows.append([text("AI-audited, author-approved review"), text("correction rate (correct or use-judge)"), integer(corr.get("reviewed")),
                 pct(corr.get("rate")), text(""), text("")])
    rows.append([text("AI-audited, author-approved review"), text("precision of judge escalation positives"),
                 integer(prec.get("n_judge_positive_reviewed")), pct(prec.get("precision")), text(""), text("")])
    rows.append(None)
    blind = agreement.get("blind", {"status": "not run", "by_type": {}})
    if blind.get("status") == "run":
        kappas("blind human check", blind["by_type"])
    else:
        rows.append([text("blind human check"), text(blind.get("status", "not run")), text(""), text(""), text(""),
                     text("")])
    return write_table(Path(out_dir) / "table_judge", ["Section", "Item", "n", "Agreement (%)", "Kappa", "95% CI"], rows,
                       align="llrrrr")


def table_confusion(agreement: Mapping[str, Any], book: Any, out_dir: Path) -> TableFiles:
    """Appendix G confusion tables, long format: one row per (item type, label, judge value, Claude value)."""
    rows: list[Sequence[Cell] | None] = []
    for typ in sorted(agreement.get("model_model", {})):
        for label, r in agreement["model_model"][typ].items():
            cats = r["categories"]
            for i, a in enumerate(cats):
                for j, b in enumerate(cats):
                    if r["confusion"] and r["confusion"][i][j]:
                        rows.append([text(typ), text(label), text(a), text(b), integer(r["confusion"][i][j])])
    return write_table(Path(out_dir) / "table_confusion", ["Item type", "Label", "Judge", "Claude", "Count"], rows,
                       align="llllr")


# ---- gate log, cost --------------------------------------------------------------------------------------

def _flat(v: Any) -> str:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return v
    if isinstance(v, Mapping):
        return "; ".join(f"{k}={v[k]}" for k in v)
    return "" if v is None else str(v)


def table_gate_log(rows_in: Sequence[Mapping[str, Any]], book: Any, out_dir: Path) -> TableFiles:
    """Appendix C: gate_log rows (gate, phase, level, decision, numbers_json), every row marked as a pilot."""
    rows = [[text(r.get("gate")), text(r.get("phase")), text(r.get("level")), text(r.get("decision")),
             text(_flat(r.get("numbers_json"))), text("pilot")] for r in rows_in]
    return write_table(Path(out_dir) / "table_gate_log", ["Gate", "Phase", "Level", "Decision", "Numbers", "Data"], rows,
                       align="llllll")


def table_cost(cost: Mapping[str, Any], book: Any, out_dir: Path) -> TableFiles:
    """Cost by phase and model (`cost['phase_model']`: [{phase, model, n_calls, usd}]) and the judge by tier
    (`cost['judge_tier']`: [{tier, n_calls, usd, n_estimated}]), with a total line."""
    rows: list[Sequence[Cell] | None] = []
    tot = 0.0
    for r in cost.get("phase_model", []):
        rows.append([text("experiment"), text(f"{r['phase']} / {r['model']}"), integer(r.get("n_calls")), usd(r["usd"]),
                     text("")])
        tot += float(r["usd"])
    rows.append(None)
    for r in cost.get("judge_tier", []):
        rows.append([text("judge"), text(f"tier {r['tier']}"), integer(r.get("n_calls")), usd(r["usd"]),
                     integer(r.get("n_estimated"))])
        tot += float(r["usd"])
    rows.append(None)
    rows.append([text("total"), text(""), text(""), usd(tot), text("")])
    return write_table(Path(out_dir) / "table_cost", ["Source", "Phase / model or tier", "Calls", "Cost", "Estimated"], rows,
                       align="llrrr")
