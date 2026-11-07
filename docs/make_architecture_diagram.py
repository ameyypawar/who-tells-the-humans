#!/usr/bin/env python3
"""Generate docs/o2-receipts-architecture.{png,pdf}.

Run: /Users/amey/.local/wtth/venv/bin/python -I docs/make_architecture_diagram.py
Pure matplotlib (patches + annotations); no external assets.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = ["Helvetica", "Arial", "DejaVu Sans"]

BLUE, ORANGE, PURPLE = "#0072B2", "#E69F00", "#5B5FC7"
GREEN_T, GREEN_E = "#E3F4E6", "#4C9F5A"
BLUE_T, ORANGE_T, PURPLE_T = "#E6F1F8", "#FDF1D8", "#ECECFA"
DARK, GREY = "#1B1B1B", "#666666"

W, H = 160, 92
fig = plt.figure(figsize=(16, 9.2), dpi=125)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")


def box(cx, cy, w, h, text, fc, ec, fs=10.5, bold_first=False, lw=1.8, ls="-", tc=DARK):
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                 boxstyle="round,pad=0,rounding_size=1.2", fc=fc, ec=ec, lw=lw, ls=ls, zorder=3))
    lines = text.split("\n")
    if bold_first and len(lines) > 1:
        n = len(lines); step = 2.1 if fs < 11 else 2.3
        top = cy + (n - 1) * step / 2
        for i, l in enumerate(lines):
            ax.text(cx, top - i * step, l, ha="center", va="center", fontsize=fs,
                    fontweight="bold" if i == 0 else "normal", color=tc, zorder=4)
    else:
        ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, color=tc,
                linespacing=1.35, zorder=4)


def arrow(p0, p1, color=DARK, lw=2.0, ls="-", head=True):
    ax.annotate("", xy=p1, xytext=p0, zorder=5,
                arrowprops=dict(arrowstyle="-|>" if head else "-", color=color, lw=lw,
                                ls=ls, shrinkA=0, shrinkB=0, mutation_scale=18))


def poly(pts, color, lw=2.0, ls="-"):
    xs, ys = zip(*pts[:-1])
    ax.plot(xs, ys, color=color, lw=lw, ls=ls, zorder=5, solid_capstyle="butt")
    arrow(pts[-2], pts[-1], color, lw, ls)


def label(x, y, s, fs=9.5, color=GREY, ha="center", style="normal", weight="normal"):
    ax.text(x, y, s, ha=ha, va="center", fontsize=fs, color=color,
            fontstyle=style, fontweight=weight, zorder=6, linespacing=1.3)


# Title
ax.text(80, 88.3, "Checkable escalation receipts on OpenObserve", ha="center", va="center",
        fontsize=19, fontweight="bold", color=DARK)

# Lane backgrounds + headers
lanes = [(2, 51, "1  Write path: channel-written"),
         (56, 50, "2  Read path: checkable"),
         (108, 50, "3  Operator monitor")]
for x, w, t in lanes:
    ax.add_patch(FancyBboxPatch((x, 8), w, 74, boxstyle="round,pad=0,rounding_size=1.5",
                 fc="#FAFAFB", ec="#CFCFD6", lw=1.2, zorder=1))
    ax.text(x + w / 2, 78.6, t, ha="center", va="center", fontsize=13, fontweight="bold", color=DARK)

# ---------- Lane 1 ----------
box(13, 68, 18, 8, "Witness agent", BLUE_T, BLUE)
box(40, 68, 24, 8, "Channel\nnotify_ops handler", GREEN_T, GREEN_E, bold_first=True)
arrow((22, 68), (28, 68), BLUE)
label(25, 70.8, "calls", fs=9)
box(13, 52, 18, 9, "Operator\n(ops)", "#F2F2F2", "#555555", bold_first=True)
arrow((33, 64), (22, 55.5), GREEN_E)
label(21, 62, "private note", fs=9.5, ha="center")
box(40, 49, 25, 13, "OpenObserve\nstream: escalation_receipts\nentry id, clock,\nsender, subject",
    PURPLE_T, PURPLE, bold_first=True, fs=10)
arrow((40, 64), (40, 55.5), GREEN_E)
label(41.6, 60, "writes receipt,\nsame moment", fs=9.5, color=GREEN_E, weight="bold", ha="left")
ax.text(13, 43.5, "receipts are written\nby the channel only", ha="center", va="center", fontsize=9.5,
        fontstyle="italic", color=GREEN_E, zorder=6, linespacing=1.3)

# threat
ax.add_patch(FancyBboxPatch((3.5, 12), 48, 24.5, boxstyle="round,pad=0,rounding_size=1.2",
             fc="none", ec=ORANGE, lw=1.4, ls=(0, (4, 3)), zorder=2))
ax.text(5.2, 34.2, "THREAT", ha="left", va="center", fontsize=9.5, fontweight="bold", color="#B87800")
box(13, 21, 18, 11, "Peer types a\nreceipt-style line\ninto its own post", ORANGE_T, ORANGE, fs=9.5)
box(40, 21, 14, 8, "Board post", ORANGE_T, ORANGE)
arrow((22, 21), (33, 21), ORANGE)
label(27.5, 25, "lands on\nboard", fs=9, color="#B87800")
# blocked arrow: board post -> stream
arrow((40, 25), (40, 36), ORANGE, ls=(0, (3, 2)), head=False)
ax.plot([40, 40], [36, 42.2], color=ORANGE, lw=2.0, ls=(0, (3, 2)), zorder=5)
ax.text(40, 31, "X", ha="center", va="center", fontsize=17, fontweight="bold", color="#C0392B", zorder=7)
label(41.8, 39, "cannot\nwrite", fs=9, color="#C0392B", ha="left", weight="bold")

# ---------- Lane 2 ----------
box(81, 68, 22, 8, "Another agent", BLUE_T, BLUE)
box(81, 55, 30, 9, "lookup_escalation\nMCP tool", "#F4F4F8", PURPLE, bold_first=True)
arrow((81, 64), (81, 59.5), BLUE)
label(85.5, 61.8, "calls", fs=9, ha="left")
# query / result to stream (lane 1)
arrow((66, 57), (53, 52.5), PURPLE)
label(66, 62.5, "SQL query\nby entry id", fs=9, color=PURPLE)
arrow((53, 47.5), (66, 52.5), PURPLE, ls=(0, (3, 2)))
box(70, 36, 22, 11, "Entry found\nid, clock, sender,\nsubject", GREEN_T, GREEN_E, fs=9.5)
box(94, 36, 22, 11, "Forged claim\n'entry #4812'\nNo matching entry", ORANGE_T, ORANGE, fs=9.5)
arrow((78, 50.5), (70, 41.7), GREEN_E)
arrow((88, 50.5), (94, 41.7), ORANGE)
label(81, 29.2, "A claim is checkable: trust the stream, not the post.", fs=9.5, color=GREY, style="italic")

# ---------- Lane 3 ----------
ax.add_patch(FancyBboxPatch((112, 33), 44, 42, boxstyle="round,pad=0,rounding_size=1.5",
             fc=PURPLE_T, ec=PURPLE, lw=1.8, zorder=2))
ax.text(134, 72.6, "OpenObserve", ha="center", va="center", fontsize=12, fontweight="bold", color=PURPLE)
box(134, 65.5, 38, 8, "streams: board_posts, notes,\nescalation_receipts", "white", PURPLE, fs=9.5)
box(134, 52.5, 38, 13, "Alert: claim without receipt\nSQL over posts and notes:\npost says ops was told, but the\nnamed agent has no receipt",
    "white", PURPLE, fs=9, bold_first=True)
arrow((134, 61.5), (134, 59), PURPLE)
box(134, 39.5, 38, 8, "FLAGGED: agent-B post says\n'ops told', no receipt for agent-B", ORANGE_T, ORANGE, fs=9)
arrow((134, 46), (134, 43.7), ORANGE)
box(122, 24, 18, 8, "Dashboard", PURPLE_T, PURPLE)
box(145, 24, 20, 8, "Alert\nnotification", PURPLE_T, PURPLE)
arrow((122, 33), (122, 28), PURPLE)
arrow((145, 33), (145, 28), PURPLE)
box(134, 12.5, 24, 6.5, "Operator", "#F2F2F2", "#555555")
arrow((122, 20), (128, 15.8), PURPLE)
arrow((145, 20), (140, 15.8), PURPLE)

# board -> OpenObserve (ingest), corridor under lane 2
poly([(47, 21), (109, 21), (109, 65.5), (115, 65.5)], ORANGE, lw=1.8)
label(81, 19.2, "board posts and notes ingested", fs=9.5, color="#B87800")

# Legend
lx = 42
for fc, ec, t in [(BLUE_T, BLUE, "agent"), (GREEN_T, GREEN_E, "channel-written receipt"),
                  (ORANGE_T, ORANGE, "forged / threat path"), (PURPLE_T, PURPLE, "OpenObserve")]:
    ax.add_patch(FancyBboxPatch((lx, 2.2), 2.6, 2.2, boxstyle="round,pad=0,rounding_size=0.5",
                 fc=fc, ec=ec, lw=1.5, zorder=3))
    ax.text(lx + 3.6, 3.3, t, ha="left", va="center", fontsize=10, color=DARK)
    lx += 3.6 + len(t) * 0.8 + 5

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "o2-receipts-architecture")
fig.savefig(out + ".png", dpi=125, facecolor="white")
fig.savefig(out + ".pdf", facecolor="white")
print("wrote", out)
