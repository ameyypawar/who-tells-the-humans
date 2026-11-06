"""Recompute the headline numbers of the confirmatory Haiku shifts from data/release/*.csv (stdlib only).

Usage: python -I scripts/release_summary.py [release_dir]      (default: <repo>/data/release)

Prints, for phase=core, model=haiku:
  P1  per cell: witnesses with P1 / witnesses (k/n, %)       outcomes_per_witness.csv, column p1
  P2  per cell (pre-registered P2 cells only): shifts in which no witness told ops (k/n)   outcomes_per_shift.csv, column p2
  H3 point difference: P1(NO-RECEIPT) - P1(FORGED), in percentage points
Interval estimates need the cluster bootstrap in wtth.analysis and are not recomputed here.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "release"


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["phase"] == "core" and r["model"] == "haiku"]


def summarize(release_dir: Path = DEFAULT_DIR) -> dict:
    p1: dict[str, list[int]] = {}
    for r in _rows(release_dir / "outcomes_per_witness.csv"):
        k_n = p1.setdefault(r["cell"], [0, 0])
        k_n[0] += int(r["p1"])
        k_n[1] += 1
    p2: dict[str, list[int]] = {}
    for r in _rows(release_dir / "outcomes_per_shift.csv"):
        if r["p2"] == "":
            continue
        k_n = p2.setdefault(r["cell"], [0, 0])
        k_n[0] += int(r["p2"])
        k_n[1] += 1
    h3 = 100.0 * (p1["NO-RECEIPT"][0] / p1["NO-RECEIPT"][1] - p1["FORGED"][0] / p1["FORGED"][1])
    return {"p1": p1, "p2": p2, "h3_delta_points": h3}


def main(argv: list[str]) -> int:
    d = Path(argv[1]) if len(argv) > 1 else DEFAULT_DIR
    s = summarize(d)
    print("P1 per cell (witnesses with P1 / witnesses), Haiku core")
    for cell, (k, n) in sorted(s["p1"].items()):
        print(f"  {cell:<11} {k:>4}/{n:<4} {100.0 * k / n:5.1f}%")
    print("P2 per cell (shifts where no witness told ops / shifts), Haiku core")
    for cell, (k, n) in sorted(s["p2"].items()):
        print(f"  {cell:<11} {k:>4}/{n:<4} {100.0 * k / n:5.1f}%")
    print(f"H3 point difference P1(NO-RECEIPT) - P1(FORGED): {s['h3_delta_points']:.1f} points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
