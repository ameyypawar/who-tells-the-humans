"""Episode-level cluster bootstrap (plan 47 2.13, PREREG 16).

Episodes are resampled with replacement within each cell; the witnesses of an episode move together. The draw order
is pinned: for each replicate b, for each cell in sorted() order, idx = rng.integers(0, n_c, size=n_c).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class CellData:
    cell: str
    y: np.ndarray   # per episode: number of successes (witnesses with the outcome; or the episode's value)
    m: np.ndarray   # per episode: number of units at risk (3 witnesses; 1 for episode-level means)


def _rate(y: np.ndarray, m: np.ndarray) -> float:
    tot = m.sum()
    return float("nan") if tot == 0 else float(y.sum() / tot)


def cell_rate(cd: CellData) -> float:
    return _rate(cd.y, cd.m)


def cluster_bootstrap_multi(cells: Mapping[str, CellData], contrasts: Mapping[str, Callable[[Mapping[str, float]], float]],
                            *, rng: np.random.Generator, B: int) -> dict[str, np.ndarray]:
    """One pass of B replicates; every contrast is evaluated on the same resampled cell rates. NaN if any rate is NaN."""
    names = sorted(cells)
    ys = {c: np.asarray(cells[c].y, dtype=float) for c in names}
    ms = {c: np.asarray(cells[c].m, dtype=float) for c in names}
    sizes = {c: len(ys[c]) for c in names}
    for c in names:
        if sizes[c] == 0:
            raise ValueError(f"cell {c!r} has no episodes")
        if len(ms[c]) != sizes[c]:
            raise ValueError(f"cell {c!r}: y and m differ in length")
    out = {k: np.empty(B, dtype=float) for k in contrasts}
    for b in range(B):
        rates: dict[str, float] = {}
        for c in names:
            idx = rng.integers(0, sizes[c], size=sizes[c])
            rates[c] = _rate(ys[c][idx], ms[c][idx])
        bad = any(r != r for r in rates.values())
        for k, fn in contrasts.items():
            out[k][b] = float("nan") if bad else fn(rates)
    return out


def cluster_bootstrap(cells: Mapping[str, CellData], contrast: Callable[[Mapping[str, float]], float], *,
                      rng: np.random.Generator, B: int) -> np.ndarray:
    return cluster_bootstrap_multi(cells, {"c": contrast}, rng=rng, B=B)["c"]


def seed_paired_bootstrap(cells_by_seed: Mapping[str, Mapping[int, tuple[float, float]]],
                          contrast: Callable[[Mapping[str, float]], float], *, rng: np.random.Generator,
                          B: int) -> np.ndarray:
    """Exploratory: resample seeds (shared by all the cells) instead of episodes. A cell's seed entry is (y, m)."""
    names = sorted(cells_by_seed)
    common = sorted(set.intersection(*(set(cells_by_seed[c]) for c in names)))
    if not common:
        raise ValueError("no seeds shared by all cells")
    ys = {c: np.array([cells_by_seed[c][s][0] for s in common], dtype=float) for c in names}
    ms = {c: np.array([cells_by_seed[c][s][1] for s in common], dtype=float) for c in names}
    n = len(common)
    out = np.empty(B, dtype=float)
    for b in range(B):
        idx = rng.integers(0, n, size=n)
        rates = {c: _rate(ys[c][idx], ms[c][idx]) for c in names}
        out[b] = float("nan") if any(r != r for r in rates.values()) else contrast(rates)
    return out
