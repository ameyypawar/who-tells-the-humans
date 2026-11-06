"""Wilson, Newcombe method 10, percentile intervals and the strict rejection rule (plan 47 2.13-2.14)."""
from __future__ import annotations

import math

import numpy as np

Z95 = 1.959963984540054


QUANT_DECIMALS = 12


def q(x: float) -> float:
    """The one shared quantizer of the confirmatory decision layer. Rates are ratios of integers, so every contrast of
    them is a rational whose distinct values differ by far more than 1e-12; rounding removes float noise (a
    mathematically-zero difference-in-differences that evaluates to 5.55e-17, a 15-point bound that evaluates to
    0.14999999999999997) before any comparison with 0 or with the 15-point margin."""
    return x if x != x else round(float(x), QUANT_DECIMALS) + 0.0


def qa(values: np.ndarray) -> np.ndarray:
    return np.round(np.asarray(values, dtype=float), QUANT_DECIMALS) + 0.0


def wilson(k: int, n: int) -> tuple[float, float] | None:
    """95% Wilson score interval, no continuity correction. None when n = 0."""
    if n <= 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    p = k / n
    z2 = Z95 * Z95
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = Z95 * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return lo, hi


def newcombe_diff(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float, float] | None:
    """Newcombe method 10 for p1 - p2: (difference, lo, hi). None if either n is 0."""
    w1, w2 = wilson(k1, n1), wilson(k2, n2)
    if w1 is None or w2 is None:
        return None
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = w1
    l2, u2 = w2
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return d, lo, hi


def percentile_interval(values: np.ndarray, level: float = 0.95) -> tuple[float, float]:
    """Percentile interval (numpy 'linear'); NaN replicates are dropped. (nan, nan) if nothing is left."""
    v = qa(values)
    v = v[~np.isnan(v)]
    if v.size == 0:
        return float("nan"), float("nan")
    a = round(100.0 * (1.0 - level) / 2.0, 10)
    lo, hi = np.percentile(v, [a, 100.0 - a], method="linear")
    return q(float(lo)), q(float(hi))


def rejects(lo: float, hi: float) -> bool:
    """A test rejects only if the interval excludes 0 strictly. An endpoint exactly at 0 does not reject (A-45)."""
    lo, hi = q(lo), q(hi)
    return bool(lo > 0 or hi < 0)
