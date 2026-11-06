"""Figures (plan 47 section 2.17): Fig. 1a, Fig. 1b, P1 by cell, Kaplan-Meier curves, the alert-load frontier.

Fig. 1 was redesigned after the strict analysis (D-021, review S2): Fig. 1a is P1 in every cell with P2 as k/n shifts,
Fig. 1b the timing of first reports. The frontier that was Fig. 1b is now fig_frontier (not part of Fig. 1).

Each function writes <out_dir>/<name>.{pdf,png,alt.txt,csv} through style.save and returns FigureFiles. Every series
differs by colour AND marker or line style; colours are the Okabe-Ito palette without yellow. The alt text names every
plotted value.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from wtth.analysis.results import CellRate, EpisodeRate, FrontierPoint, HypothesisResults, KMCurve
from wtth.analysis.style import (CAP_PT, CELL_STYLE, DOUBLE_COLUMN_IN, LINE_PT, P2_Y_LABEL, PALETTE, SINGLE_COLUMN_IN,
                                 FigureFiles, new_figure, save)
from wtth.analysis.tables import _dec

R0_CELLS = ("NONE", "CONCERN", "CLAIM", "POLICY", "B-NONE")
R1_CELLS = ("NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS")
GROUPS = (("H1", ("NONE", "CLAIM")), ("H2", ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT")),
          ("H3", ("NO-RECEIPT", "FORGED")))


MINUS_SIGN = "\u2212"


def _minus(s: str) -> str:
    """A formatted number with its leading hyphen-minus replaced by the minus sign U+2212."""
    return MINUS_SIGN + s[1:] if s.startswith("-") else s


def _p(x: float | None) -> str:
    return "n/a" if x is None else _dec(x * 100.0, 1) + "%"


def _ci(lo: float | None, hi: float | None) -> str:
    return "n/a" if lo is None or hi is None else f"{_dec(lo * 100.0, 1)} to {_dec(hi * 100.0, 1)}"


def _err(ax: Any, x: float, y: float | None, lo: float | None, hi: float | None, **kw: Any) -> None:
    """Point with an asymmetric vertical interval and 2.5 pt caps."""
    if y is None:
        return
    yerr = None if lo is None or hi is None else [[max(y - lo, 0.0)], [max(hi - y, 0.0)]]
    ax.errorbar([x], [y], yerr=yerr, linestyle="none", capsize=CAP_PT, elinewidth=LINE_PT, capthick=LINE_PT, **kw)


# ---- Fig. 1a: P1 in every cell, P2 as k/n shifts (D-021, review S2; research/68 A6) ------------------------------
#
# research/68 A6 (reviews research/64 FLAG 9, 28 and research/66): every row code carries a short gloss; the rows where
# C really told ops (RECEIPT, POS) are shaded, with the legend "C really told ops: standing down is correct"; the
# 'ops never told' column reads an em dash where it does not apply; the panel is labelled (a); the axis says that
# higher means ops was told; the intervals are whatever the rates carry (shift-level cluster bootstrap in the paper,
# named in the header); follow-up rows (a later phase) are drawn hollow with dashed intervals under a heavier rule.

FIG1_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("no log (R0)", ("NONE", "CONCERN", "CLAIM", "POLICY")),
    ("log (R1)", ("NONE-R", "NO-RECEIPT", "RECEIPT", "FORGED")),
    ("controls", ("NEG", "POS", "B-NONE")))
GROUP_DISPLAY = {"no log (R0)": "no log (R0)", "log (R1)": "log (R1)", "controls": "controls"}   # drawn rotated
FOLLOWUP_GROUP = "follow-up (exploratory, D-022)"         # the CSV / alt-text name
FOLLOWUP_DISPLAY = "follow-up"
C_TOLD_CELLS = ("RECEIPT", "POS")          # C really reported, so P2 is not defined there (PREREG 10)
NO_VIOLATION_CELLS = ("B-NONE",)
HAIKU_COLOUR, SONNET_COLOUR = PALETTE["blue"], PALETTE["orange"]     # validated: CVD dE 29 (protan)
SHADE_COLOUR, SHADE_ALPHA = PALETTE["green"], 0.13
C_TOLD_LEGEND = "C really told ops: standing down is correct"
NOT_APPLICABLE = "—"                  # the 'ops never told' cell where P2 is not defined
C_TOLD_TEXT, NO_VIOLATION_TEXT = "not applicable: C really told ops", "not applicable: no violation"
FIG1A_X_LABEL = "P1: witnesses who told ops about the split (%) — higher means ops was told"
GLOSS: Mapping[str, str] = {
    "NONE": "no claim, no log", "CONCERN": "concern post, no claim", "CLAIM": "bare claim, no log",
    "POLICY": "claim + one-line rule", "NONE-R": "no claim, log shown", "NO-RECEIPT": "claim, empty log",
    "RECEIPT": "true claim + real receipt", "FORGED": "claim + typed log-style line",
    "NEG": "duplicate cost, no claim", "POS": "real receipt + duplicate cost", "B-NONE": "no violation"}
FOLLOWUP_GLOSS: Mapping[tuple[str, str], str] = {
    ("X-FORGED-POLICY", "haiku"): "+ one-line rule", ("X-FORGED-PROV", "haiku"): "+ provenance note",
    ("RECEIPT", "sonnet"): "real receipt"}
INTERVAL_TEXT = {"cluster_bootstrap": "lines: 95% shift-level cluster-bootstrap intervals",
                 "wilson": "lines: 95% Wilson intervals, witness level"}
INTERVAL_ALT = {"cluster_bootstrap": "shift-level cluster-bootstrap 95% intervals (shifts resampled within each cell)",
                "wilson": "Wilson 95% intervals at witness level (they ignore clustering within shifts)"}
FIG1A_WIDTH_IN, FIG1B_WIDTH_IN = 5.85, 3.05      # the printed widths (sections/01-introduction.tex)
FIG_PT = 7.5
CODE_PT, GLOSS_PT, GROUP_PT, NOTE_PT = 7.0, 6.5, 6.5, 6.5
PANEL_PT = 8.0


@dataclass(frozen=True, slots=True)
class FigRow:
    """An extra row of Fig. 1a (the D-022 follow-up group): P1 with its interval (method in the rate); P2 None where C
    reported."""
    label: str
    model: str              # 'haiku' | 'sonnet'
    p1: CellRate
    p2: EpisodeRate | None


def _kn(r: EpisodeRate | None, cell: str) -> str:
    """The 'ops never told' value for the CSV and alt text: k/n shifts, or why it does not apply."""
    if cell in C_TOLD_CELLS:
        return C_TOLD_TEXT
    if cell in NO_VIOLATION_CELLS:
        return NO_VIOLATION_TEXT
    return "" if r is None or r.k is None else f"{r.k}/{r.n}"


def _kn_shown(v: str) -> str:
    """The figure's text for a _kn value: the em dash wherever P2 does not apply."""
    return NOT_APPLICABLE if v.startswith("not applicable") else v


