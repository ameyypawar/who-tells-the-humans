"""argv / env / mcp-config construction (research/41 sections 2.3.5, 2.5). Owner: WP3.

Nothing here reads or writes os.environ. The API key is only ever placed in the env dict returned by build_env.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wtth.config import env_allowlist
from wtth.textnorm import sha256_text
from wtth.types import Identity, SlotDirs, Toolset, TurnRequest, TurnType

# WP4-ADD: the server name comes from settings.mcp_server_name (CLI 2.1.317 reserves "workspace")
_DEBRIEF = (TurnType.D1, TurnType.D2)
_NO_TOOLS = (TurnType.D1, TurnType.D2, TurnType.JUDGE)  # WP6: a judge turn gets the debrief argv form


def _budget_str(v: float) -> str:
    r = round(v, 2)
    return f"{r:.2f}" if abs(r - v) < 1e-12 else repr(v)


def build_argv(settings: Any, req: TurnRequest, mcp_config_json: str | None) -> list[str]:
    """The measured rehearsal order, research/41 section 2.5. Every value is its own element; "" is an element."""
    debrief = req.turn_type in _NO_TOOLS
    argv: list[str] = [
        str(settings.paths.binary),
        "-p", req.prompt.text,
        "--bare",
        "--model", req.model,
        "--effort", settings.models.effort,
        "--max-turns", str(req.max_turns),
        "--max-budget-usd", _budget_str(req.max_budget_usd),
        "--system-prompt-file", str(req.system_path),
        "--tools", "",
        "--disable-slash-commands",
        "--setting-sources", "",
        "--settings", str(req.settings_path),
        "--strict-mcp-config",
    ]
    if not debrief:
        if mcp_config_json is None or req.toolset is None:
            raise ValueError("witness/probe turn needs mcp_config_json and a toolset")
        prefix = f"mcp__{settings.mcp_server_name}__"
        names = [prefix + t.name for t in req.toolset.tools]
        argv += ["--mcp-config", mcp_config_json]
        if getattr(settings, "allowed_tools_separate", False):
            # Fallback form if CC rejects the comma-joined single element (research/41 4.3 item 3).
            argv += ["--allowedTools", *names]
        else:
            argv += ["--allowedTools", ",".join(names)]
    argv += [
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--output-format", "stream-json", "--verbose",
    ]
    return argv


def _allowlist_path(settings: Any) -> Path:
    return Path(settings.paths.repo_root) / "configs" / "env_allowlist.json"


def build_env(settings: Any, slot: SlotDirs, api_key: str, bodies_dir: Path | None) -> dict[str, str]:
    """Child env built from scratch (os.environ is never consulted). bodies_dir not None -> audit set added."""
    base, audit = env_allowlist(_allowlist_path(settings))
    env: dict[str, str] = dict(base)
    env["HOME"] = str(slot.home)
    env["TMPDIR"] = str(slot.tmp)
    env["CLAUDE_CONFIG_DIR"] = str(slot.config)
    env["ANTHROPIC_API_KEY"] = api_key
    if bodies_dir is not None:
        for k, v in audit.items():
            env[k] = v.replace("{bodies_dir}", str(bodies_dir))
    return env


def build_mcp_config(settings: Any, ident: Identity, toolset: Toolset) -> str:
    """Inline --mcp-config JSON (research/41 section 2.3.5). Env names avoid *_KEY/*_TOKEN/*_SECRET."""
    if ident.turn_id is None:
        raise ValueError("identity.turn_id is required for the MCP child")
    runs_root = str(settings.paths.runs_dir)
    env = {
        "WTTH_DB": str(settings.paths.db_path),
        "WTTH_RUN_ID": ident.run_id,
        "WTTH_EPISODE_ID": str(ident.episode_id),
        "WTTH_ATTEMPT": str(ident.attempt),
        "WTTH_ROUND": str(ident.round),
        "WTTH_SLOT": str(ident.slot),
        "WTTH_SENDER": ident.sender,
        "WTTH_CLOCK": ident.clock,
        "WTTH_VISIBLE_FROM": str(ident.visible_from_round),
        "WTTH_WORLD": str(ident.world.value if hasattr(ident.world, "value") else ident.world),
        "WTTH_TURN_ID": ident.turn_id,
        "WTTH_TOOLSET": ident.toolset,
        "WTTH_TOOLS_PATH": str(settings.paths.tools_yaml),
        "WTTH_TOOLS_SHA256": toolset.sha256,
        "WTTH_TICKET_IDS": ",".join(ident.ticket_ids),
        "WTTH_FAULT_DIR": str(settings.paths.faults_dir),
        "WTTH_SCHEMA_VERSION": "1",
    }
    if getattr(ident, "receipts", False):   # D-028: the receipts store (a credentials-file PATH, never its content)
        rc = getattr(settings, "receipts", None)
        if rc is None:
            raise ValueError("a receipts cell needs settings.receipts")
        env.update({"WTTH_OO_CONFIG": str(rc.credentials_file), "WTTH_OO_URL": rc.url, "WTTH_OO_STREAM": rc.stream})
    cfg = {"mcpServers": {settings.mcp_server_name: {
        "type": "stdio",
        "command": str(settings.paths.python),
        "args": ["-I", "-m", "wtth.mcp_server", "--runs-root", runs_root],
        "env": env,
        "alwaysLoad": True,
    }}}
    return json.dumps(cfg, separators=(",", ":"), sort_keys=True)


def redact_argv(argv: list[str]) -> list[str]:
    """Replace the prompt element (the one after -p) with <prompt sha256=...>."""
    out = list(argv)
    for i, a in enumerate(out[:-1]):
        if a == "-p":
            out[i + 1] = f"<prompt sha256={sha256_text(out[i + 1])}>"
            break
    return out
