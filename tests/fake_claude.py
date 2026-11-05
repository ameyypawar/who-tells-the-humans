#!/usr/bin/env python3
"""Fake `claude` CLI (research/41 section 3.2.1). Owner: WP5.

Drop-in for the pinned binary: same argv, stream-json events on stdout, modes for every failure the runner must
classify. Invoked through a shim written by tests.util.write_shim:

    exec <venv python> -I tests/fake_claude.py --behavior <behavior.json> "$@"

Contract violations (argv shape, stdin, cwd, env, slot reuse, system file) print
`FAKE_CONTRACT_VIOLATION: <what>` to stderr and exit 64.

MCP path (witness turns): the configured `workspace` server is spawned through mcp.client.stdio with exactly the
configured env map, so the real handlers and receipts run and the tool_result text is the server's actual reply.

Behaviour file (JSON), see the module-level DEFAULT_BEHAVIOR and resolve_behavior():
    {"default": {...}, "rules": [{"match": {...}, ...overrides}], "contract": {...}}
Match keys: world, round, slot, attempt, episode_id, sender, toolset (from the mcp-config env WTTH_*), turn_type
(witness|d1|d2), model, prompt_contains. The first matching rule is merged (dict-recursive) over `default`.

Everything here is stdlib plus `mcp` (client side). The key is never written or printed.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
import zlib
from pathlib import Path

FAKE_VERSION = "2.1.317"
MCP_PREFIX = "mcp__workspace__"
SERVER_NAME = "workspace"
SDK_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
BILLING_HEADER = "x-anthropic-billing-header: cc_version=2.1.317.fake; cc_entrypoint=sdk-cli; cch=00000;"
STDERR_WARNING = "⚠ Permission mode forced to default — CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set"
STDERR_NOISE = "fake_claude: unexpected stderr noise line"
EXIT_CONTRACT = 64
REPO = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = REPO / "configs" / "env_allowlist.json"
DEFAULT_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
# Variables a /bin/sh shim or macOS adds on its own; never part of the orchestrator-built env.
SHELL_NOISE_ENV = {"PWD", "OLDPWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}
SECRET_ENV_RE = re.compile(r"^ANTHROPIC|_KEY$|_TOKEN$|_SECRET$|PASSWORD")
PATTERN_DECISIONS = ("approve", "approve", "decline")  # scripted_lines section 6

MODES = ("normal", "hang", "hang_hard", "exit143", "crash_after_calls", "truncate", "drop_server",
         "server_dies_mid_turn", "refusal", "max_turns", "max_budget", "api_error", "wrong_model", "bad_version",
         "permission_denial", "stderr_noise", "slow")

DEFAULT_BEHAVIOR = {
    "mode": "normal",
    "witness": {"calls": [{"tool": "submit_reviews", "args": "auto"}], "final_text": "Done."},
    "d1": {"text": "It read as a normal live shift."},
    "d2": {"text": "1. Yes.\n2. No.\n3. Nothing unusual."},
    "cost": {"claude-haiku-5-5": 0.0009, "claude-sonnet-5-5": 0.006},
    "sleep_s": 0,
}


# ---------------------------------------------------------------------------------------------------------
# helpers usable by tests (import as `from tests import fake_claude`)
# ---------------------------------------------------------------------------------------------------------

def contract_for(settings, api_key: str, *, slots_root: Path | None = None, cwd: str | None = None) -> dict:
    """The `contract` section for a behaviour file, built from a wtth Settings object and the test's dummy key."""
    return {
        "cwd": str(cwd or settings.paths.cwd),
        "slots_root": str(slots_root or settings.paths.slots_root),
        "api_key_sha256": hashlib.sha256(api_key.encode()).hexdigest(),  # never the key itself
        "models": [settings.models.haiku, settings.models.sonnet],
    }


def write_behavior(path: Path, behavior: dict | None = None, *, contract: dict | None = None) -> Path:
    """Write a behaviour JSON file (merged over nothing: the fake merges it over DEFAULT_BEHAVIOR itself)."""
    doc = dict(behavior or {})
    if contract is not None:
        doc["contract"] = contract
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return path


def probe_behavior(deny_mode: str, **extra) -> dict:
    """Behaviour for a deny-probe turn whose --settings file denies mcp__workspace__submit_reviews.

    deny_mode "strip": the tool is absent from init.tools, no permission_denials entry.
    deny_mode "deny": the tool is listed, the model attempts it, permission_denials holds the entry.
    """
    if deny_mode not in ("strip", "deny"):
        raise ValueError(deny_mode)
    return {"default": {"deny_mode": deny_mode, **extra}}


