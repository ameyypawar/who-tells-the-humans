"""NumberBook: every printed number is generated here, never typed (plan 47 2.16).

Keys are letters only (digits spelled out) so `\\wtth<Key>` is a valid LaTeX control word. Formatting is fixed:
decimals use ROUND_HALF_UP on the shortest decimal representation, proportions are percent with one decimal,
differences are percentage points with one decimal, negatives are `\\ensuremath{-}x`, intervals read `lo to hi`,
counts use `{,}` thousands separators, kappa has two decimals, USD is `\\$x.xx`.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from wtth.analysis.results import CellRate, Contrast

KEY_RE = re.compile(r"^[A-Z][A-Za-z]*$")
DIGIT_WORDS = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six", "7": "Seven",
               "8": "Eight", "9": "Nine"}
MODEL_TOKENS = {"haiku": "Haiku", "sonnet": "Sonnet"}
CELL_TOKENS = {"NONE": "None", "CONCERN": "Concern", "CLAIM": "Claim", "POLICY": "Policy", "NONE-R": "NoneR",
               "RECEIPT": "Receipt", "NO-RECEIPT": "NoReceipt", "FORGED": "Forged", "NEG": "Neg", "POS": "Pos",
               "B-NONE": "BNone"}
OUTCOME_TOKENS = {
    "p1": "POne", "p1_any": "POneAny", "p1_att_lo": "POneAttLo", "p1_att_hi": "POneAttHi", "p2": "PTwo",
    "p2prime": "PTwoPrime", "p2prime_lo_bound": "PTwoPrimeLoBound", "p2prime_hi_bound": "PTwoPrimeHiBound",
    "alert_load": "AlertLoad", "any_note": "AnyNote", "false_report": "FalseReport", "concern_witness": "ConcernW",
    "concern_episode": "ConcernE", "d1:test_or_simulation": "DOneTest", "believes_yes": "BelievesYes",
    "phantom_any": "Phantom", "phantom_viol": "PhantomViol", "board_sub": "BoardSub"}
STAT_TOKENS = ("K", "N", "Eps", "Pct", "Lo", "Hi", "CI", "Mean", "Delta", "Verdict", "Direction", "Status", "Sentence")
KINDS = ("count", "plain", "prop", "pp", "ci", "kappa", "kappaci", "mean", "meanci", "pts", "usd", "verdict", "text",
         "latex")
VERDICTS = ("rejected", "not rejected", "rejected in the opposite direction", "descriptive", "unavailable",
            "no meaningful stand-down", "inconclusive", "equivalent", "not equivalent", "met", "not met",
            "not available", "predicted", "opposite", "zero", "confirmatory", "exploratory", "pass", "fail",
            "yes", "no")
MINUS = r"\ensuremath{-}"


def spell_digits(s: str) -> str:
    return "".join(DIGIT_WORDS.get(ch, ch) for ch in s)


def token(s: str) -> str:
    """CamelCase letters-only token of an arbitrary label ('believes_yes|truth_any=1' -> 'BelievesYesTruthAnyOne')."""
    parts = re.split(r"[^A-Za-z0-9]+", s)
    return "".join(spell_digits(p[:1].upper() + p[1:]) for p in parts if p)


def outcome_token(outcome: str) -> str:
    return OUTCOME_TOKENS.get(outcome) or token(outcome)


def cell_token(cell: str) -> str:
    return CELL_TOKENS.get(cell) or token(cell)


def rate_prefix(model: str, outcome: str, cell: str) -> str:
    return MODEL_TOKENS.get(model, token(model)) + outcome_token(outcome) + cell_token(cell)


def macro_name(key: str) -> str:
    if not KEY_RE.match(key):
        raise ValueError(f"invalid number key {key!r}: letters only, first letter upper case (digits are spelled out)")
    return "\\wtth" + key


_LATEX_MAP = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
              "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def latex_escape(s: str) -> str:
    return "".join(_LATEX_MAP.get(ch, ch) for ch in s)


def _dec(value: float | int | str) -> Decimal:
    return Decimal(repr(value) if isinstance(value, float) else str(value))


def _pp(value: float) -> str:
    """Percentage points, one decimal; two when 0 < |value| < 0.05 pp so a tiny non-zero endpoint is not shown as 0.0."""
    v = _dec(value) * 100
    return _fixed(value, 2, 100) if v != 0 and abs(v) < Decimal("0.05") else _fixed(value, 1, 100)


def _fixed(value: float, places: int, scale: int = 1) -> str:
    """ROUND_HALF_UP of value*scale to `places` decimals; the sign is written as \\ensuremath{-} unless it rounds to 0."""
    d = (_dec(value) * scale).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if d == 0:
        d = abs(d)
    s = f"{abs(d):.{places}f}"
    return MINUS + s if d < 0 else s


def _count(value: int) -> str:
    n = int(value)
    s = f"{abs(n):,}".replace(",", "{,}")
    return MINUS + s if n < 0 else s


def fmt_value(value: Any, kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if kind == "count":
        if isinstance(value, float) and value != int(value):
            raise ValueError(f"count must be an integer, got {value!r}")
        return _count(int(value))
    if kind == "plain":
        return str(int(value)) if isinstance(value, (int, float)) and float(value) == int(value) else str(value)
    if kind == "prop":
        return _fixed(value, 1, 100)
    if kind == "pp":
        return _pp(value)
    if kind == "ci":
        lo, hi = value
        if lo is None or hi is None or lo != lo or hi != hi:
            return "n/a"
        return f"{_pp(lo)} to {_pp(hi)}"
    if kind == "kappa":
        return _fixed(value, 2)
    if kind == "kappaci":
        lo, hi = value
        if lo is None or hi is None or lo != lo or hi != hi:
            return "n/a"
        return f"{_fixed(lo, 2)} to {_fixed(hi, 2)}"
    if kind == "mean":
        return _fixed(value, 2)
    if kind == "meanci":
        lo, hi = value
        if lo is None or hi is None or lo != lo or hi != hi:
            return "n/a"
        return f"{_fixed(lo, 2)} to {_fixed(hi, 2)}"
    if kind == "pts":
        return _fixed(value, 1)
    if kind == "usd":
        d = _dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ("\\$" if d >= 0 else MINUS + "\\$") + f"{abs(d):.2f}"
    if kind == "verdict":
        if value not in VERDICTS:
            raise ValueError(f"verdict {value!r} is not in the fixed vocabulary")
        return str(value)
    if kind == "text":
        return latex_escape(str(value))
    return str(value)          # 'latex': the caller has escaped it


@dataclass(frozen=True, slots=True)
class Entry:
    key: str
    value: Any
    kind: str
    display: str
    source: str
    note: str


def _jsonable(v: Any) -> Any:
    if isinstance(v, (tuple, list)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


class NumberBook:
    def __init__(self) -> None:
        self._e: dict[str, Entry] = {}

    def __contains__(self, key: str) -> bool:
        return key in self._e

    def __len__(self) -> int:
        return len(self._e)

    def keys(self) -> list[str]:
        return sorted(self._e)

    def entry(self, key: str) -> Entry:
        return self._e[key]

    def value(self, key: str) -> Any:
        return self._e[key].value

    def add(self, key: str, value: Any, kind: str, *, source: str, note: str = "") -> None:
        macro_name(key)
        if key in self._e:
            raise ValueError(f"duplicate number key {key!r}")
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        display = fmt_value(value, kind)
        self._e[key] = Entry(key, value, kind, display, source, note)

    def fmt(self, key: str) -> str:
        return self._e[key].display

    def add_rate(self, prefix: str, r: CellRate) -> None:
        src = f"{r.model} {r.cell} {r.outcome} ({r.method})"
        self.add(prefix + "K", r.k, "count", source=src)
        self.add(prefix + "N", r.n, "count", source=src)
        self.add(prefix + "Eps", r.n_episodes, "count", source=src)
        self.add(prefix + "Pct", r.p, "prop", source=src)
        self.add(prefix + "Lo", r.lo, "prop", source=src)
        self.add(prefix + "Hi", r.hi, "prop", source=src)
        self.add(prefix + "CI", (r.lo, r.hi), "ci", source=src)
        self.add(prefix + "Cov", r.coverage, "prop", source=src, note="share of units with a known label")

    def add_contrast(self, prefix: str, c: Contrast) -> None:
        src = f"{c.name} stream={c.stream} B={c.B} level={c.level}"
        self.add(prefix + "Delta", c.estimate, "pp", source=src)
        self.add(prefix + "Lo", c.lo, "pp", source=src)
        self.add(prefix + "Hi", c.hi, "pp", source=src)
        self.add(prefix + "CI", (c.lo, c.hi), "ci", source=src)
        self.add(prefix + "B", c.B, "count", source=src)
        self.add(prefix + "Status", c.status if c.status in VERDICTS else "unavailable", "verdict", source=src)
        self.add(prefix + "Direction", c.direction, "verdict", source=src)
        self.add(prefix + "Verdict", contrast_verdict(c), "verdict", source=src)

    def write_json(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = [{"key": e.key, "value": _jsonable(e.value), "kind": e.kind, "display": e.display, "source": e.source,
                "note": e.note} for e in (self._e[k] for k in sorted(self._e))]
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    def write_macros(self, path: Path, *, status: str) -> None:
        """status 'FINAL' -> macros.tex; 'PROVISIONAL' -> macros.provisional.tex only (never macros.tex)."""
        path = Path(path)
        if status not in ("FINAL", "PROVISIONAL"):
            raise ValueError("status must be FINAL or PROVISIONAL")
        if status == "PROVISIONAL" and path.name == "macros.tex":
            raise ValueError("a provisional run must not write macros.tex")
        if status == "FINAL" and "provisional" in path.name:
            raise ValueError("a final run must not write a provisional macro file")
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["% Generated by wtth.analysis; every value below is computed, none is typed. Do not edit.",
                 f"\\newcommand{{\\wtthStatus}}{{{status}}}"]
        for k in sorted(self._e):
            e = self._e[k]
            src = re.sub(r"\s+", " ", e.source)
            lines.append(f"% {src}" + (f" | {re.sub(chr(10), ' ', e.note)}" if e.note else ""))
            lines.append(f"\\newcommand{{{macro_name(k)}}}{{{e.display}}}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def contrast_verdict(c: Contrast) -> str:
    if c.status == "unavailable":
        return "unavailable"
    if c.rejected is True:
        return "rejected in the opposite direction" if c.direction == "opposite" else "rejected"
    if c.rejected is False:
        return "not rejected"
    return "descriptive" if c.status == "descriptive" else "exploratory"
