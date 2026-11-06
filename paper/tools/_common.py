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