def denied_tools(settings_path: str, beh: dict) -> set[str]:
    """Bare tool names denied by the --settings file (permissions.deny) plus behaviour `deny_tools`."""
    out = set(beh.get("deny_tools", []))
    try:
        doc = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        for ent in doc.get("permissions", {}).get("deny", []):
            if isinstance(ent, str) and ent.startswith(MCP_PREFIX):
                out.add(ent[len(MCP_PREFIX):])
    except (OSError, ValueError, AttributeError):
        pass
    return out


# ---------------------------------------------------------------------------------------------------------
# behaviour resolution
# ---------------------------------------------------------------------------------------------------------

def _merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = {k: copy.deepcopy(v) for k, v in base.items()}
        for k, v in over.items():
            out[k] = _merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def _rule_matches(match: dict, facts: dict) -> bool:
    for k, want in match.items():
        if k == "prompt_contains":
            if str(want) not in facts.get("prompt", ""):
                return False
            continue
        have = facts.get(k)
        if have is None:
            return False
        if isinstance(want, int) and not isinstance(want, bool):
            try:
                if int(have) != want:
                    return False
            except (TypeError, ValueError):
                return False
        elif str(have) != str(want):
            return False
    return True


def resolve_behavior(doc: dict, facts: dict) -> dict:
    """default (merged over the built-in default), then the first matching rule merged over that."""
    beh = _merge(DEFAULT_BEHAVIOR, doc.get("default", {}))
    for rule in doc.get("rules", []):
        if _rule_matches(rule.get("match", {}), facts):
            over = {k: v for k, v in rule.items() if k != "match"}
            beh = _merge(beh, over)
            break
    return beh


# ---------------------------------------------------------------------------------------------------------
# contract checks
# ---------------------------------------------------------------------------------------------------------

class Argv:
    """Parsed claude argv."""

    def __init__(self) -> None:
        self.prompt = ""
        self.model = ""
        self.effort = ""
        self.max_turns = 0
        self.max_budget = 0.0
        self.system_file = ""
        self.settings = ""
        self.mcp_config: str | None = None
        self.allowed_tools: str | None = None
        self.permission_mode = ""
        self.is_witness = False


def _isint(s: str) -> bool:
    return bool(re.fullmatch(r"\d+", s))


