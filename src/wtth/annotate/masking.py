"""Masking of private text and the residual scan (plan 47 section 2.10, G-5).

Every text shown to Claude, or written outside runs/, goes through Masker.mask. Private strings come only
from prereg.load_private(); they are never printed, logged or put in exception messages.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wtth.redact import _Redactor, _variants

MIN_FRAGMENT = 30

PARAPHRASE_PLACEHOLDER = '[withheld line: a paraphrase of "Already sent ops a note about the Hendricks batches."]'
FORGED_PLACEHOLDER = "[withheld line: a receipt-style line in the escalation-log layout, typed by the poster]"
FRAGMENT_PLACEHOLDER = "[withheld text]"
_PLACEHOLDERS = (PARAPHRASE_PLACEHOLDER, FORGED_PLACEHOLDER, FRAGMENT_PLACEHOLDER)
_SPLIT = re.compile("(" + "|".join(re.escape(p) for p in _PLACEHOLDERS) + ")")


class PrivateLeak(RuntimeError):
    """A private string survived masking in a written file. The message names ids only."""


def _fold(s: str) -> str:
    """Case-fold that never changes the length (so spans map back to the original text)."""
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in s)


def placeholder_for(pid: str) -> str:
    return FORGED_PLACEHOLDER if pid.endswith("FORGED_LINE") else PARAPHRASE_PLACEHOLDER


@functools.lru_cache(maxsize=8)
def _folded_public(lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_fold(x) for x in lines if x)


def _is_public(span_folded: str, lines: tuple[str, ...]) -> bool:
    return any(span_folded in p for p in _folded_public(lines))


def _fragment_scan(segment: str, private_folded: list[str], public_lines: tuple[str, ...]) -> tuple[str, int]:
    """Replace every span >= MIN_FRAGMENT characters shared with a private string, unless that (maximal) span is
    contained in a public line. Repeats until no such span is left."""
    from difflib import SequenceMatcher

    if len(segment) < MIN_FRAGMENT:
        return segment, 0
    work = list(_fold(segment))
    redact = [False] * len(segment)
    n = 0
    for pf in private_folded:
        if len(pf) < MIN_FRAGMENT:
            continue
        while True:
            sm = SequenceMatcher(None, "".join(work), pf, autojunk=False)
            m = sm.find_longest_match(0, len(work), 0, len(pf))
            if m.size < MIN_FRAGMENT:
                break
            lo, hi = m.a, m.a + m.size
            while lo < hi and work[lo].isspace():
                lo += 1
            while hi > lo and work[hi - 1].isspace():
                hi -= 1
            span = "".join(work[lo:hi])
            if hi - lo >= MIN_FRAGMENT and not _is_public(span, public_lines):
                for i in range(lo, hi):
                    redact[i] = True
                n += 1
            for i in range(m.a, m.a + m.size):
                work[i] = "\x00"
    if not n:
        return segment, 0
    out: list[str] = []
    i = 0
    while i < len(segment):
        if redact[i]:
            j = i
            while j < len(segment) and redact[j]:
                j += 1
            out.append(FRAGMENT_PLACEHOLDER)
            i = j
        else:
            out.append(segment[i])
            i += 1
    return "".join(out), n


@dataclass(frozen=True, slots=True)
class Masker:
    private: Mapping[str, str]
    public_lines: tuple[str, ...]

    def mask(self, text: str) -> tuple[str, int]:
        """(masked text, number of replacements). Exact strings first (longest first, raw and JSON-escaped
        forms), then the fragment scan on the text between placeholders."""
        n = 0
        pairs = [(pid, v) for pid, val in self.private.items() if val for v in _variants(val)]
        pairs.sort(key=lambda p: -len(p[1]))
        for pid, needle in pairs:
            c = text.count(needle)
            if c:
                n += c
                text = text.replace(needle, placeholder_for(pid))
        pf = [_fold(v) for v in sorted((v for v in self.private.values() if v), key=len, reverse=True)]
        parts = _SPLIT.split(text)
        for i in range(0, len(parts), 2):  # even indices are text; odd are placeholders
            parts[i], k = _fragment_scan(parts[i], pf, self.public_lines)
            n += k
        return "".join(parts), n


def make_masker(settings: Any) -> Masker:
    from wtth.prereg import load_private, load_scenario_texts

    private = load_private(settings)
    lines: list[str] = []
    for scenario in ("main", "backup"):
        t = load_scenario_texts(scenario, settings)
        lines.extend(t.lines.values())
        lines.append(t.claims[0])
        lines.extend(t.c_note)
    return Masker(private=dict(private), public_lines=tuple(dict.fromkeys(x for x in lines if x)))


def residual(data: bytes, private: Mapping[str, str]) -> dict[str, int]:
    """Occurrences left per private id (raw and JSON-escaped forms). {} means clean."""
    return _Redactor(dict(private)).residual(data)


def assert_clean_file(path: Path, private: Mapping[str, str]) -> None:
    """Residual scan of a written file. On a hit: delete the file and raise PrivateLeak naming ids only."""
    path = Path(path)
    left = residual(path.read_bytes(), private)
    if left:
        path.unlink(missing_ok=True)
        raise PrivateLeak("private strings survived masking for ids: " + ",".join(sorted(left)))
