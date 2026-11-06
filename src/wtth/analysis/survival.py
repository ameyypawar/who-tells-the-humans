"""Kaplan-Meier with log-log Greenwood intervals and the hazard by round (plan 47 2.15, F-23)."""
from __future__ import annotations

import math
from collections.abc import Sequence

from wtth.analysis.intervals import Z95
from wtth.analysis.results import HazardRow, KMCurve


def kaplan_meier(first_rounds: Sequence[int | None], *, cell: str, unit: str, horizon: int = 5) -> KMCurve:
    """Discrete rounds 1..horizon. `first_rounds[i]` is the round of the first event, None = censored after the horizon.

    S(t) = prod (1 - d_j / n_j); Greenwood sum G(t) = sum d_j / (n_j (n_j - d_j)); interval S^exp(+-z sigma) with
    sigma = sqrt(G) / |ln S|, clipped to [0, 1]. S = 1 gives [1, 1]; S = 0 gives [0, 0].
    """
    times, surv, lo, hi, nrisk, nev = [], [], [], [], [], []
    s, g = 1.0, 0.0
    dead = False
    for t in range(1, horizon + 1):
        n = sum(1 for r in first_rounds if r is None or r >= t)
        d = sum(1 for r in first_rounds if r == t)
        if n > 0 and d > 0:
            if d == n:
                s, dead = 0.0, True
            else:
                s *= 1.0 - d / n
                g += d / (n * (n - d))
        times.append(t)
        nrisk.append(n)
        nev.append(d)
        surv.append(s)
        if dead or s <= 0.0:
            lo.append(0.0)
            hi.append(0.0)
        elif s >= 1.0 or g == 0.0:
            lo.append(1.0)
            hi.append(1.0)
        else:
            sigma = math.sqrt(g) / abs(math.log(s))
            lo.append(max(0.0, min(1.0, s ** math.exp(Z95 * sigma))))
            hi.append(max(0.0, min(1.0, s ** math.exp(-Z95 * sigma))))
    return KMCurve(cell, unit, tuple(times), tuple(surv), tuple(lo), tuple(hi), tuple(nrisk), tuple(nev))


def hazard_by_round(first_rounds: Sequence[int | None], *, model: str, cell: str, horizon: int = 5) -> list[HazardRow]:
    out = []
    for t in range(1, horizon + 1):
        n = sum(1 for r in first_rounds if r is None or r >= t)
        d = sum(1 for r in first_rounds if r == t)
        out.append(HazardRow(model, cell, t, n, d, (d / n) if n else None))
    return out
