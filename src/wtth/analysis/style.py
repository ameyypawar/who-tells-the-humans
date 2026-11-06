"""Accessible matplotlib defaults and the figure saver (plan 47 section 2.17).

Agg backend, Okabe-Ito palette without yellow, colour AND marker/line style for every series, 8-9 pt text for a final
width of 3.35 in (single column) or 6.9 in (double), lines >= 1.0 pt, error-bar caps 2.5 pt, light y-grid, top and
right spines off. save() writes a byte-stable PDF (fonttype 42, no creation date), a 300 dpi PNG, an .alt.txt and a
.csv of the plotted data.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")

PALETTE: Mapping[str, str] = {
    "black": "#000000", "orange": "#E69F00", "sky": "#56B4E9", "green": "#009E73",
    "blue": "#0072B2", "vermillion": "#D55E00", "purple": "#CC79A7",
}
ALLOWED_COLOURS = frozenset(v.upper() for v in PALETTE.values()) | {"#FFFFFF"}
SINGLE_COLUMN_IN = 3.35
DOUBLE_COLUMN_IN = 6.9
TEXT_PT = 8.5
LINE_PT = 1.2
CAP_PT = 2.5
P2_COLOUR, P2PRIME_COLOUR = PALETTE["blue"], PALETTE["vermillion"]

# One (colour, marker, linestyle) per cell, unique as a pair, consistent across every figure.
CELL_STYLE: Mapping[str, tuple[str, str, str]] = {
    "NONE": (PALETTE["black"], "o", "-"),
    "CONCERN": (PALETTE["orange"], "P", "--"),
    "CLAIM": (PALETTE["blue"], "s", "-"),
    "POLICY": (PALETTE["sky"], "X", "-."),
    "NONE-R": (PALETTE["vermillion"], "^", "-"),
    "RECEIPT": (PALETTE["orange"], "<", ":"),
    "NO-RECEIPT": (PALETTE["green"], "D", "--"),
    "FORGED": (PALETTE["purple"], "v", "-."),
    "NEG": (PALETTE["sky"], ">", ":"),
    "POS": (PALETTE["purple"], "h", "--"),
    "B-NONE": (PALETTE["black"], "p", ":"),
}
P2_Y_LABEL = "P(operator never told), lower is safer"


def apply_style() -> None:
    import matplotlib as mpl

    mpl.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": TEXT_PT, "axes.labelsize": TEXT_PT, "axes.titlesize": TEXT_PT,
        "xtick.labelsize": TEXT_PT - 0.5, "ytick.labelsize": TEXT_PT - 0.5, "legend.fontsize": TEXT_PT - 0.5,
        "lines.linewidth": LINE_PT, "lines.markersize": 5.0, "axes.linewidth": 1.0,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": PALETTE["black"], "axes.labelcolor": PALETTE["black"], "text.color": PALETTE["black"],
        "xtick.color": PALETTE["black"], "ytick.color": PALETTE["black"],
        "axes.grid": True, "axes.grid.axis": "y", "grid.color": PALETTE["black"], "grid.alpha": 0.12,
        "grid.linewidth": 0.6, "axes.axisbelow": True, "figure.facecolor": "#FFFFFF", "axes.facecolor": "#FFFFFF",
        "savefig.facecolor": "#FFFFFF", "legend.frameon": False, "figure.dpi": 100, "savefig.dpi": 300,
        "pdf.fonttype": 42, "ps.fonttype": 42, "pdf.compression": 6, "figure.autolayout": False,
    })


def new_figure(width_in: float, height_in: float, *, nrows: int = 1, ncols: int = 1, **kw: Any):
    import matplotlib.pyplot as plt

    apply_style()
    return plt.subplots(nrows, ncols, figsize=(width_in, height_in), **kw)


@dataclass(frozen=True, slots=True)
class FigureFiles:
    pdf: Path
    png: Path
    alt_txt: Path
    data_csv: Path


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    return str(v)


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([_cell(r.get(c)) for c in cols])


def save(fig, stem: Path, *, alt_text: str, rows: Sequence[Mapping[str, Any]]) -> FigureFiles:
    """<stem>.pdf, <stem>.png, <stem>.alt.txt, <stem>.csv. The figure is closed. Output is deterministic."""
    import matplotlib.pyplot as plt

    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    files = FigureFiles(stem.with_suffix(".pdf"), stem.with_suffix(".png"), stem.with_name(stem.name + ".alt.txt"),
                        stem.with_suffix(".csv"))
    fig.savefig(files.pdf, format="pdf", metadata={"CreationDate": None, "ModDate": None, "Creator": None,
                                                   "Producer": None, "Title": None})
    fig.savefig(files.png, format="png", dpi=300, metadata={"Software": None})
    files.alt_txt.write_text(alt_text.strip() + "\n", encoding="utf-8")
    write_csv(files.data_csv, rows)
    plt.close(fig)
    return files


def used_colours(fig) -> set[str]:
    """Upper-case #RRGGBB colours of the artists a test cares about (lines, markers, patches, collections, text)."""
    from matplotlib.colors import to_hex

    found: set[str] = set()

    def add(c: Any, alpha_ok: bool = True) -> None:
        if c is None or (isinstance(c, str) and c in ("none", "None")):
            return
        try:
            found.add(to_hex(c, keep_alpha=False).upper())
        except ValueError:
            pass

    for ax in fig.axes:
        for ln in ax.get_lines():
            add(ln.get_color())
            add(ln.get_markerfacecolor())
            add(ln.get_markeredgecolor())
        for coll in ax.collections:
            for arr in (coll.get_edgecolor(), coll.get_facecolor()):
                for c in arr:
                    if len(c) and c[-1] > 0:
                        add(tuple(c[:3]))
        for p in ax.patches:
            for c in (p.get_edgecolor(), p.get_facecolor()):
                if c[-1] > 0:
                    add(tuple(c[:3]))
        for t in ax.texts + [ax.title, ax.xaxis.label, ax.yaxis.label] + ax.get_xticklabels() + ax.get_yticklabels():
            add(t.get_color())
        leg = ax.get_legend()
        if leg:
            for t in leg.get_texts():
                add(t.get_color())
    return found
