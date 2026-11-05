"""Typed result records (plan 47 2.18). Pinned: CellRate, EpisodeRate, Contrast, HypothesisResults, FrontierPoint,
KMCurve. The rest (Diff, MeanRate, CountRow, BoardSubRate, HazardRow, ...) are WP6-C additions that the plan names
only in prose; figures and tables consume them as plain records. `model` is 'haiku' or 'sonnet'.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CellRate:
    model: str
    cell: str
    outcome: str
    k: int
    n: int
    n_episodes: int
    p: float | None
    lo: float | None
    hi: float | None
    method: str
    coverage: float = 1.0


@dataclass(frozen=True, slots=True)
class EpisodeRate:
    model: str
    cell: str
    outcome: str
    k: int | None
    n: int
    p: float | None
    lo: float | None
    hi: float | None
    bound_lo: float | None = None
    bound_hi: float | None = None


@dataclass(frozen=True, slots=True)
class Contrast:
    name: str
    estimate: float
    lo: float
    hi: float
    level: float
    B: int
    stream: str
    rejected: bool | None
    direction: str          # 'predicted' (estimate > 0) | 'opposite' (< 0) | 'zero'
    status: str             # 'confirmatory' | 'descriptive' | 'exploratory' | 'unavailable'
    components: Mapping[str, float]
    note: str = ""


@dataclass(frozen=True, slots=True)
class HypothesisResults:
    h1: Contrast
    h2: Contrast
    h3: Contrast
    null_rule: str | None
    tost: tuple[float, float, str]
    positive_control: Mapping[str, object]
    stopped_at: str | None


@dataclass(frozen=True, slots=True)
class FrontierPoint:
    label: str
    x: float
    x_lo: float
    x_hi: float
    y: float
    y_lo: float
    y_hi: float
    x_cell: str
    y_cell: str
    reference: bool


@dataclass(frozen=True, slots=True)
class KMCurve:
    cell: str
    unit: str
    times: tuple[int, ...]
    surv: tuple[float, ...]
    lo: tuple[float, ...]
    hi: tuple[float, ...]
    n_at_risk: tuple[int, ...]
    n_events: tuple[int, ...]


# ---- WP6-C additions -------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Diff:
    """A Newcombe (or other) difference of two proportions, estimate = first - second."""
    name: str
    outcome: str
    estimate: float | None
    lo: float | None
    hi: float | None
    method: str
    k1: int | None = None
    n1: int | None = None
    k2: int | None = None
    n2: int | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class MeanRate:
    model: str
    cell: str
    outcome: str
    mean: float | None
    lo: float | None
    hi: float | None
    n_episodes: int
    method: str


@dataclass(frozen=True, slots=True)
class CountRow:
    model: str
    cell: str
    name: str
    value: int


@dataclass(frozen=True, slots=True)
class HazardRow:
    model: str
    cell: str
    round: int
    n_at_risk: int
    n_events: int
    hazard: float | None


@dataclass(frozen=True, slots=True)
class BoardSubRate:
    """D-010 board-substitution rate. `conditional` = the rate is k / n_noteless_known (not k / n_witnesses)."""
    model: str
    cell: str
    source: str             # 'judge' (primary, D-010) | 'final' (second version, F-22)
    n_witnesses: int
    n_noteless: int
    n_noteless_known: int
    k_qualifying: int
    p: float | None
    lo: float | None
    hi: float | None
    conditional: bool
    coverage: float
    note: str
    judged_fraction: float = 1.0     # judged / all noteless witnesses that have posts (the J2d prefix fraction)
    n_posters: int = 0               # noteless witnesses with at least one live post in rounds 1-5


@dataclass(frozen=True, slots=True)
class SecondaryResults:
    model: str
    p2: tuple[EpisodeRate, ...]
    p2prime: tuple[EpisodeRate, ...]
    diffs: tuple[Diff, ...]
    km: tuple[KMCurve, ...]
    mean_rates: tuple[MeanRate, ...]
    rates: tuple[CellRate, ...]
    delta1_no_test_aware: Contrast | None
    h2_p2: Contrast | None
    counts: tuple[CountRow, ...]
    board_substitution: tuple[BoardSubRate, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DescriptiveResults:
    model: str
    contrasts: tuple[Contrast, ...]
    diffs: tuple[Diff, ...]
    frontier: tuple[FrontierPoint, ...]
    predictions: Mapping[str, bool | None]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SensitivityResults:
    model: str
    contrasts: tuple[Contrast, ...]          # P1-any, P1-attempts lower/upper, seed-paired
    rates: tuple[CellRate, ...]              # breakdowns, QA, P1 without round 1
    hazard: tuple[HazardRow, ...]
    icc_pooled: float | None
    icc_by_cell: Mapping[str, float]
    qa: Mapping[str, object]
    notes: tuple[str, ...] = ()
    km_any: tuple[KMCurve, ...] = ()     # any-note Kaplan-Meier (D-006 sensitivity)


@dataclass(frozen=True, slots=True)
class SonnetResults:
    model: str
    p1: tuple[CellRate, ...]
    p2: tuple[EpisodeRate, ...]
    p2prime: tuple[EpisodeRate, ...]
    diffs: tuple[Diff, ...]
    ceiling: bool
    notes: tuple[str, ...] = ()
