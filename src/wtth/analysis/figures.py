"""Figures (plan 47 section 2.17): Fig. 1a, Fig. 1b, P1 by cell, Kaplan-Meier curves.

Each function writes <out_dir>/<name>.{pdf,png,alt.txt,csv} through style.save and returns FigureFiles. Every series
differs by colour AND marker or line style; colours are the Okabe-Ito palette without yellow. The alt text names every
plotted value.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from wtth.analysis.results import CellRate, EpisodeRate, FrontierPoint, HypothesisResults, KMCurve
from wtth.analysis.style import (CAP_PT, CELL_STYLE, DOUBLE_COLUMN_IN, LINE_PT, P2_COLOUR, P2_Y_LABEL, P2PRIME_COLOUR,
                                 PALETTE, SINGLE_COLUMN_IN, FigureFiles, new_figure, save)
from wtth.analysis.tables import _dec

FIG1A_R0 = ("NONE", "CONCERN", "CLAIM", "POLICY")
FIG1A_R1 = ("NONE-R", "NO-RECEIPT", "FORGED", "NEG")
R0_CELLS = ("NONE", "CONCERN", "CLAIM", "POLICY", "B-NONE")
R1_CELLS = ("NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS")
GROUPS = (("H1", ("NONE", "CLAIM")), ("H2", ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT")),
          ("H3", ("NO-RECEIPT", "FORGED")))


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


# ---- Fig. 1a ---------------------------------------------------------------------------------------------

def fig1a(p2: Sequence[EpisodeRate], p2prime: Sequence[EpisodeRate], out_dir: Path, *, model_label: str) -> FigureFiles:
    """P2 (filled circle) and P2' (open square) with Wilson 95% intervals in the eight cells; a bracketed P2' is a
    range bar between its bounds. R0 | R1 divider; tick labels show n."""
    order = FIG1A_R0 + FIG1A_R1
    pos = {c: i + (0.7 if c in FIG1A_R1 else 0.0) for i, c in enumerate(order)}
    a, b = {r.cell: r for r in p2}, {r.cell: r for r in p2prime}
    fig, ax = new_figure(DOUBLE_COLUMN_IN, 3.0)
    rows: list[dict[str, Any]] = []
    off = 0.14
    for c in order:
        x = pos[c]
        r = a.get(c)
        if r is not None:
            _err(ax, x - off, r.p, r.lo, r.hi, color=P2_COLOUR, marker="o", markersize=5.5, markerfacecolor=P2_COLOUR,
                 markeredgecolor=P2_COLOUR)
            rows.append({"model": model_label, "cell": c, "outcome": "P2", "k": r.k, "n": r.n, "p": r.p, "lo": r.lo,
                         "hi": r.hi, "bound_lo": None, "bound_hi": None})
        q = b.get(c)
        if q is not None:
            if q.p is None and q.bound_lo is not None and q.bound_hi is not None:
                ax.errorbar([x + off], [(q.bound_lo + q.bound_hi) / 2], yerr=[[(q.bound_hi - q.bound_lo) / 2],
                            [(q.bound_hi - q.bound_lo) / 2]], linestyle="none", color=P2PRIME_COLOUR, capsize=CAP_PT + 1,
                            elinewidth=3.2, capthick=LINE_PT, alpha=0.9)
            else:
                _err(ax, x + off, q.p, q.lo, q.hi, color=P2PRIME_COLOUR, marker="s", markersize=5.0,
                     markerfacecolor="#FFFFFF", markeredgecolor=P2PRIME_COLOUR, markeredgewidth=1.4)
            rows.append({"model": model_label, "cell": c, "outcome": "P2prime", "k": q.k, "n": q.n, "p": q.p, "lo": q.lo,
                         "hi": q.hi, "bound_lo": q.bound_lo, "bound_hi": q.bound_hi})
    ax.axvline((pos["POLICY"] + pos["NONE-R"]) / 2, color=PALETTE["black"], linestyle=":", linewidth=1.0, alpha=0.5)
    ax.text(pos["NONE"] - 0.4, 1.0, "R0", ha="left", va="top", fontsize=8)
    ax.text(pos["NONE-R"] - 0.4, 1.0, "R1", ha="left", va="top", fontsize=8)
    ax.set_xticks([pos[c] for c in order])
    ax.set_xticklabels([f"{c}\nn={a[c].n}" if c in a else c for c in order])
    ax.set_xlim(-0.6, pos["NEG"] + 0.6)
    ax.set_ylim(0, 1)
    ax.set_ylabel(P2_Y_LABEL)
    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([], [], color=P2_COLOUR, marker="o", linestyle="none", markersize=5.5, label="P2"),
                       Line2D([], [], color=P2PRIME_COLOUR, marker="s", linestyle="none", markerfacecolor="#FFFFFF",
                              markeredgewidth=1.4, markersize=5.0, label="P2′ (bar = bounds)")],
              loc="upper right", ncol=2)
    fig.tight_layout()
    parts = [f"{c} {_p(a[c].p)} ({_ci(a[c].lo, a[c].hi)})" for c in order if c in a]
    parts2 = [f"{c} " + (_p(b[c].p) + f" ({_ci(b[c].lo, b[c].hi)})" if b[c].p is not None
                         else f"bracketed {_p(b[c].bound_lo)} to {_p(b[c].bound_hi)}") for c in order if c in b]
    alt = (f"Figure 1a, {model_label}. Probability that the operator is never told (P2, filled circles) and the "
           f"stricter P2 prime (open squares) in eight violation cells, with Wilson 95% intervals; lower is safer. "
           f"P2 by cell, as percent with the interval: {'; '.join(parts)}. "
           f"P2 prime: {'; '.join(parts2)}.")
    return save(fig, Path(out_dir) / "fig1a", alt_text=alt, rows=rows)


# ---- Fig. 1b ---------------------------------------------------------------------------------------------

_FRONTIER_STYLE = {"CLAIM": ("blue", "o"), "POLICY": ("green", "^"), "RECEIPT": ("orange", "D"),
                   "RECEIPTS": ("orange", "D"), "RECEIPT ARM": ("orange", "D"), "FORGED": ("vermillion", "s")}
_FALLBACK = (("purple", "v"), ("sky", "P"), ("vermillion", "X"))


def _label_offsets(points: Sequence[FrontierPoint]) -> dict[int, tuple[float, float, str]]:
    """Label offsets (points) and alignment. A point alone gets (6, 6). Points that sit on top of each other (x within
    0.25 and y within 0.08 of one another, e.g. CLAIM, POLICY and NONE all at zero) are fanned out left, centre and
    right above the cluster at staggered heights, each tied to its marker by a leader line."""
    out: dict[int, tuple[float, float, str]] = {id(pt): (6.0, 6.0, "left") for pt in points}
    seen: set[int] = set()
    for pt in points:
        if id(pt) in seen:
            continue
        near = [q for q in points if abs(q.x - pt.x) <= 0.25 and abs((q.y or 0.0) - (pt.y or 0.0)) <= 0.08]
        if len(near) < 2:
            continue
        near.sort(key=lambda q: q.x)
        slots = [(-30.0, 34.0, "right"), (0.0, 62.0, "center"), (30.0, 34.0, "left"), (0.0, 90.0, "center")]
        for q, slot in zip(near, slots):
            out[id(q)] = slot
            seen.add(id(q))
    return out


def fig1b(points: Sequence[FrontierPoint], out_dir: Path, *, model_label: str) -> FigureFiles:
    """The frontier: x = mean alert load (bootstrap interval), y = P2 (Wilson interval). The NONE reference is a
    grey-toned star (palette black at half opacity). Points are labelled directly."""
    fig, ax = new_figure(SINGLE_COLUMN_IN + 1.6, 3.2)
    rows: list[dict[str, Any]] = []
    fb = iter(_FALLBACK)
    offsets = _label_offsets(points)
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
    ax.set_xlabel("Mean alert load (qualifying notes per episode)")
    ax.set_ylabel(P2_Y_LABEL)
    ax.set_ylim(0, 1)
    ax.set_xlim(0, 1.25 * max([pt.x_hi for pt in points] or [1.0]))   # room for the fanned labels
    ax.grid(axis="x", alpha=0.12)
    fig.tight_layout()
    parts = [f"{pt.label}{' (reference)' if pt.reference else ''}: alert load {_dec(pt.x, 2)} ({_dec(pt.x_lo, 2)} to "
             f"{_dec(pt.x_hi, 2)}), P2 {_p(pt.y)} ({_ci(pt.y_lo, pt.y_hi)})" for pt in points]
    alt = (f"Figure 1b, {model_label}. The frontier between operator alerting and the probability the operator is never "
           f"told. x is the mean alert load per episode with a bootstrap interval, y is P2 with a Wilson interval, "
           f"lower is safer. NONE is drawn as a grey reference star. Points: {'; '.join(parts)}.")
    return save(fig, Path(out_dir) / "fig1b", alt_text=alt, rows=rows)


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
        ax.set_title(f"{name}: " + (f"{_dec(c.estimate * 100, 1)} pp [{_dec(c.lo * 100, 1)}, {_dec(c.hi * 100, 1)}]"
                                    if ok else "not available") + f" ({c.status})", fontsize=8)
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
