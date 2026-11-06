"""Shared helpers for the paper generators. Run with the harness venv (needs PyYAML): python -I tools/<tool>.py"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

PAPER = Path(__file__).resolve().parent.parent
REPO = PAPER.parent
FROZEN_TAG = "frozen-v1"
FALLBACK_REF = "main"

_LATEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
          "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex_escape(s: str) -> str:
    return "".join(_LATEX.get(c, c) for c in s)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout


def resolve_ref() -> tuple[str, bool]:
    """(ref, provisional). frozen-v1 if the tag exists, else main and PROVISIONAL."""
    try:
        git("rev-parse", "--verify", "--quiet", f"refs/tags/{FROZEN_TAG}")
        return FROZEN_TAG, False
    except subprocess.CalledProcessError:
        return FALLBACK_REF, True


def show(ref: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:{path}"], check=True, capture_output=True).stdout


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def hashes(ref: str) -> dict:
    return json.loads(show(ref, "configs/prereg_hashes.json"))


def write_generated(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def scan_private(paths: list[Path]) -> dict:
    """Residual scan of generated files with wtth.redact (private strings loaded by the harness; ids only are reported).
    Skipped silently if the harness is not importable."""
    try:
        import sys
        sys.path.insert(0, str(REPO / "src"))
        from wtth.config import load_settings
        from wtth.prereg import load_private
        from wtth.redact import _Redactor
    except Exception:  # noqa: BLE001
        return {"_skipped": "wtth not importable"}
    red = _Redactor(dict(load_private(load_settings())))
    out = {}
    for p in paths:
        hits = red.residual(p.read_bytes())
        if hits:
            out[str(p.relative_to(PAPER))] = sorted(hits)
    return out


def stored_prompt_matches(text: str, cell: str, level: str, rnd: int = 2, phase: str = "core") -> list | None:
    """[(seed, slot)] of the final witness turns of `phase` / `cell` / `level`, round `rnd`, whose stored user prompt
    (exposures.prompt_text, the text as sent) equals `text` byte for byte; None when runs/wtth.sqlite is absent.
    Read-only; nothing is printed here, so a prompt that carries a withheld string never leaves the database."""
    import sqlite3

    path = REPO / "runs" / "wtth.sqlite"
    if not path.is_file():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT seed, slot, prompt_text FROM v_exposures_final WHERE turn_type='witness' AND scenario='main' "
            "AND phase=? AND cell=? AND level=? AND round=? ORDER BY seed, slot", (phase, cell, level, rnd)).fetchall()
    finally:
        con.close()
    return [(seed, slot) for seed, slot, t in rows if t == text]
