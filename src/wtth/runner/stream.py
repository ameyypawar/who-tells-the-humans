"""stream-json parser (research/41 section 2.6). Owner: WP3.

Never raises on content; raises only on I/O errors. Usage is read only from the `result` event.
"""
from __future__ import annotations

import json
from pathlib import Path

from wtth.types import StreamRecord, ToolUse

# system subtypes seen in real 2.1.317 logs that carry nothing we need
_IGNORED_SYSTEM = frozenset({"thinking_tokens", "ui_invalidate"})


def _result_text(content: object) -> str:
    """tool_result content -> text: the string, or the concatenated `text` of a list of blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type", "text") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return str(content)


def _unknown(rec: StreamRecord, key: str) -> None:
    rec.unknown_events[key] = rec.unknown_events.get(key, 0) + 1


def _first_seen(lst: list[str], v: object) -> None:
    if isinstance(v, str) and v and v not in lst:
        lst.append(v)


def _handle_assistant(rec: StreamRecord, d: dict, by_id: dict[str, ToolUse]) -> None:
    _first_seen(rec.request_ids, d.get("request_id"))
    msg = d.get("message")
    if not isinstance(msg, dict):
        _unknown(rec, "assistant/no_message")
        return
    _first_seen(rec.message_ids, msg.get("id"))
    sr = msg.get("stop_reason")
    if isinstance(sr, str) and sr:
        rec.assistant_stop_reasons.append(sr)
    content = msg.get("content")
    if isinstance(content, str):
        rec.assistant_texts.append(content)
        return
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            txt = b.get("text")
            if isinstance(txt, str):
                rec.assistant_texts.append(txt)
        elif t == "tool_use":
            inp = b.get("input")
            tu = ToolUse(seq=len(rec.tool_uses) + 1, tool_use_id=b.get("id") if isinstance(b.get("id"), str) else None,
                         name=str(b.get("name", "")), input=inp if isinstance(inp, dict) else {})
            rec.tool_uses.append(tu)
            if tu.tool_use_id is not None:
                by_id[tu.tool_use_id] = tu
        # `thinking` (text is empty on 5.5) and other block types: nothing to keep


def _handle_user(rec: StreamRecord, d: dict, by_id: dict[str, ToolUse]) -> None:
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        tu = by_id.get(b.get("tool_use_id"))
        if tu is None:
            _unknown(rec, "user/orphan_tool_result")
            continue
        tu.has_result = True
        tu.result_text = _result_text(b.get("content"))
        tu.result_is_error = bool(b.get("is_error", False))


def parse_stream(path: Path) -> StreamRecord:
    rec = StreamRecord()
    raw = Path(path).read_bytes()
    lines = raw.split(b"\n")
    last_idx = -1
    for i, ln in enumerate(lines):
        if ln.strip():
            last_idx = i
    by_id: dict[str, ToolUse] = {}
    for i, bline in enumerate(lines):
        if not bline.strip():
            continue
        rec.n_lines += 1
        is_last = i == last_idx
        try:
            text = bline.decode("utf-8")
        except UnicodeDecodeError:
            if is_last:
                rec.truncated_tail = True
            else:
                rec.n_bad_json += 1
            continue
        text = text.rstrip("\r")
        if not text.startswith("{"):
            rec.n_nonjson += 1
            continue
        try:
            d = json.loads(text)
        except ValueError:
            if is_last:
                rec.truncated_tail = True
            else:
                rec.n_bad_json += 1
            continue
        if not isinstance(d, dict):
            rec.n_bad_json += 1
            continue
        typ, sub = d.get("type"), d.get("subtype")
        if typ == "system":
            if sub == "init":
                rec.n_init += 1
                if rec.init is None:
                    rec.init = d
                    if isinstance(d.get("session_id"), str):
                        rec.session_id = d["session_id"]
            elif sub == "api_retry":
                rec.api_retries.append(d)
            elif sub not in _IGNORED_SYSTEM:
                _unknown(rec, f"system/{sub}")
        elif typ == "rate_limit_event":
            rec.rate_limit_events += 1
        elif typ == "assistant":
            _handle_assistant(rec, d, by_id)
        elif typ == "user":
            _handle_user(rec, d, by_id)
        elif typ == "result":
            rec.n_result += 1
            if rec.result is None:
                rec.result = d
                if rec.session_id is None and isinstance(d.get("session_id"), str):
                    rec.session_id = d["session_id"]
        else:
            _unknown(rec, f"{typ}/{sub}" if sub is not None else str(typ))
    return rec
