"""configs/analysis.yaml loader (plan 47 F-01). The file is written by the lead in the freeze commit.

WP6 refuses to run without it: a missing or malformed file is an error, never a default.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SEED_KEYS = ("bootstrap", "judge_order", "first_batch", "h5_sample", "qa_sample", "blind_sample", "export_order")


class AnalysisConfigError(ValueError):
    """configs/analysis.yaml is malformed."""


class MissingAnalysisConfig(RuntimeError):
    """configs/analysis.yaml does not exist (the lead adds it in the freeze commit, F-01)."""


@dataclass(frozen=True, slots=True)
class AnalysisSeeds:
    bootstrap_B: int
    bootstrap: int
    judge_order: int
    first_batch: int
    h5_sample: int
    qa_sample: int
    blind_sample: int
    export_order: int
    j2_order: tuple[str, ...]
    kappa_threshold: float


def analysis_yaml_path(settings: Any) -> Path:
    return Path(settings.repo_root) / "configs" / "analysis.yaml"


def parse_analysis_seeds(raw: Any, where: str = "analysis.yaml") -> AnalysisSeeds:
    if not isinstance(raw, dict):
        raise AnalysisConfigError(f"{where}: top level must be a mapping")
    seeds = raw.get("seeds")
    if not isinstance(seeds, dict):
        raise AnalysisConfigError(f"{where}: 'seeds' mapping is missing")
    missing = [k for k in SEED_KEYS if k not in seeds]
    if missing:
        raise AnalysisConfigError(f"{where}: seeds missing {missing}")
    for k in SEED_KEYS:
        if isinstance(seeds[k], bool) or not isinstance(seeds[k], int):
            raise AnalysisConfigError(f"{where}: seeds.{k} must be an integer")
    b = raw.get("bootstrap_B")
    if isinstance(b, bool) or not isinstance(b, int) or b < 1:
        raise AnalysisConfigError(f"{where}: bootstrap_B must be a positive integer")
    order = raw.get("j2_order")
    if not isinstance(order, list) or not order or not all(isinstance(x, str) for x in order):
        raise AnalysisConfigError(f"{where}: j2_order must be a non-empty list of sub-tier names")
    if len(set(order)) != len(order):
        raise AnalysisConfigError(f"{where}: j2_order has duplicates")
    kt = raw.get("kappa_threshold")
    if isinstance(kt, bool) or not isinstance(kt, (int, float)) or not 0 < float(kt) <= 1:
        raise AnalysisConfigError(f"{where}: kappa_threshold must be a number in (0, 1]")
    return AnalysisSeeds(bootstrap_B=int(b), bootstrap=int(seeds["bootstrap"]),
                         judge_order=int(seeds["judge_order"]), first_batch=int(seeds["first_batch"]),
                         h5_sample=int(seeds["h5_sample"]), qa_sample=int(seeds["qa_sample"]),
                         blind_sample=int(seeds["blind_sample"]), export_order=int(seeds["export_order"]),
                         j2_order=tuple(order), kappa_threshold=float(kt))


def load_analysis_seeds(settings: Any, path: Path | None = None) -> AnalysisSeeds:
    """Read configs/analysis.yaml (or `path`). MissingAnalysisConfig if the file is absent."""
    p = Path(path) if path is not None else analysis_yaml_path(settings)
    if not p.is_file():
        raise MissingAnalysisConfig(
            f"{p} does not exist: the lead writes configs/analysis.yaml (bootstrap_B, seeds, j2_order, "
            f"kappa_threshold) in the freeze commit (plan 47 F-01); WP6 will not guess these values")
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return parse_analysis_seeds(raw, str(p))
