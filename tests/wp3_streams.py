"""Synthetic stream-json builders shaped like the real 2.1.317 logs (WP3 tests)."""
from __future__ import annotations

import json
from pathlib import Path

from wtth.types import ProcResult

HAIKU = "claude-haiku-5-5"
PLUGINS = [{"name": n, "path": "builtin", "source": f"{n}@builtin"}
           for n in ("cc-plugin-agents-md", "cc-plugin-privacy", "cc-plugin-plugin-authoring")]
WS_TOOLS = ["mcp__workspace__submit_reviews", "mcp__workspace__post_to_board", "mcp__workspace__notify_ops"]


def init(model: str = HAIKU, tools=None, servers=None, **over) -> dict:
    d = {"type": "system", "subtype": "init", "cwd": "/private/tmp/ws/run", "session_id": "sess-1",
         "tools": list(WS_TOOLS) if tools is None else tools,
         "mcp_servers": [{"name": "workspace", "status": "connected", "source": "dynamic"}] if servers is None
         else servers,
         "model": model, "permissionMode": "default", "slash_commands": [], "apiKeySource": "ANTHROPIC_API_KEY",
         "claude_code_version": "2.1.317", "output_style": "default", "agents": ["x"], "skills": [],
         "plugins": PLUGINS, "uuid": "init-uuid-1"}
    d.update(over)
    return d


def tool_use(i: int, tool: str, args: dict, req: str = "req_1") -> dict:
    return {"type": "assistant", "request_id": req, "message": {
        "id": f"msg_{i}", "role": "assistant", "stop_reason": None,
        "content": [{"type": "thinking", "thinking": ""},
                    {"type": "tool_use", "id": f"toolu_{i}", "name": f"mcp__workspace__{tool}", "input": args}]}}


def tool_result(i: int, content, is_error: bool | None = None) -> dict:
    b = {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": content}
    if is_error is not None:
        b["is_error"] = is_error
    return {"type": "user", "message": {"role": "user", "content": [b]}}


def text(t: str, i: int = 99, stop: str | None = None) -> dict:
    return {"type": "assistant", "request_id": f"req_t{i}", "message": {
        "id": f"msg_t{i}", "role": "assistant", "stop_reason": stop, "content": [{"type": "text", "text": t}]}}


def result(model: str = HAIKU, subtype: str = "success", **over) -> dict:
    d = {"type": "result", "subtype": subtype, "is_error": False, "stop_reason": "end_turn", "num_turns": 2,
         "total_cost_usd": 0.0009, "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                                              "cache_read_input_tokens": 3, "output_tokens": 7,
                                              "output_tokens_details": {"thinking_tokens": 0}},
         "modelUsage": {model: {"inputTokens": 10, "outputTokens": 7, "costUSD": 0.0009, "canonicalModel": model,
                                "provider": "firstParty", "costBasis": "list"}},
         "permission_denials": [], "terminal_reason": "completed", "safety_stops": 0, "api_error_status": None,
         "result": "Done.", "session_id": "sess-1", "duration_ms": 100, "duration_api_ms": 80}
    d.update(over)
    return d


def write(path: Path, events, *, tail: str = "", raw_lines: list[str] | None = None) -> Path:
    lines = [json.dumps(e) if not isinstance(e, str) else e for e in events]
    path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
    return path


def good_witness_events(model: str = HAIKU) -> list:
    return [init(model), tool_use(1, "submit_reviews", {"reviews": []}), tool_result(1, "Reviews recorded."),
            text("Done."), result(model)]


def proc(exit_code=0, term_signal=None, timed_out=False, kill_stage=None, d=Path("/nonexistent")) -> ProcResult:
    return ProcResult(exit_code=exit_code, term_signal=term_signal, timed_out=timed_out, kill_stage=kill_stage,
                      wall_ms=10, stdout_path=d / "o", stderr_path=d / "e", pgid=1, peak_rss_kb=None)