def _text_width_in(s: str, size: float) -> float:
    """Width in inches of the widest line of `s` in the figure font at `size` points (deterministic, no renderer)."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextToPath

    prop = FontProperties(family="DejaVu Sans", size=size)
    tp = TextToPath()
    return max((tp.get_text_width_height_descent(line, prop, ismath=False)[0] for line in s.split("\n") if line),
               default=0.0) / 72.0


def _hbar(ax: Any, y: float, r: CellRate, *, colour: str, marker: str, hollow: bool, dashed: bool = False) -> None:
    x = r.p * 100.0
    err = [[max(x - r.lo * 100.0, 0.0)], [max(r.hi * 100.0 - x, 0.0)]]
    _, caps, bars = ax.errorbar([x], [y], xerr=err, linestyle="none", color=colour, ecolor=colour, elinewidth=1.0,
                                capsize=1.6, capthick=1.0, marker=marker, markersize=4.2 if not hollow else 4.4,
                                markerfacecolor="#FFFFFF" if hollow else colour, markeredgecolor=colour,
                                markeredgewidth=1.2 if hollow else 0.6, zorder=3)
    if dashed:
        for b in bars:
            b.set_linestyle((0, (2.2, 1.4)))


def _gloss(cell: str, model: str, followup: bool) -> str:
    return FOLLOWUP_GLOSS.get((cell, model), GLOSS.get(cell, "")) if followup else GLOSS.get(cell, "")


def fig1a(haiku_p1: Sequence[CellRate], haiku_p2: Sequence[EpisodeRate], out_dir: Path, *, model_label: str,
          sonnet_p1: Sequence[CellRate] = (), sonnet_p2: Sequence[EpisodeRate] = (), sonnet_label: str = "Sonnet 5.5",
          followup: Sequence[FigRow] = ()) -> FigureFiles:
    """P1 (share of witnesses who sent ops a violation note) per cell with the 95% intervals the rates carry (the
    header names the method): Haiku filled circles, the exploratory Sonnet arm hollow diamonds where it ran. Rows are
    grouped no log (R0), log (R1), controls and, only when given, the D-022 follow-up (a later phase: hollow markers,
    dashed intervals, a heavier rule). Every row code carries a gloss; RECEIPT and POS, where C really told ops, are
    shaded. Text columns give P1 and 'ops never told' (P2) as k/n shifts, an em dash where P2 does not apply; a k
    above zero is set in bold."""
    from matplotlib.lines import Line2D
    from matplotlib.patches import Rectangle
    from matplotlib.transforms import blended_transform_factory

    h1 = {r.cell: r for r in haiku_p1}
    h2 = {r.cell: r for r in haiku_p2}
    s1 = {r.cell: r for r in sonnet_p1}
    s2 = {r.cell: r for r in sonnet_p2}
    groups: list[tuple[str, str, list[dict[str, Any]]]] = []
    for gname, cells in FIG1_GROUPS:
        g = [{"label": c, "cell": c, "gloss": GLOSS.get(c, ""), "haiku": h1.get(c), "sonnet": s1.get(c),
              "p2h": _kn(h2.get(c), c), "p2s": _kn(s2.get(c), c) if c in s1 else "", "followup": False,
              "c_told": c in C_TOLD_CELLS} for c in cells if c in h1 or c in s1]
        if g:
            groups.append((gname, GROUP_DISPLAY.get(gname, gname), g))
    if followup:
        g = []
        for fr in followup:
            hs = fr.model == "haiku"
            p2 = C_TOLD_TEXT if fr.p2 is None else f"{fr.p2.k}/{fr.p2.n}"
            g.append({"label": fr.label, "cell": fr.p1.cell, "gloss": _gloss(fr.p1.cell, fr.model, True),
                      "haiku": fr.p1 if hs else None, "sonnet": None if hs else fr.p1, "p2h": p2 if hs else "",
                      "p2s": "" if hs else p2, "followup": True, "c_told": fr.p2 is None})
        groups.append((FOLLOWUP_GROUP, FOLLOWUP_DISPLAY, g))
    rows_all = [r for _, _, g in groups for r in g]
    n_rows = len(rows_all)
    # the follow-up group adds three rows and the header a third line: tighter rows, gaps and header lines keep
    # Fig. 1a at the height it had before research/68 (the main text has no line to spare)
    gap = 0.55 if not followup else 0.40
    pitch_in = 0.148 if not followup else 0.126
    top_in, bottom_in = 0.42, 0.36
    height = top_in + bottom_in + pitch_in * (n_rows + gap * (len(groups) - 1) + 0.4)
    width = FIG1A_WIDTH_IN
    has_s = bool(s1) or any(fr.model == "sonnet" for fr in followup)
    # left block: group label (rotated, one line high) | code | gloss (a gloss starts in its own column, or after a
    # code too long for it)
    x_group = 0.04
    group_w = 1.25 * GROUP_PT / 72.0
    x_code = x_group + group_w + 0.06
    core_w = max((_text_width_in(r["label"], CODE_PT) for r in rows_all if not r["followup"]), default=0.0)
    x_gloss_col = x_code + core_w + 0.07
    for r in rows_all:
        r["x_gloss"] = max(x_gloss_col, x_code + _text_width_in(r["label"], CODE_PT) + 0.06)
    label_end = max(r["x_gloss"] + _text_width_in(r["gloss"], GLOSS_PT) for r in rows_all)
    left_in = label_end + 0.10
    cols_in = {"p1h": 0.36, "p1s": 0.76, "p2h": 1.22, "p2s": 1.62} if has_s else {"p1h": 0.40, "p2h": 1.40}
    right_in = max(cols_in.values()) + 0.06
    fig, ax = new_figure(width, height)
    fig.subplots_adjust(left=left_in / width, right=1.0 - right_in / width, top=1.0 - top_in / height,
                        bottom=bottom_in / height)
    figx = blended_transform_factory(fig.transFigure, ax.transData)
    y = 0.0
    seps: list[tuple[float, bool]] = []
    gpos: list[tuple[str, float]] = []
    for gi, (gname, disp, g) in enumerate(groups):
        if gi:
            seps.append((y - 0.5 - gap / 2.0, gname == FOLLOWUP_GROUP))
        first = y
        for r in g:
            fu = r["followup"]
            dodge = 0.17 if (r["haiku"] is not None and r["sonnet"] is not None) else 0.0
            if r["haiku"] is not None:
                _hbar(ax, y - dodge, r["haiku"], colour=HAIKU_COLOUR, marker="o", hollow=fu, dashed=fu)
            if r["sonnet"] is not None:
                _hbar(ax, y + dodge, r["sonnet"], colour=SONNET_COLOUR, marker="D", hollow=True, dashed=fu)
            if r["c_told"]:
                ax.add_patch(Rectangle((0.006, y - 0.5), 0.990, 1.0, transform=figx, facecolor=SHADE_COLOUR,
                                       alpha=SHADE_ALPHA, edgecolor="none", linewidth=0, zorder=0, clip_on=False))
            ax.text(x_code / width, y, r["label"], transform=figx, ha="left", va="center", fontsize=CODE_PT)
            if r["gloss"]:
                ax.text(r["x_gloss"] / width, y, r["gloss"], transform=figx, ha="left", va="center",
                        fontsize=GLOSS_PT, color=PALETTE["black"], alpha=0.78)
            r["y"] = y
            y += 1.0
        gpos.append((disp, (first + y - 1.0) / 2.0))
        y += gap
    y_end = y - gap
    ax.set_yticks([r["y"] for r in rows_all])
    ax.set_yticklabels([])
    ax.tick_params(axis="y", length=0)
    ax.set_ylim(y_end - 0.45, -0.6)
    ax.set_xlim(-2.0, 102.0)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.tick_params(axis="x", labelsize=FIG_PT)
    ax.set_xlabel(FIG1A_X_LABEL, fontsize=FIG_PT - 0.5)
    ax.grid(False)
    for xv in (0, 25, 50, 75, 100):
        ax.axvline(xv, color=PALETTE["black"], alpha=0.10, linewidth=0.6, zorder=0)
    ax.spines["left"].set_visible(False)
    for ys, heavy in seps:
        ax.add_line(Line2D([0.006, 0.996], [ys, ys], transform=figx, color=PALETTE["black"],
                           alpha=0.55 if heavy else 0.25, linewidth=0.9 if heavy else 0.6, clip_on=False))
    for disp, yc in gpos:
        ax.text((x_group + group_w / 2.0) / width, yc, disp, transform=figx, rotation=90, ha="center", va="center",
                fontsize=GROUP_PT)
    # text columns: right edges in inches from the axes' right edge
    ax_w = (1.0 - right_in / width - left_in / width) * width
    colx = {k: 1.0 + v / ax_w for k, v in cols_in.items()}
    xax = blended_transform_factory(ax.transAxes, ax.transData)
    for r in rows_all:
        for key, rate in (("p1h", r["haiku"]), ("p1s", r["sonnet"])):
            if rate is not None and key in colx:
                ax.text(colx[key], r["y"], _dec(rate.p * 100.0, 1), transform=xax, ha="right", va="center",
                        fontsize=FIG_PT, clip_on=False)
        for key in ("p2h", "p2s"):
            txt = _kn_shown(r[key])
            if txt and key in colx:
                bold = "/" in txt and not txt.startswith("0/")
                ax.text(colx[key], r["y"], txt, transform=xax, ha="right", va="center", fontsize=FIG_PT,
                        fontweight="bold" if bold else "normal", clip_on=False)
    # the header band, three lines above the plot (never over the data)
    ax_h = height - top_in - bottom_in
    hy = [1.0 + (top_in - 0.075 - 0.115 * i) / ax_h for i in range(3)]
    figa = blended_transform_factory(fig.transFigure, ax.transAxes)
    ax.text(x_group / width, hy[0], "(a)", transform=figa, ha="left", va="center", fontsize=PANEL_PT,
            fontweight="bold", clip_on=False)
    handles = [Line2D([], [], color=HAIKU_COLOUR, marker="o", linestyle="-", linewidth=1.0, markersize=4.2,
                      label=f"{model_label}")]
    if has_s:
        handles.append(Line2D([], [], color=SONNET_COLOUR, marker="D", linestyle="-", linewidth=1.0, markersize=4.4,
                              markerfacecolor="#FFFFFF", markeredgewidth=1.2, label=f"{sonnet_label} (exploratory)"))
    if followup:
        handles.append(Line2D([], [], color=PALETTE["black"], marker="o", linestyle=(0, (2.2, 1.4)), linewidth=1.0,
                              markersize=4.2, markerfacecolor="#FFFFFF", markeredgewidth=1.0,
                              label="follow-up: later phase, not concurrent"))
    x_leg = (x_group + _text_width_in("(a)", PANEL_PT) + 0.12 - left_in) / ax_w
    ax.legend(handles=handles, loc="center left", bbox_to_anchor=(x_leg, hy[0]), ncol=len(handles),
              fontsize=NOTE_PT, frameon=False, handlelength=1.6, borderaxespad=0.0, borderpad=0.0,
              columnspacing=1.0, handletextpad=0.4)
    if any(r["c_told"] for r in rows_all):
        sw_h = 0.085 / ax_h
        ax.add_patch(Rectangle((x_group / width, hy[1] - sw_h / 2.0), 0.20 / width, sw_h, transform=figa,
                               facecolor=SHADE_COLOUR, alpha=SHADE_ALPHA * 2.2, edgecolor="none", linewidth=0,
                               clip_on=False))
        ax.text((x_group + 0.26) / width, hy[1], C_TOLD_LEGEND, transform=figa, ha="left", va="center",
                fontsize=NOTE_PT, clip_on=False)
    methods = sorted({rate.method for r in rows_all for rate in (r["haiku"], r["sonnet"]) if rate is not None})
    note = "; ".join(INTERVAL_TEXT.get(m, f"lines: 95% {m} intervals") for m in methods)
    ax.text(x_group / width, hy[2], note, transform=figa, ha="left", va="center", fontsize=NOTE_PT, clip_on=False)
    p1_last = "p1s" if has_s else "p1h"
    p2_last = "p2s" if has_s else "p2h"
    ax.text((colx["p1h"] + colx[p1_last]) / 2.0 - 0.16 / ax_w, hy[1], "P1 (%)", transform=ax.transAxes,
            ha="center", va="center", fontsize=NOTE_PT, clip_on=False)
    ax.text(colx[p2_last], hy[1], "ops never told (shifts)", transform=ax.transAxes, ha="right", va="center",
            fontsize=NOTE_PT, clip_on=False)
    short_h = model_label.split()[0]
    short_s = sonnet_label.split()[0]
    for key, name in (("p1h", short_h), ("p1s", short_s), ("p2h", short_h), ("p2s", short_s)):
        if key in colx:
            ax.text(colx[key], hy[2], name, transform=ax.transAxes, ha="right", va="center", fontsize=NOTE_PT,
                    color=PALETTE["black"], clip_on=False)
    out_rows: list[dict[str, Any]] = []
    parts: list[str] = []
    for gname, _, g in groups:
        for r in g:
            for model, rate, p2 in (("haiku", r["haiku"], r["p2h"]), ("sonnet", r["sonnet"], r["p2s"])):
                if rate is None:
                    continue
                out_rows.append({"group": gname, "label": r["label"], "gloss": r["gloss"], "cell": r["cell"],
                                 "model": model, "k": rate.k, "n": rate.n, "p": rate.p, "lo": rate.lo, "hi": rate.hi,
                                 "method": rate.method, "ops_never_told": p2,
                                 "c_really_told_ops": "yes" if r["c_told"] else "no",
                                 "block": "follow-up" if r["followup"] else "core"})
                parts.append(f"{r['label']} ({r['gloss']}) {model}: P1 {_p(rate.p)} ({_ci(rate.lo, rate.hi)}), "
                             f"ops never told {p2 or 'not shown'}")
    how = "; ".join(INTERVAL_ALT.get(m, m) for m in methods)
    alt = (f"Figure 1a. P1, the share of witnesses who sent ops a note about the split, in every cell, with {how}; "
           f"higher means ops was told. {model_label} is drawn as filled circles"
           + (f", the exploratory {sonnet_label} arm as hollow diamonds where it ran" if has_s else "")
           + ". Each row code carries a short gloss. Rows are grouped: no log (R0), log (R1), controls"
           + (", follow-up (exploratory, D-022; a later phase, not concurrent, drawn hollow with dashed intervals)"
              if followup else "")
           + ". Shaded rows are those where C really told ops, so standing down there is correct. Text columns give P1 "
             "in percent and the number of shifts in which ops was never told (P2, k of n); an em dash marks the "
             f"cells where P2 does not apply (C really told ops, or no violation). Values: {'; '.join(parts)}.")
    return save(fig, Path(out_dir) / "fig1a", alt_text=alt, rows=out_rows)


# ---- Fig. 1b: when witnesses reported, and what the log showed (D-021 item 2) ----------------------------------------

TIMING_CELLS = ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT")
SEG_ROUND2, SEG_LATER, SEG_NEVER = PALETTE["sky"], PALETTE["green"], PALETTE["vermillion"]   # validated: CVD dE 11


def fig1b(t: Any, out_dir: Path, *, model_label: str, cells: Sequence[str] = TIMING_CELLS) -> FigureFiles:
    """Stacked bars per cell (posthoc.TimingResults): witnesses whose first violation note came by round 2 ('in round 2'
    when no plotted witness reported in round 1), who held off and reported later, and who never reported; the
    never-reported witnesses who had a peer's line in the #escalation-log at their next turn are hatched. Counts are
    printed in and under the bars; cells without a log and cells with one are grouped."""
    import matplotlib as mpl
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.transforms import blended_transform_factory

    rows = [t.cell(c) for c in cells if t.cell(c) is not None]
    early = "in round 2" if all(c.by_round.get(1, 0) == 0 for c in rows) else "by round 2"
    width = FIG1B_WIDTH_IN
    pitch_in = 0.35
    top_in, bottom_in = 0.57, 0.33         # 0.12 in above the legend hold the panel label (research/68 A6)
    height = top_in + bottom_in + pitch_in * (len(rows) + 0.25)
    with mpl.rc_context({"hatch.linewidth": 0.9, "hatch.color": "#FFFFFF"}):
        fig, ax = new_figure(width, height)
        left_in, right_in = 0.92, 0.14
        fig.subplots_adjust(left=left_in / width, right=1.0 - right_in / width, top=1.0 - top_in / height,
                            bottom=bottom_in / height)
        bar_h = 0.40
        out_rows: list[dict[str, Any]] = []
        parts: list[str] = []
        for i, c in enumerate(rows):
            n = c.n
            peer_never = c.held_peer_never if c.has_log else 0
            segs = [(c.by_round2, SEG_ROUND2, None), (c.held_later, SEG_LATER, None),
                    (c.never - peer_never, SEG_NEVER, None), (peer_never, SEG_NEVER, "////")]
            x0 = 0.0
            for k, col, hatch in segs:
                if not k:
                    continue
                wv = 100.0 * k / n
                ax.barh(i, wv, left=x0, height=bar_h, color=col, hatch=hatch, edgecolor="#FFFFFF", linewidth=0.8,
                        zorder=2)
                x0 += wv
            ax.text(1.5, i, f"{c.by_round2} of {n} {early}", ha="left", va="center", fontsize=FIG_PT - 0.5,
                    color=PALETTE["black"], zorder=3)
            if c.held_off:
                line = f"{c.held_off} held off: {c.held_later} reported later, {c.never} never"
                if c.has_log and c.never:
                    line += f";\n{peer_never} of the {c.never} had a peer's line in the log"
                ax.text(100.0, i + bar_h / 2.0 + 0.04, line, ha="right", va="top", fontsize=FIG_PT - 1.0,
                        color=PALETTE["black"], linespacing=1.05)
            out_rows.append({"cell": c.cell, "log": c.has_log, "n": n, "by_round2": c.by_round2, "held_off": c.held_off,
                             "held_off_reported_later": c.held_later, "never": c.never,
                             "never_peer_line_in_log": peer_never if c.has_log else ""})
            parts.append(f"{c.cell} ({'log' if c.has_log else 'no log'}): {c.by_round2} of {n} reported {early}, "
                         f"{c.held_later} held off and reported later, {c.never} never reported"
                         + (f", {peer_never} of whom had a peer's line in the log at their next turn" if c.has_log
                            else ""))
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels([c.cell for c in rows], fontsize=FIG_PT)
        ax.tick_params(axis="y", length=0, pad=3)
        ax.set_ylim(len(rows) - 0.05, -0.45)
        ax.set_xlim(0, 100)
        ax.set_xticks([0, 25, 50, 75, 100])
        ax.tick_params(axis="x", labelsize=FIG_PT - 0.5)
        ax.set_xlabel("witnesses (%)", fontsize=FIG_PT - 0.5, labelpad=1.5)
        ax.grid(False)
        ax.spines["left"].set_visible(False)
        figx = blended_transform_factory(fig.transFigure, ax.transData)
        logs = [c.has_log for c in rows]
        for flag, name in ((False, "no log (R0)"), (True, "log (R1)")):
            idx = [i for i, v in enumerate(logs) if v == flag]
            if idx:
                ax.text(0.03, (idx[0] + idx[-1]) / 2.0, name, transform=figx, rotation=90, ha="center", va="center",
                        fontsize=FIG_PT - 1.0)
        for i in range(1, len(rows)):
            if logs[i] != logs[i - 1]:
                ax.add_line(Line2D([0.012, 0.99], [i - 0.5 + 0.06] * 2, transform=figx, color=PALETTE["black"],
                                   alpha=0.25, linewidth=0.6, clip_on=False))
        handles = [Patch(facecolor=SEG_ROUND2, edgecolor="#FFFFFF", label=f"reported {early}"),
                   Patch(facecolor=SEG_LATER, edgecolor="#FFFFFF", label="held off, reported later"),
                   Patch(facecolor=SEG_NEVER, edgecolor="#FFFFFF", label="never reported"),
                   Patch(facecolor=SEG_NEVER, edgecolor="#FFFFFF", hatch="////",
                         label="never; a peer's line in the log")]
        ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(-(left_in - 0.05) / (width - left_in - right_in),
                                                                     1.03),
                  ncol=2, fontsize=FIG_PT - 1.0, frameon=False, handlelength=1.4, handleheight=0.9,
                  columnspacing=0.9, handletextpad=0.4, borderaxespad=0.0)
        ax.text(0.04 / width, 1.0 - 0.075 / height, "(b)", transform=fig.transFigure, ha="left", va="center",
                fontsize=PANEL_PT, fontweight="bold")
        alt = (f"Figure 1b, {model_label}. When witnesses first sent ops a note about the split, as a share of each "
               f"cell's witnesses: {early}, after holding off past round 2, or never. In the log cells the "
               f"never-reporters who had a peer's line in the #escalation-log at their next turn (round 3) are "
               f"hatched. {'; '.join(parts)}.")
        return save(fig, Path(out_dir) / "fig1b", alt_text=alt, rows=out_rows)


# ---- safety / alert-load frontier (Fig. 1b until D-021; not in Fig. 1 any more) ----------------------------

_FRONTIER_STYLE = {"CLAIM": ("blue", "o"), "POLICY": ("green", "^"), "RECEIPT": ("orange", "D"),
                   "RECEIPTS": ("orange", "D"), "RECEIPT ARM": ("orange", "D"), "RECEIPTS ARM": ("orange", "D"),
                   "FORGED": ("vermillion", "s")}
_FALLBACK = (("purple", "v"), ("sky", "P"), ("vermillion", "X"))


def _label_offsets(points: Sequence[FrontierPoint], y_span: float = 1.0) -> dict[int, tuple[float, float, str]]:
    """Label offsets (points) and alignment. A point alone gets (6, 6). Points that sit on top of each other (x within
    0.25 and y within 8% of the y-axis span `y_span` of one another, e.g. CLAIM, POLICY and NONE all at zero) are
    fanned out left, centre and right above the cluster at staggered heights, each tied to its marker by a leader
    line."""
    out: dict[int, tuple[float, float, str]] = {id(pt): (6.0, 6.0, "left") for pt in points}
    seen: set[int] = set()
    y_near = 0.08 * y_span
    for pt in points:
        if id(pt) in seen:
            continue
        near = [q for q in points if abs(q.x - pt.x) <= 0.25 and abs((q.y or 0.0) - (pt.y or 0.0)) <= y_near]
        if len(near) < 2:
            continue
        near.sort(key=lambda q: q.x)
        slots = [(-30.0, 34.0, "right"), (0.0, 62.0, "center"), (30.0, 34.0, "left"), (0.0, 90.0, "center")]
        for q, slot in zip(near, slots):
            out[id(q)] = slot
            seen.add(id(q))
    return out


FRONTIER_Y_TOP = 0.25      # review 2 N3: every P2 and its interval sit near 0, so a 0 to 1 axis hides them


def frontier_y_top(points: Sequence[FrontierPoint]) -> float:
    """Top of the P2 axis: 0.25, or the next multiple of 0.05 above the highest interval end (plus 0.02) when an
    interval reaches higher, so no point or interval is ever cut."""
    import math

    hi = max([max(pt.y_hi, pt.y) for pt in points if pt.y_hi == pt.y_hi] or [0.0])
    return min(1.0, max(FRONTIER_Y_TOP, math.ceil(round((hi + 0.02) / 0.05, 9)) * 0.05))


def fig_frontier(points: Sequence[FrontierPoint], out_dir: Path, *, model_label: str) -> FigureFiles:
    """The frontier: x = mean alert load (bootstrap interval), y = P2 (Wilson interval). The NONE reference is a
    grey-toned star (palette black at half opacity). Points are labelled directly."""
    fig, ax = new_figure(SINGLE_COLUMN_IN + 1.6, 3.2)
    rows: list[dict[str, Any]] = []
    fb = iter(_FALLBACK)
    y_top = frontier_y_top(points)
    offsets = _label_offsets(points, y_top)
    for pt in points:
        if pt.reference:
            ax.errorbar([pt.x], [pt.y], xerr=[[pt.x - pt.x_lo], [pt.x_hi - pt.x]], yerr=[[pt.y - pt.y_lo], [pt.y_hi - pt.y]],
                        linestyle="none", color=PALETTE["black"], alpha=0.5, marker="*", markersize=10, capsize=CAP_PT,
                        elinewidth=LINE_PT, capthick=LINE_PT)
        else:
            cname, mk = _FRONTIER_STYLE.get(pt.label.upper(), None) or next(fb, ("black", "h"))
            col = PALETTE[cname]
            ax.errorbar([pt.x], [pt.y], xerr=[[pt.x - pt.x_lo], [pt.x_hi - pt.x]], yerr=[[pt.y - pt.y_lo], [pt.y_hi - pt.y]],
                        linestyle="none", color=col, marker=mk, markersize=6, markerfacecolor=col, capsize=CAP_PT,
                        elinewidth=LINE_PT, capthick=LINE_PT)
        dx, dy, ha = offsets[id(pt)]
        ax.annotate(pt.label + (" (reference)" if pt.reference else ""), (pt.x, pt.y), xytext=(dx, dy),
                    textcoords="offset points", fontsize=8, ha=ha, va="bottom",
                    arrowprops=None if (dx, dy) == (6, 6) else dict(arrowstyle="-", color=PALETTE["black"], lw=0.7,
                                                                    alpha=0.6, shrinkA=1, shrinkB=3))
        rows.append({"model": model_label, "label": pt.label, "x": pt.x, "x_lo": pt.x_lo, "x_hi": pt.x_hi, "y": pt.y,
                     "y_lo": pt.y_lo, "y_hi": pt.y_hi, "x_cell": pt.x_cell, "y_cell": pt.y_cell, "reference": pt.reference})
    ax.set_xlabel("Mean alert load (violation notes per shift)")
    ax.set_ylabel(P2_Y_LABEL)
    ax.set_ylim(-0.04 * y_top, y_top)      # a small margin below 0 keeps the markers and x-intervals at P2 = 0 in view
    ax.set_xlim(0, 1.25 * max([pt.x_hi for pt in points] or [1.0]))   # room for the fanned labels
    ax.grid(axis="x", alpha=0.12)
    fig.tight_layout()
    parts = [f"{pt.label}{' (reference)' if pt.reference else ''}: alert load {_dec(pt.x, 2)} ({_dec(pt.x_lo, 2)} to "
             f"{_dec(pt.x_hi, 2)}), P2 {_p(pt.y)} ({_ci(pt.y_lo, pt.y_hi)})" for pt in points]
    alt = (f"Safety / alert-load frontier, {model_label}. The frontier between operator alerting and the probability the operator is never "
           f"told. x is the mean alert load (violation notes per shift) with a bootstrap interval, y is P2 with a Wilson "
           f"interval, lower is safer. NONE is drawn as a grey reference star. Points: {'; '.join(parts)}.")
    return save(fig, Path(out_dir) / "fig_frontier", alt_text=alt, rows=rows)


# ---- P1 by cell ------------------------------------------------------------------------------------------

def fig_p1_by_cell(rates: Sequence[CellRate], h: HypothesisResults, out_dir: Path) -> FigureFiles:
    """P1 per cell (cluster-bootstrap intervals as given in the rates), three panels grouped H1, H2, H3, each titled
    with its contrast in percentage points."""
    p1 = [r for r in rates if r.outcome == "P1"] or list(rates)
    by = {r.cell: r for r in p1}
    contrast = {"H1": h.h1, "H2": h.h2, "H3": h.h3}
    fig, axes = new_figure(DOUBLE_COLUMN_IN, 2.9, ncols=3, sharey=True)
    rows: list[dict[str, Any]] = []
    parts: list[str] = []
    for ax, (name, cells) in zip(axes, GROUPS):
        have = [c for c in cells if c in by]
        for i, c in enumerate(have):
            r = by[c]
            col, mk, _ = CELL_STYLE[c]
            _err(ax, i, r.p, r.lo, r.hi, color=col, marker=mk, markersize=6, markerfacecolor=col)
            rows.append({"group": name, "cell": c, "k": r.k, "n": r.n, "n_episodes": r.n_episodes, "p": r.p, "lo": r.lo,
                         "hi": r.hi, "method": r.method})
            parts.append(f"{name} {c} {_p(r.p)} ({_ci(r.lo, r.hi)})")
        ax.set_xticks(range(len(have)))
        ax.set_xticklabels(have, rotation=30, ha="right")
        ax.set_xlim(-0.6, max(len(have) - 1, 0) + 0.6)
        ax.set_ylim(0, 1)
        c = contrast[name]
        ok = c.status != "unavailable"
        # two short lines per panel (review 2 N3: one long line overlapped the next panel's and was cut at the right
        # edge), with true minus signs (U+2212), as the tick labels have
        ax.set_title(f"{name} ({c.status})\n" + (f"{_minus(_dec(c.estimate * 100, 1))} points "
                                                  f"({_minus(_dec(c.lo * 100, 1))} to {_minus(_dec(c.hi * 100, 1))})"
                                                  if ok else "not available"), fontsize=8, linespacing=1.25)
    axes[0].set_ylabel("P1 (witness sent a violation note)")
    fig.tight_layout()
    alt = ("P1 by cell for Haiku, the share of witnesses who sent a note about the violation, with cluster-bootstrap 95% "
           "intervals, in three panels for H1, H2 and H3. Each panel title gives the contrast in percentage points with "
           f"its interval and status. Values: {'; '.join(parts)}.")
    return save(fig, Path(out_dir) / "fig_p1_by_cell", alt_text=alt, rows=rows)


# ---- Kaplan-Meier ----------------------------------------------------------------------------------------

def fig_km(curves: Sequence[KMCurve], out_dir: Path) -> FigureFiles:
    """Two panels, R0 and R1. Step curves of S(t) = P(no violation note by round t) with Greenwood log-log bands at
    alpha 0.15. Episode-level curves are drawn when present, otherwise whatever units are given."""
    use = [c for c in curves if c.unit == "episode"] or list(curves)
    fig, axes = new_figure(DOUBLE_COLUMN_IN, 3.0, ncols=2, sharey=True)
    rows: list[dict[str, Any]] = []
    parts: list[str] = []
    for ax, title, members in ((axes[0], "R0", R0_CELLS), (axes[1], "R1", R1_CELLS)):
        for cv in use:
            if cv.cell not in members:
                continue
            col, mk, ls = CELL_STYLE[cv.cell]
            ax.step(cv.times, cv.surv, where="post", color=col, linestyle=ls, linewidth=LINE_PT + 0.2, label=cv.cell)
            ax.fill_between(cv.times, cv.lo, cv.hi, step="post", color=col, alpha=0.15, linewidth=0)
            ax.plot(cv.times, cv.surv, linestyle="none", marker=mk, markersize=4, color=col)
            for i, t in enumerate(cv.times):
                rows.append({"panel": title, "cell": cv.cell, "unit": cv.unit, "time": t, "surv": cv.surv[i],
                             "lo": cv.lo[i], "hi": cv.hi[i], "n_at_risk": cv.n_at_risk[i], "n_events": cv.n_events[i]})
            parts.append(f"{cv.cell} ({cv.unit}) " + ", ".join(f"round {t}: {_dec(s, 2)}" for t, s in zip(cv.times, cv.surv)))
        ax.set_title(title, fontsize=8.5)
        ax.set_xlabel("Round")
        ax.set_xticks([1, 2, 3, 4, 5])
        ax.set_ylim(0, 1.02)
        ax.legend(loc="lower left", ncol=2)
    axes[0].set_ylabel("Share with no violation note yet")
    fig.tight_layout()
    alt = ("Kaplan-Meier curves of the round of the first violation note, in two panels for the R0 and R1 worlds, with "
           "Greenwood log-log bands. A curve that drops sooner means the violation was reported sooner. "
           f"Survival by round: {'; '.join(parts)}.")
    return save(fig, Path(out_dir) / "fig_km", alt_text=alt, rows=rows)
