"""Text normalization shared by the MCP server and the renderer (research/41 section 2.10).

Pure functions, stdlib only. The server imports this module, so it must stay light.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

# The characters str.splitlines() splits on (\r\n counts as one break, handled by the run regex).
BREAK_CHARS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85  "
_BREAK_RUN = re.compile("[ \t]*(?:[" + re.escape(BREAK_CHARS) + "][ \t]*)+")
_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")

NO_SUBJECT = "(no subject)"


def collapse_breaks(s: str) -> tuple[str, bool]:
    """Replace each maximal run of break characters (with adjacent spaces/tabs) by one space.

    Returns (text, changed); changed is True iff at least one break character was present.
    """
    text, n = _BREAK_RUN.subn(" ", s)
    return text, n > 0


def cut(s: str, n: int) -> tuple[str, bool]:
    """s[:n] in code points; returns (text, was_cut)."""
    if len(s) > n:
        return s[:n], True
    return s, False


def one_line(s: str) -> str:
    return collapse_breaks(s)[0].strip()


def normalize_post(raw: Any, cap: int = 600) -> tuple[str | None, dict]:
    """Normalize a live board post. Returns (text or None when rejected, flags).

    flags holds only the keys that are true: 'bad_type' (not a str), 'empty', 'collapsed', 'cut'.
    """
    if not isinstance(raw, str):
        return None, {"bad_type": True}
    collapsed, changed = collapse_breaks(raw)
    text, was_cut = cut(collapsed.strip(), cap)
    flags: dict = {}
    if changed:
        flags["collapsed"] = True
    if was_cut:
        flags["cut"] = True
    if not text:
        flags["empty"] = True
        return None, flags
    return text, flags


def normalize_note(subject_raw: Any, message_raw: Any, subject_cap: int = 60,
                   message_cap: int = 2000) -> tuple[bool, str, str, dict]:
    """Normalize a notify_ops call. Returns (accepted, subject, message, flags).

    Absent (None) fields count as empty strings. A field that is neither str nor None -> not accepted,
    flag 'bad_type'. Accepted iff subject_raw.strip() or message_raw.strip().
    flags holds only true keys: 'bad_type', 'empty', 'empty_subject', 'subject_cut', 'message_cut'.
    """
    flags: dict = {}
    if (subject_raw is not None and not isinstance(subject_raw, str)) or (
            message_raw is not None and not isinstance(message_raw, str)):
        flags["bad_type"] = True
        return False, "", "", flags
    subj_raw = subject_raw or ""
    msg_raw = message_raw or ""
    if not (subj_raw.strip() or msg_raw.strip()):
        flags["empty"] = True
        return False, "", "", flags
    subject, subject_cut = cut(collapse_breaks(subj_raw)[0].strip(), subject_cap)
    if not subject:
        subject = NO_SUBJECT
        flags["empty_subject"] = True
    if subject_cut:
        flags["subject_cut"] = True
    message, message_cut = cut(msg_raw, message_cap)
    if message_cut:
        flags["message_cut"] = True
    return True, subject, message, flags


def fill(template: str, values: dict[str, str]) -> str:
    """Single-pass template fill: `{name}` -> values[name]. Substituted text is never re-scanned.

    An unknown placeholder raises KeyError(name).
    """
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canon_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def kw_hits(subject: str, message: str, keywords: Iterable[str]) -> dict[str, list[str]]:
    """P1 keyword scan (research/42 section 2.2, B8): casefold literal substring, no other normalization.

    Subject and message are scanned separately (pass the stored, already cut fields). Returns
    {"subject": [...], "message": [...]} with keywords in the order given.
    """
    kws = list(keywords)
    sub, msg = subject.casefold(), message.casefold()
    return {"subject": [k for k in kws if k.casefold() in sub],
            "message": [k for k in kws if k.casefold() in msg]}
