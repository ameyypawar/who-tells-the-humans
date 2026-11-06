"""Sentence templates of research/42 3.9, chosen by rule; numbers come from the NumberBook (plan 47 2.16).

The 95 and the 15 are protocol constants and are registered in the book (ProtocolLevel, ProtocolMargin) like any
other number. Every sentence is LaTeX; item text never enters a sentence.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wtth.analysis.numbers import MINUS, NumberBook, fmt_value, is_count_mean_contrast, latex_escape, token
from wtth.analysis.results import Contrast, HypothesisResults, SonnetResults

PCT = r"\%"


def _ensure_constants(book: NumberBook) -> None:
    if "ProtocolLevel" not in book:
        book.add("ProtocolLevel", 95, "plain", source="PREREG 16: 95 percent intervals")
    if "ProtocolMargin" not in book:
        book.add("ProtocolMargin", 15, "plain", source="PREREG 11: null margin in percentage points")


def _p1(book: NumberBook, M: str, cell: str) -> tuple[str, str, str]:
    from wtth.analysis.numbers import cell_token

    pre = f"{M}POne{cell_token(cell)}"
    return book.fmt(pre + "Pct"), book.fmt(pre + "K"), book.fmt(pre + "N")


def _ci(c: Contrast) -> tuple[str, str, str]:
    return fmt_value(c.estimate, "pp"), fmt_value(c.lo, "pp"), fmt_value(c.hi, "pp")


def _neg(c: Contrast) -> str:
    return fmt_value(-c.estimate, "pp")


def sentences(h: HypothesisResults, book: NumberBook, *, model_label: str) -> Mapping[str, str]:
    """Keys HOne, HTwo, HThree (and PositiveControl). Rule-based template choice, no free text."""
    _ensure_constants(book)
    M = token(model_label.split()[0])
    L = latex_escape(model_label)
    lvl, mrg = book.fmt("ProtocolLevel"), book.fmt("ProtocolMargin")
    out: dict[str, str] = {}

    # ---- H1
    c = h.h1
    est, lo, hi = _ci(c)
    if c.status == "unavailable":
        out["HOne"] = "H1 could not be evaluated: a cell it needs has no episodes."
    elif c.rejected and c.direction == "predicted":
        a, ka, na = _p1(book, M, "NONE")
        b, kb, nb = _p1(book, M, "CLAIM")
        out["HOne"] = (f"Among {L} witnesses, a peer's false claim to have told ops lowered the share who notified ops "
                       f"about the split from {a}{PCT} ({ka}/{na}) to {b}{PCT} ({kb}/{nb}), a stand-down of {est} points "
                       f"({lvl}{PCT} cluster-bootstrap CI {lo} to {hi}; H1 rejected, first test of the pre-registered "
                       f"sequence).")
    elif c.rejected:
        out["HOne"] = (f"Contrary to the prediction, the claim raised reporting by {_neg(c)} points ({lvl}{PCT} CI "
                       f"{lo} to {hi}); H1 is rejected in the opposite direction.")
    else:
        pc = h.positive_control
        gap = pc.get("g2_gap_points")
        gtxt = "" if gap is None else f" The positive control (pilot G2) showed a {fmt_value(gap, 'pts')}-point drop."
        if h.null_rule == "no_meaningful_stand_down":
            out["HOne"] = (f"We find no meaningful stand-down: {est} points ({lvl}{PCT} CI {lo} to {hi}); the upper bound "
                           f"is below the pre-registered {mrg}-point margin.{gtxt}")
        else:
            out["HOne"] = (f"The data show no stand-down but cannot rule out one of up to {hi} points ({est}; {lvl}{PCT} "
                           f"CI {lo} to {hi}); by the pre-registered rule this is inconclusive.{gtxt}")

    # ---- H2
    c = h.h2
    est, lo, hi = _ci(c)
    r1 = fmt_value(c.components.get("dR1"), "pp")
    r0 = fmt_value(c.components.get("dR0"), "pp")
    if c.status == "unavailable":
        out["HTwo"] = "H2 could not be evaluated: a cell it needs has no episodes."
    elif c.status == "descriptive":
        out["HTwo"] = (f"Because H1 was not rejected, H2 and H3 are descriptive: the difference in stand-downs was {est} "
                       f"points ({lvl}{PCT} CI {lo} to {hi}), reported without a test.")
    elif c.rejected and c.direction == "predicted":
        out["HTwo"] = (f"With a channel-written log, the same claim's stand-down was {r1} points, against {r0} without "
                       f"one (difference {est}; {lvl}{PCT} CI {lo} to {hi}; H2 rejected, second test).")
    elif c.rejected:
        out["HTwo"] = (f"Contrary to the prediction, the difference in stand-downs was {est} points ({lvl}{PCT} CI {lo} "
                       f"to {hi}); H2 is rejected in the opposite direction.")
    else:
        out["HTwo"] = (f"With a channel-written log, the stand-down was {r1} points, against {r0} without one "
                       f"(difference {est}; {lvl}{PCT} CI {lo} to {hi}); H2 was not rejected, which does not show that "
                       f"receipts have no effect.")

    # ---- H3
    c = h.h3
    est, lo, hi = _ci(c)
    if c.status == "unavailable":
        out["HThree"] = "H3 could not be evaluated: a cell it needs has no episodes."
    elif c.status == "descriptive":
        why = "H1" if h.stopped_at == "H1" else "H2"
        out["HThree"] = (f"Because {why} was not rejected, H3 is descriptive: {est} points ({lvl}{PCT} CI {lo} to {hi}), "
                         f"reported without a test.")
    else:
        a, ka, na = _p1(book, M, "NO-RECEIPT")
        b, kb, nb = _p1(book, M, "FORGED")
        if c.rejected and c.direction == "predicted":
            out["HThree"] = (f"A receipt-style line typed by the claimant into its own post lowered reporting relative to "
                             f"the same claim with an empty log: {a}{PCT} vs {b}{PCT} ({est} points; {lvl}{PCT} CI {lo} "
                             f"to {hi}; H3 rejected, third test).")
        elif c.rejected:
            out["HThree"] = (f"Contrary to the prediction, the forged line raised reporting: {a}{PCT} vs {b}{PCT} ({est} "
                             f"points; {lvl}{PCT} CI {lo} to {hi}); H3 is rejected in the opposite direction.")
        else:
            out["HThree"] = (f"A receipt-style line typed by the claimant: {a}{PCT} vs {b}{PCT} ({est} points; {lvl}{PCT} "
                             f"CI {lo} to {hi}); H3 was not rejected.")
    pc = h.positive_control
    if pc.get("core_diff") is not None:
        out["PositiveControl"] = (
            f"In the core, the share of episodes with any witness note was {fmt_value(pc['core_diff'], 'pp')} points lower "
            f"with C's real note than without ({lvl}{PCT} Newcombe CI {fmt_value(pc['core_lo'], 'pp')} to "
            f"{fmt_value(pc['core_hi'], 'pp')}).")
    return out


def sonnet_sentence(s: SonnetResults, book: NumberBook, *, model_label: str, episodes_per_cell: int) -> str:
    """Exploratory Sonnet template: P1 per cell with Wilson intervals, no confirmatory test."""
    _ensure_constants(book)
    M = token(model_label.split()[0])
    lvl = book.fmt("ProtocolLevel")
    parts = []
    from wtth.analysis.numbers import cell_token

    for r in s.p1:
        pre = f"{M}POne{cell_token(r.cell)}"
        parts.append(f"{latex_escape(r.cell)} {book.fmt(pre + 'Pct')}{PCT} ({book.fmt(pre + 'K')}/{book.fmt(pre + 'N')}; "
                     f"{lvl}{PCT} Wilson CI {book.fmt(pre + 'CI')})")
    n = fmt_value(episodes_per_cell, "count")
    ceil = " P1 in NONE is at or above the ceiling margin." if s.ceiling else ""
    return (f"In an exploratory {latex_escape(model_label)} subset ({n} episodes per cell), " + "; ".join(parts) +
            f"; no confirmatory test.{ceil}")


def descriptive_sentence(c: Contrast, book: NumberBook, *, label: str) -> str:
    _ensure_constants(book)
    if is_count_mean_contrast(c.name):
        est, lo, hi = (fmt_value(x, "mean") for x in (c.estimate, c.lo, c.hi))
        return (f"{latex_escape(label)}: {est} notes per shift ({book.fmt('ProtocolLevel')}{PCT} CI {lo} to {hi}), "
                f"reported without a test.")
    est, lo, hi = _ci(c)
    return f"{latex_escape(label)}: {est} points ({book.fmt('ProtocolLevel')}{PCT} CI {lo} to {hi}), reported without a test."


# ---- post hoc (D-021) --------------------------------------------------------------------------------------------

def _p_clause(book: NumberBook, key: str) -> str:
    d = book.fmt(key)
    return ("p " + d) if d.startswith(r"\ensuremath{<}") else ("p = " + d)


def robustness_sentence(book: NumberBook, M: str = "Haiku") -> str | None:
    """Post-hoc robustness of H1-H3 (D-021 item 1), from the registered PostHoc keys only."""
    need = [M + "PostHocHOneFisherP", M + "PostHocHThreeFisherP", M + "PostHocHOneNewcombeCI",
            M + "PostHocHThreeNewcombeCI", M + "PostHocHTwoMoverCI"]
    if any(k not in book for k in need):
        return None
    s = (f"Post hoc (D-021), at witness level and ignoring clustering, Fisher's exact test gives "
         f"{_p_clause(book, M + 'PostHocHOneFisherP')} for H1 and {_p_clause(book, M + 'PostHocHThreeFisherP')} for "
         f"H3, with Newcombe intervals of {book.fmt(M + 'PostHocHOneNewcombeCI')} and "
         f"{book.fmt(M + 'PostHocHThreeNewcombeCI')} points; the MOVER interval of the H2 difference-in-differences "
         f"is {book.fmt(M + 'PostHocHTwoMoverCI')} points.")
    pre = M + "PostHocBoundaryHOneClaim"
    if pre + "PZero" in book:
        s += (f" CLAIM's {book.fmt(pre + 'NonReporters')} non-reporters sit in {book.fmt(pre + 'ShiftsM')} of its "
              f"{book.fmt(pre + 'ShiftsN')} shifts, so an episode-cluster resample draws none of them with probability "
              f"{book.fmt(pre + 'PZero')}{PCT} ({book.fmt(pre + 'PZeroBoot')}{PCT} of the "
              f"{book.fmt(pre + 'PZeroBootB')} H1 replicates).")
    return s


def timing_sentence(book: NumberBook, M: str = "Haiku") -> str | None:
    """Post-hoc timing (D-021 item 2): held-off witnesses in CLAIM and NO-RECEIPT, the log at their next turn."""
    c, r = M + "TimingClaim", M + "TimingNoReceipt"
    if any(k not in book for k in (c + "HeldOffK", r + "HeldOffK", r + "HeldOffPeerNeverK")):
        return None
    base = []
    for cell, tok in (("NONE", "None"), ("NONE-R", "NoneR")):
        k = M + "Timing" + tok + "HeldOffK"
        if k in book:
            base.append((cell, book.value(k), book.fmt(k)))
    base_txt = ""
    if base and all(v == 0 for _, v, _ in base):
        base_txt = " (none in " + " and ".join(latex_escape(b[0]) for b in base) + ")"
    elif base:
        base_txt = " (" + "; ".join(f"{latex_escape(b[0])} {b[2]}" for b in base) + ")"
    s = (f"Post hoc (D-021): by the end of round 2, {book.fmt(c + 'HeldOffK')} of {book.fmt(c + 'N')} CLAIM witnesses "
         f"and {book.fmt(r + 'HeldOffK')} of {book.fmt(r + 'N')} NO-RECEIPT witnesses had sent no violation note"
         f"{base_txt}. Of these, {book.fmt(c + 'HeldOffReportedK')} and {book.fmt(r + 'HeldOffReportedK')} reported "
         f"later and {book.fmt(c + 'NeverK')} and {book.fmt(r + 'NeverK')} never did; "
         f"{book.fmt(r + 'HeldOffPeerNeverK')} of the {book.fmt(r + 'NeverK')} NO-RECEIPT never-reporters had a peer's "
         f"line in the log at their next turn.")
    sh = M + "TimingShiftReceipt"
    if sh + "LateAfterReceiptK" in book:
        s += (f" In {book.fmt(sh + 'LateAfterReceiptK')} of {book.fmt(sh + 'N')} RECEIPT shifts the first witness note "
              f"came in round 3 or later, after C's real log line had been in view since round 2.")
    return s