def _isfloat(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def check_argv(args: list[str], models: tuple[str, ...]) -> tuple[Argv, list[str]]:
    """Validate against the section 2.5 table (binary element excluded). Returns (parsed, violations)."""
    v: list[str] = []
    a = Argv()
    a.is_witness = "--mcp-config" in args
    seq: list[tuple[str, str | None]] = [  # (literal or var-name, validator kind) in order
        ("-p", None), ("$prompt", "any"), ("--bare", None), ("--model", None), ("$model", "model"),
        ("--effort", None), ("$effort", "low"), ("--max-turns", None), ("$max_turns", "int"),
        ("--max-budget-usd", None), ("$max_budget", "float"), ("--system-prompt-file", None),
        ("$system_file", "any"), ("--tools", None), ("", None), ("--disable-slash-commands", None),
        ("--setting-sources", None), ("", None), ("--settings", None), ("$settings", "any"),
        ("--strict-mcp-config", None),
    ]
    if a.is_witness:
        seq += [("--mcp-config", None), ("$mcp_config", "any"), ("--allowedTools", None), ("$allowed", "any")]
    seq += [("--permission-mode", None), ("$perm", "perm"), ("--no-session-persistence", None),
            ("--output-format", None), ("stream-json", None), ("--verbose", None)]
    if len(args) != len(seq):
        v.append(f"argv: expected {len(seq)} elements, got {len(args)}")
    for i, (want, kind) in enumerate(seq[: len(args)]):
        got = args[i]
        if not want.startswith("$"):
            if got != want:
                v.append(f"argv[{i}]: expected {want!r}, got {got!r}")
            continue
        name = want[1:]
        if kind == "model":
            a.model = got
            if got not in models:
                v.append(f"argv[{i}]: model {got!r} not in {list(models)}")
        elif kind == "low":
            a.effort = got
            if got != "low":
                v.append(f"argv[{i}]: effort must be 'low', got {got!r}")
        elif kind == "int":
            if not _isint(got):
                v.append(f"argv[{i}]: --max-turns value {got!r} is not an integer")
            else:
                a.max_turns = int(got)
        elif kind == "float":
            if not _isfloat(got):
                v.append(f"argv[{i}]: --max-budget-usd value {got!r} is not a number")
            else:
                a.max_budget = float(got)
        elif kind == "perm":
            a.permission_mode = got
            if got != "dontAsk":
                v.append(f"argv[{i}]: permission mode must be 'dontAsk', got {got!r}")
        else:
            if name == "prompt":
                a.prompt = got
                if not got:
                    v.append("argv: empty prompt")
            elif name == "system_file":
                a.system_file = got
            elif name == "settings":
                a.settings = got
                if not got:
                    v.append("argv: empty --settings value")
            elif name == "mcp_config":
                a.mcp_config = got
            elif name == "allowed":
                a.allowed_tools = got
    return a, v


def check_environment(env: dict[str, str], contract: dict) -> list[str]:
    v: list[str] = []
    path = Path(contract.get("env_allowlist_path") or DEFAULT_ALLOWLIST)
    doc = json.loads(path.read_text(encoding="utf-8"))
    base: dict = doc["base"]
    audit: dict = doc.get("audit", {})
    dynamic = list(doc.get("dynamic", []))
    audit_on = "CLAUDE_CODE_ENABLE_TELEMETRY" in env
    expected = set(base) | set(dynamic) | (set(audit) if audit_on else set())
    have = {k for k in env if k not in SHELL_NOISE_ENV}
    missing = sorted(expected - have)
    extra = sorted(have - expected)
    if missing:
        v.append("env: missing names " + ",".join(missing))
    if extra:
        v.append("env: unexpected names " + ",".join(extra))
    for k, want in base.items():
        if k in env and env[k] != want:
            v.append(f"env: {k} has a non-allowlisted value")
    if audit_on:
        for k, want in audit.items():
            if k not in env:
                continue
            if "{bodies_dir}" in want:
                if not (env[k].startswith("file:") and len(env[k]) > 5):
                    v.append(f"env: {k} must be file:<dir>")
            elif env[k] != want:
                v.append(f"env: {k} has a non-allowlisted value")
    want = contract.get("api_key_sha256")
    if want is not None and hashlib.sha256(env.get("ANTHROPIC_API_KEY", "").encode()).hexdigest() != want:
        v.append("env: ANTHROPIC_API_KEY is not the test dummy key")
    key = contract.get("api_key")  # legacy form (writes the key to disk): still honoured
    if key is not None and env.get("ANTHROPIC_API_KEY") != key:
        v.append("env: ANTHROPIC_API_KEY is not the test dummy key")
    return v


def check_slot(env: dict[str, str], contract: dict) -> tuple[list[str], object | None]:
    """HOME/CLAUDE_CONFIG_DIR/TMPDIR under <slots_root>/wNN and the slot not used by another running fake."""
    v: list[str] = []
    lock_fh = None
    root = contract.get("slots_root")
    homes = {k: env.get(k) for k in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR")}
    if any(not x for x in homes.values()):
        return ["slot: HOME/CLAUDE_CONFIG_DIR/TMPDIR must all be set"], None
    slot_dirs = {str(Path(x).parent) for x in homes.values()}
    if len(slot_dirs) != 1:
        v.append("slot: HOME, CLAUDE_CONFIG_DIR and TMPDIR do not share one slot dir")
        return v, None
    slot = Path(next(iter(slot_dirs)))
    if root:
        try:
            Path(homes["HOME"]).resolve().relative_to(Path(root).resolve())
        except ValueError:
            v.append("slot: HOME is not under the test slots root")
    if not slot.is_dir():
        v.append("slot: slot dir does not exist")
        return v, None
    try:
        lock_fh = open(slot / ".busy", "a+")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        v.append("slot: another running fake already uses this slot")
        lock_fh = None
    return v, lock_fh


def check_system_file(path: str) -> list[str]:
    p = Path(path)
    if not p.is_file():
        return [f"system file: {path} does not exist"]
    if p.stat().st_mode & 0o222:
        return ["system file: must be read-only (mode 0444)"]
    return []


def violation_exit(violations: list[str]) -> None:
    for what in violations:
        sys.stderr.write(f"FAKE_CONTRACT_VIOLATION: {what}\n")
    sys.stderr.flush()
    os._exit(EXIT_CONTRACT)


# ---------------------------------------------------------------------------------------------------------
# event output
# ---------------------------------------------------------------------------------------------------------

def _now_iso() -> str:
    n = _dt.datetime.now(_dt.timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _usage(inp: int, out: int, cache_create: int = 0, cache_read: int = 0) -> dict:
    return {"input_tokens": inp, "cache_creation_input_tokens": cache_create, "cache_read_input_tokens": cache_read,
            "cache_creation": {"ephemeral_5m_input_tokens": cache_create, "ephemeral_1h_input_tokens": 0},
            "output_tokens": out, "service_tier": "standard", "inference_geo": "global", "speed": "standard"}


class Turn:
    """State of one fake CLI run."""

    def __init__(self, args: Argv, beh: dict, env: dict[str, str], cwd: str) -> None:
        self.args = args
        self.beh = beh
        self.env = env
        self.cwd = cwd
        self.session_id = str(uuid.uuid4())
        self.model = args.model
        self.n_calls = 0
        self.n_requests = 0
        self.t0 = time.monotonic()
        self.tools: list[dict] = []          # [{name, description, input_schema}] as listed by the server
        self.status = "connected"
        self.mcp_log: Path | None = None
        self.bodies_dir: Path | None = None
        self.audit = "CLAUDE_CODE_ENABLE_TELEMETRY" in env
        self.denials: list[dict] = []
        self.denied: set[str] = set()
        self.messages: list[dict] = [{"role": "user", "content": args.prompt}]
        self.system_text = ""
        with contextlib.suppress(OSError):
            self.system_text = Path(args.system_file).read_text(encoding="utf-8")
        bd = env.get("OTEL_LOG_RAW_API_BODIES", "")
        if bd.startswith("file:") and len(bd) > 5:
            self.bodies_dir = Path(bd[5:]) / self.session_id

    # ---- mcp log (CC style) ----
    def log_mcp(self, text: str, key: str = "debug") -> None:
        if self.mcp_log is None:
            return
        rec = {key: text, "timestamp": _now_iso(), "sessionId": self.session_id, "cwd": self.cwd}
        with open(self.mcp_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    def open_mcp_log(self) -> None:
        home = self.env.get("HOME")
        if not home:
            return
        stamp = _dt.datetime.now(_dt.timezone.utc)
        name = stamp.strftime("%Y-%m-%dT%H-%M-%S-") + f"{int(stamp.microsecond / 1000):03d}Z.jsonl"
        d = Path(home) / "Library" / "Caches" / "claude-cli-nodejs" / self.cwd.replace("/", "-") / f"mcp-logs-{SERVER_NAME}"
        d.mkdir(parents=True, exist_ok=True)
        self.mcp_log = d / name

    # ---- bodies (audit turns) ----
    def write_body(self, response_text: str) -> None:
        self.n_requests += 1
        if self.audit:
            sys.stdout.write("[otel] fake log record api_request\n")
            sys.stdout.flush()
        if self.bodies_dir is None:
            return
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        body = {
            "model": self.model, "max_tokens": 32000, "stream": True,
            "system": [{"type": "text", "text": BILLING_HEADER}, {"type": "text", "text": SDK_IDENTITY},
                       {"type": "text", "text": self.system_text}],
            "tools": [{"name": MCP_PREFIX + t["name"], "description": t["description"],
                       "input_schema": t["input_schema"]} for t in self.tools],
            "messages": copy.deepcopy(self.messages),
            "metadata": {"user_id": "fake-opaque-id"},
        }
        leak = (self.beh.get("bodies") or {}).get("leak")
        if leak:
            body["messages"][0]["content"] = str(body["messages"][0]["content"]) + "\n" + str(leak)
        k = f"{self.n_requests:03d}"
        (self.bodies_dir / f"{k}.request.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        (self.bodies_dir / f"{k}.response.json").write_text(
            json.dumps({"content": [{"type": "text", "text": response_text}]}, ensure_ascii=False), encoding="utf-8")

    # ---- events ----
    def common(self) -> dict:
        return {"session_id": self.session_id, "uuid": str(uuid.uuid4())}

    def emit_init(self) -> None:
        if self.beh.get("ui_invalidate", "auto") is True or (
                self.beh.get("ui_invalidate", "auto") == "auto" and zlib.crc32(self.args.prompt.encode()) % 5 == 0):
            emit({"type": "system", "subtype": "ui_invalidate", **self.common()})
        version = "2.1.318" if self.beh["mode"] == "bad_version" else FAKE_VERSION
        names = [MCP_PREFIX + t["name"] for t in self.tools]
        servers = [] if not self.args.is_witness else [
            {"name": SERVER_NAME, "status": self.status, "source": "dynamic"}]
        emit({"type": "system", "subtype": "init", "cwd": self.cwd, "session_id": self.session_id, "tools": names,
              "mcp_servers": servers, "model": self.model, "permissionMode": "default", "slash_commands": [],
              "apiKeySource": "ANTHROPIC_API_KEY", "claude_code_version": version, "output_style": "default",
              "agents": ["claude", "Explore", "general-purpose", "Plan", "statusline-setup"], "skills": [],
              "plugins": [{"name": n, "path": "builtin", "source": f"{n}@builtin"} for n in
                          ("cc-plugin-agents-md", "cc-plugin-privacy", "cc-plugin-plugin-authoring")],
              "analytics_disabled": True, "uuid": str(uuid.uuid4()), "original_cwd": self.cwd,
              "additional_directories": [], "fast_mode_state": "off"})

    def assistant(self, content: list[dict], stop_reason=None) -> None:
        emit({"type": "assistant",
              "message": {"model": self.model, "id": "msg_fake_" + uuid.uuid4().hex[:20], "type": "message",
                          "role": "assistant", "content": content, "container": None, "stop_reason": stop_reason,
                          "stop_sequence": None, "usage": _usage(200 + 40 * self.n_requests, 24)},
              "parent_tool_use_id": None, "timestamp": _now_iso(),
              "request_id": "req_fake_" + uuid.uuid4().hex[:20], **self.common()})

    def tool_call_events(self, name: str, tool_input: dict, text: str, is_error: bool) -> None:
        self.n_calls += 1
        tid = f"toolu_fake_{self.n_calls}"
        emit({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50 * self.n_calls,
              "estimated_tokens_delta": 50, **self.common()})
        self.assistant([{"type": "thinking", "thinking": "", "signature": "ZmFrZQ=="},
                        {"type": "tool_use", "id": tid, "name": MCP_PREFIX + name, "input": tool_input,
                         "caller": {"type": "direct"}}])
        content: object = text
        if self.beh.get("result_content_form") == "list":
            content = [{"type": "text", "text": text}]
        block = {"tool_use_id": tid, "type": "tool_result", "content": content}
        if is_error:
            block["is_error"] = True
        emit({"type": "user", "message": {"role": "user", "content": [block]}, "parent_tool_use_id": None,
              "timestamp": _now_iso(), **self.common()})
        self.messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": MCP_PREFIX + name, "input": tool_input}]})
        self.messages.append({"role": "user", "content": [block]})

    def result(self, *, subtype="success", is_error=False, text="", stop_reason="end_turn", num_turns=None,
               api_error_status=None, permission_denials=None, usage_model=None, no_result_field=False) -> None:
        cost = float(self.beh.get("cost", {}).get(self.model, 0.001))
        model_key = usage_model or self.model
        elapsed = int((time.monotonic() - self.t0) * 1000)
        doc = {
            "type": "result", "subtype": subtype, "is_error": is_error, "duration_ms": elapsed,
            "duration_api_ms": max(1, elapsed // 2), "num_turns": num_turns if num_turns is not None else self.n_calls + 1,
            "stop_reason": stop_reason, "session_id": self.session_id, "total_cost_usd": cost,
            "usage": {**_usage(300, 60, 500, 0), "output_tokens_details": {"thinking_tokens": 0}},
            "modelUsage": {model_key: {"inputTokens": 300, "outputTokens": 60, "cacheReadInputTokens": 0,
                                       "cacheCreationInputTokens": 500, "webSearchRequests": 0, "costUSD": cost,
                                       "contextWindow": 1000000, "maxOutputTokens": 128000, "thinkingTokens": 0,
                                       "canonicalModel": model_key, "provider": "firstParty", "costBasis": "list"}},
            "permission_denials": (permission_denials or []) + self.denials, "terminal_reason": "completed",
            "fast_mode_state": "off", "safety_stops": 0, "api_error_status": api_error_status,
            "uuid": str(uuid.uuid4()),
        }
        if not no_result_field:
            doc["result"] = text
        if subtype != "success" or is_error:
            doc["terminal_reason"] = "max_turns" if subtype == "error_max_turns" else "error"
        emit(doc)


# ---------------------------------------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------------------------------------

def _child_pids(pid: int) -> list[int]:
    for exe in ("/usr/bin/pgrep", "/bin/pgrep"):
        if os.path.exists(exe):
            r = subprocess.run([exe, "-P", str(pid)], capture_output=True, text=True)
            return [int(x) for x in r.stdout.split() if x.isdigit()]
    return []


def _text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def parse_ticket_ids(prompt: str) -> list[str]:
    """Ids from the YOUR TICKETS / YOUR LOG ENTRIES block: the first token of each line up to the blank line."""
    lines = prompt.split("\n")
    ids: list[str] = []
    for i, ln in enumerate(lines):
        if ln.strip() in ("YOUR TICKETS", "YOUR LOG ENTRIES"):
            for ln2 in lines[i + 1:]:
                if not ln2.strip():
                    break
                m = re.match(r"^([A-Z]{1,3}-\d{4})\b", ln2)
                if m:
                    ids.append(m.group(1))
            break
    return ids


def auto_args(tool: str, prompt: str, tools: list[dict]) -> dict:
    if tool == "submit_reviews":
        id_field = "ticket_id"
        for t in tools:
            if t["name"] == "submit_reviews":
                props = t["input_schema"].get("properties", {}).get("reviews", {}).get("items", {}).get("properties", {})
                if props:
                    id_field = next(iter(props))
        ids = parse_ticket_ids(prompt)
        return {"reviews": [{id_field: tid, "decision": PATTERN_DECISIONS[i % 3], "comment": "ok"}
                            for i, tid in enumerate(ids)]}
    if tool == "post_to_board":
        return {"message": "ok"}
    if tool == "notify_ops":
        return {"subject": "note", "message": "fake note"}
    return {}


async def _run_witness(turn: Turn, mcp_cfg: dict, beh: dict) -> int:
    """Witness turn: connect to the real server, run the scripted calls, emit events. Returns the exit code."""
    from mcp import ClientSession, StdioServerParameters
    import mcp.client.stdio as mcp_stdio

    mode = beh["mode"]
    server = mcp_cfg["mcpServers"][SERVER_NAME]
    mcp_stdio.get_default_environment = lambda: {}  # exactly the configured env map, nothing inherited
    params = StdioServerParameters(command=server["command"], args=list(server.get("args", [])),
                                   env=dict(server.get("env", {})))
    turn.open_mcp_log()
    turn.log_mcp("Starting connection with timeout of 30000ms")
    turn.log_mcp("Server stderr: Failed to validate request: 31 validation errors for ClientRequest\n"
                 "PingRequest.method\n  Input should be 'ping' [type=literal_error, input_value='server/discover', "
                 "input_type=str]", key="error")
    errpath = (Path(turn.env.get("TMPDIR", "/tmp")) / f"fake-server-stderr-{turn.session_id}.txt")
    errfh = open(errpath, "w+", encoding="utf-8")
    t_conn = time.monotonic()
    server_pid: int | None = None
    alive = True
    async with contextlib.AsyncExitStack() as stack:
        session = None
        try:
            r, w = await stack.enter_async_context(mcp_stdio.stdio_client(params, errlog=errfh))
            session = await stack.enter_async_context(ClientSession(r, w))
            await asyncio.wait_for(session.initialize(), 30)
            kids = _child_pids(os.getpid())
            server_pid = kids[0] if kids else None
            if mode == "drop_server" and server_pid:
                os.kill(server_pid, signal.SIGKILL)
                await asyncio.sleep(0.3)
            listed = await asyncio.wait_for(session.list_tools(), 30)
            turn.tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                          for t in listed.tools]
            connect_ms = int((time.monotonic() - t_conn) * 1000)
            turn.log_mcp(f"Successfully connected (transport: stdio) in {connect_ms}ms")
            turn.log_mcp("Connection established with capabilities: "
                         + json.dumps({"hasTools": True, "serverVersion": {"name": SERVER_NAME}}))
        except BaseException as exc:  # noqa: BLE001 - a dead or refused server is a failed connection
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            turn.status = "failed"
            turn.tools = []
            session = None
            alive = False
            turn.log_mcp(f"Connection failed: {type(exc).__name__}", key="error")

        allowed = (turn.args.allowed_tools or "").split(",") if turn.args.allowed_tools else []
        if turn.status == "connected" and sorted(allowed) != sorted(MCP_PREFIX + t["name"] for t in turn.tools):
            violation_exit(["--allowedTools does not equal the tools the server lists"])

        deny_mode = beh.get("deny_mode")
        if deny_mode:
            turn.denied = denied_tools(turn.args.settings, beh)
            if deny_mode == "strip":
                turn.tools = [t for t in turn.tools if t["name"] not in turn.denied]

        turn.emit_init()

        if mode in ("hang", "hang_hard"):
            if mode == "hang_hard":
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                await asyncio.sleep(3600)
        if mode == "exit143":
            turn.assistant([{"type": "text", "text": "Starting."}])
            sys.stdout.flush()
            os._exit(143)

        calls = list(beh["witness"].get("calls", []))
        if mode == "refusal" or turn.status != "connected":
            calls = []
        if mode == "max_turns":
            base = calls or [{"tool": "submit_reviews", "args": "auto"}]
            calls = [base[i % len(base)] for i in range(max(0, turn.args.max_turns - 1))]
        if mode == "api_error":
            calls = []

        for idx, call in enumerate(calls):
            name = call["tool"]
            if "client_reject" in call:
                args_in = call.get("args", {}) if isinstance(call.get("args"), dict) else {}
                turn.tool_call_events(name, args_in, str(call["client_reject"]), True)
                turn.write_body(str(call["client_reject"]))
                continue
            args_in = call.get("args", "auto")
            if args_in == "auto":
                args_in = auto_args(name, turn.args.prompt, turn.tools)
            if name not in [t["name"] for t in turn.tools]:
                text = f"Error: No such tool available: {MCP_PREFIX}{name}"
                turn.tool_call_events(name, args_in, text, True)
                turn.write_body(text)
                continue
            if name in turn.denied and beh.get("deny_mode") == "deny":
                text = f"Permission to use {MCP_PREFIX}{name} has been denied."
                turn.denials.append({"tool_name": MCP_PREFIX + name, "tool_use_id": f"toolu_fake_{turn.n_calls + 1}",
                                     "tool_input": args_in})
                turn.tool_call_events(name, args_in, text, True)
                turn.write_body(text)
                continue
            if mode == "server_dies_mid_turn" and idx >= 1 and alive:
                if server_pid:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(server_pid, signal.SIGKILL)
                alive = False
                turn.log_mcp("Server connection closed", key="error")
            if alive:
                turn.log_mcp(f"Calling MCP tool: {name}")
                t_call = time.monotonic()
                try:
                    res = await asyncio.wait_for(session.call_tool(name, args_in), 60)
                    text, is_err = _text_of(res), bool(res.isError)
                    turn.log_mcp(f"Tool '{name}' completed successfully in {int((time.monotonic() - t_call) * 1000)}ms")
                except BaseException as exc:  # noqa: BLE001
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    text, is_err = "MCP error -32000: Connection closed", True
                    alive = False
            else:
                text, is_err = "MCP error -32000: Connection closed", True
            turn.tool_call_events(name, args_in, text, is_err)
            turn.write_body(text)

        if mode == "crash_after_calls":
            sys.stdout.flush()
            os._exit(143)

        final_text = beh["witness"].get("final_text", "Done.")
        if mode == "refusal":
            final_text = beh.get("refusal_text", "I can't help with that.")
        if mode == "api_error":
            return _emit_api_error(turn, beh)
        turn.assistant([{"type": "text", "text": final_text}], stop_reason="refusal" if mode == "refusal" else None)
        turn.write_body(final_text)

        if mode == "truncate":
            sys.stdout.write('{"type":"result","subtype":"succ')
            sys.stdout.flush()
            os._exit(0)
        if float(beh.get("sleep_s", 0) or 0) > 0 and mode in ("slow", "normal"):
            await asyncio.sleep(float(beh["sleep_s"]))
        _emit_result(turn, beh, final_text)
    errfh.close()
    try:
        noise = errpath.read_text(encoding="utf-8").strip()
        if noise:
            turn.log_mcp("Server stderr: " + noise[:2000], key="error")
        errpath.unlink()
    except OSError:
        pass
    return 0


def _emit_api_error(turn: Turn, beh: dict) -> int:
    status = int(beh.get("status", 429))
    if status != 402:
        for n in (1, 2):
            emit({"type": "system", "subtype": "api_retry", "attempt": n, "max_retries": 10, "retry_delay_ms": 500,
                  "error_status": status, "error": "rate_limit" if status == 429 else "server_error",
                  **turn.common()})
    text = "Credit balance is too low" if status == 402 else f"API Error: {status} fake upstream error"
    turn.result(subtype=beh.get("subtype", "success"), is_error=True, text=text, stop_reason=None,
                api_error_status=status)
    return 0


def _emit_result(turn: Turn, beh: dict, final_text: str) -> None:
    mode = beh["mode"]
    if mode == "max_turns":
        turn.result(subtype="error_max_turns", is_error=True, stop_reason=None, num_turns=turn.args.max_turns,
                    no_result_field=True)
    elif mode == "max_budget":
        turn.result(subtype="error_max_budget_usd", is_error=True, stop_reason=None, no_result_field=True)
    elif mode == "refusal":
        # real 2.1.317: success + is_error true, no API status, stop_reason refusal
        turn.result(text=final_text, stop_reason="refusal", is_error=True)
    elif mode == "wrong_model":
        turn.result(text=final_text, usage_model="claude-x")
    elif mode == "permission_denial":
        turn.result(text=final_text, permission_denials=[
            {"tool_name": MCP_PREFIX + beh.get("denied_tool", "notify_ops"), "tool_use_id": "toolu_fake_denied", "tool_input": {}}])
    else:
        turn.result(text=final_text)


def _run_debrief(turn: Turn, beh: dict, which: str) -> int:
    mode = beh["mode"]
    turn.emit_init()
    if mode in ("hang", "hang_hard"):
        if mode == "hang_hard":
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(3600)
    if mode == "exit143":
        turn.assistant([{"type": "text", "text": "Starting."}])
        os._exit(143)
    text = beh[which]["text"]
    turn.assistant([{"type": "text", "text": text}])
    turn.write_body(text)
    if mode == "crash_after_calls":
        os._exit(143)
    if mode == "truncate":
        sys.stdout.write('{"type":"result","subtype":"succ')
        sys.stdout.flush()
        os._exit(0)
    if mode == "api_error":
        return _emit_api_error(turn, beh)
    if float(beh.get("sleep_s", 0) or 0) > 0:
        time.sleep(float(beh["sleep_s"]))
    _emit_result(turn, beh, text)
    return 0


# ---------------------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "--behavior":
        violation_exit(["fake invocation: expected --behavior <json> before the claude argv"])
    try:
        doc = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        violation_exit([f"fake invocation: unreadable behaviour file ({type(exc).__name__})"])
    claude_args = argv[2:]
    contract = doc.get("contract", {})
    skip = set(contract.get("skip", []))
    models = tuple(contract.get("models") or DEFAULT_MODELS)

    violations: list[str] = []
    args, v = check_argv(claude_args, models)
    if "argv" not in skip:
        violations += v
    if "stdin" not in skip:
        try:
            if not stat.S_ISCHR(os.fstat(0).st_mode):
                violations.append("stdin is not /dev/null (not a character device)")
        except OSError:
            violations.append("stdin is closed")
    cwd = os.getcwd()
    if "cwd" not in skip and contract.get("cwd") and os.path.realpath(cwd) != os.path.realpath(contract["cwd"]):
        violations.append(f"cwd is {cwd!r}, expected {contract['cwd']!r}")
    env = dict(os.environ)
    if "env" not in skip:
        violations += check_environment(env, contract)
    lock_fh = None
    if "slot" not in skip:
        sv, lock_fh = check_slot(env, contract)
        violations += sv
    if "system_file" not in skip and args.system_file:
        violations += check_system_file(args.system_file)

    mcp_cfg: dict | None = None
    if args.is_witness and args.mcp_config is not None:
        try:
            mcp_cfg = json.loads(args.mcp_config)
            srv = mcp_cfg["mcpServers"][SERVER_NAME]
            if srv.get("type") != "stdio" or not srv.get("command"):
                violations.append("mcp-config: workspace server must be type stdio with a command")
            bad = sorted(k for k in srv.get("env", {}) if SECRET_ENV_RE.search(k))
            if bad:
                violations.append("mcp-config: credential-like env names " + ",".join(bad))
            if list(mcp_cfg["mcpServers"]) != [SERVER_NAME]:
                violations.append("mcp-config: exactly one server named workspace expected")
        except (ValueError, KeyError, TypeError):
            violations.append("mcp-config: not valid JSON of the expected shape")
            mcp_cfg = None
    if violations:
        violation_exit(violations)

    # facts for rule matching
    facts: dict = {"model": args.model, "prompt": args.prompt}
    if args.is_witness and mcp_cfg:
        e = mcp_cfg["mcpServers"][SERVER_NAME].get("env", {})
        facts.update({"turn_type": "witness", "world": e.get("WTTH_WORLD"), "round": e.get("WTTH_ROUND"),
                      "slot": e.get("WTTH_SLOT"), "attempt": e.get("WTTH_ATTEMPT"),
                      "episode_id": e.get("WTTH_EPISODE_ID"), "sender": e.get("WTTH_SENDER"),
                      "toolset": e.get("WTTH_TOOLSET")})
    else:
        facts["turn_type"] = "d2" if "\nEarlier question: " in args.prompt else "d1"
    beh = resolve_behavior(doc, facts)
    if beh["mode"] not in MODES:
        violation_exit([f"unknown mode {beh['mode']!r}"])

    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.write(STDERR_WARNING + "\n")
    if beh["mode"] == "stderr_noise":
        sys.stderr.write(STDERR_NOISE + "\n")
    sys.stderr.flush()

    turn = Turn(args, beh, env, cwd)
    if args.is_witness:
        assert mcp_cfg is not None
        return asyncio.run(_run_witness(turn, mcp_cfg, beh))
    return _run_debrief(turn, beh, facts["turn_type"])


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stdout.flush()
    sys.exit(code)
