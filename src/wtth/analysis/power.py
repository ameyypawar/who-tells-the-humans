"""ICC(1) and the power table of research/42 3.4, recomputed at F (plan 47 2.15)."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from wtth.analysis.bootstrap import CellData

Z = 1.96
DELTAS = (0, 10, 15)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _anova(y: np.ndarray, k: int, centre: np.ndarray | float) -> tuple[float, float]:
    """(sum of squares between, sum of squares within) for episodes with y successes of k binary observations."""
    ybar = y / k
    ssb = float(k * np.sum((ybar - centre) ** 2))
    ssw = float(np.sum(y * (k - y) / k))
    return ssb, ssw


def icc1(cells: Mapping[str, CellData]) -> tuple[float, Mapping[str, float]]:
    """One-way ANOVA ICC(1) with k witnesses per episode: per cell, and pooled within cells (episode means centred on
    their cell mean, between df = E - C). NaN where undefined (one episode, k < 2, or no variance at all)."""
    per: dict[str, float] = {}
    ssb_tot = ssw_tot = 0.0
    n_ep = n_cells = 0
    k_all = 0
    for c, cd in sorted(cells.items()):
        y, m = np.asarray(cd.y, dtype=float), np.asarray(cd.m, dtype=float)
        if len(y) == 0:
            continue
        k = int(round(float(m.mean())))
        k_all = max(k_all, k)
        n = len(y)
        if k < 2 or n < 2:
            per[c] = float("nan")
            continue
        ssb, ssw = _anova(y, k, float(y.sum() / (n * k)))
        msb, msw = ssb / (n - 1), ssw / (n * (k - 1))
        den = msb + (k - 1) * msw
        per[c] = float("nan") if den == 0 else (msb - msw) / den
        ssb_tot += ssb
        ssw_tot += ssw
        n_ep += n
        n_cells += 1
    if k_all < 2 or n_ep - n_cells < 1:
        return float("nan"), per
    msb, msw = ssb_tot / (n_ep - n_cells), ssw_tot / (n_ep * (k_all - 1))
    den = msb + (k_all - 1) * msw
    return (float("nan") if den == 0 else (msb - msw) / den), per


def _row(se: float) -> dict[str, Any]:
    pts = 100.0 * se
    return {"se_points": pts, "mde_points": 2.8 * pts,
            "p_null_met": {str(d): _phi((15.0 - Z * pts - d) / pts) for d in DELTAS}}


def power_table(p: float, rho: float, n_by_cell: Mapping[str, int]) -> Mapping[str, Any]:
    """n_eff = 3n / (1 + 2 rho); SE = sqrt(sum p(1-p)/n_eff); MDE = 2.8 SE; P(null met | delta) =
    Phi((15 - 1.96 SE - delta) / SE), all in percentage points. H1 = NONE vs CLAIM, H2 = the four cells, H3 =
    NO-RECEIPT vs FORGED; P2 is the episode-level contrast NONE vs CLAIM (n episodes, no design effect)."""
    def neff(c: str) -> float:
        return 3.0 * n_by_cell[c] / (1.0 + 2.0 * rho)

    v = p * (1.0 - p)
    out: dict[str, Any] = {"p": p, "rho": rho, "n_by_cell": dict(n_by_cell)}
    out["H1"] = _row(math.sqrt(v / neff("NONE") + v / neff("CLAIM")))
    out["H3"] = _row(math.sqrt(v / neff("NO-RECEIPT") + v / neff("FORGED")))
    out["H2"] = _row(math.sqrt(sum(v / neff(c) for c in ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT"))))
    out["P2"] = {"se_points": 100.0 * math.sqrt(v / n_by_cell["NONE"] + v / n_by_cell["CLAIM"]),
                 "mde_points": 2.8 * 100.0 * math.sqrt(v / n_by_cell["NONE"] + v / n_by_cell["CLAIM"])}
    return out


def pilot_p1(gate_log: Sequence[Mapping[str, Any]], frozen_level: str | None) -> float | None:
    """The G1 pilot P1 share at F: the latest G1 gate_log row whose level is the frozen level."""
    best = None
    for r in gate_log:
        if r.get("gate") != "G1":
            continue
        num = r.get("numbers")
        if num is None:
            raw = r.get("numbers_json")
            num = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        lvl = r.get("level") or num.get("level")
        if frozen_level is not None and lvl != frozen_level:
            continue
        if num.get("share") is None:
            continue
        key = (r.get("ts") or 0.0, r.get("id") or 0)
        if best is None or key >= best[0]:
            best = (key, float(num["share"]))
    return None if best is None else best[1]
