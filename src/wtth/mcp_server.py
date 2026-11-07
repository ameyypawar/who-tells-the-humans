"""MCP server `workspace` (FastMCP subclass overriding list_tools/call_tool). Owner: WP2.

Launch: python -I -m wtth.mcp_server --runs-root <abs runs dir>   (WTTH_* env, see identity_from_env).
Startup order (research/41 section 2.3.5): secret-env check, identity_from_env, tools.yaml check, Db open +
schema check, one server_starts row (env var NAMES only), run (stdio). Any startup failure: one stderr line,
a fault line when WTTH_FAULT_DIR/WTTH_TURN_ID allow it, exit code 2 before the handshake.

D-028: a receipts cell's server also gets WTTH_OO_CONFIG (the path of the store's credentials file),
WTTH_OO_URL and WTTH_OO_STREAM (all three or none; none of the names is credential-like). It then opens the receipts
store for its shift (wtth.receipts) and reads it once before the handshake; a toolset with a dynamic-reply tool
(lookup_escalation) refuses to start without it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, Tool

from wtth import SCHEMA_VERSION
from wtth import receipts as receipts_mod
from wtth.handlers import FaultSink, ToolUnavailable, dispatch
from wtth.types import Caps, Identity, Toolset, World

SECRET_ENV_RE = re.compile(r"^ANTHROPIC|_KEY$|_TOKEN$|_SECRET$|PASSWORD")
_SENDER_RE = re.compile(r"^(rd|fl)-\d\d$")
_CLOCK_RE = re.compile(r"^(\d\d):(\d\d)$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

REQUIRED_ENV = (
    "WTTH_DB", "WTTH_RUN_ID", "WTTH_EPISODE_ID", "WTTH_ATTEMPT", "WTTH_ROUND", "WTTH_SLOT", "WTTH_SENDER",
    "WTTH_CLOCK", "WTTH_VISIBLE_FROM", "WTTH_WORLD", "WTTH_TURN_ID", "WTTH_TOOLSET", "WTTH_TOOLS_PATH",
    "WTTH_TOOLS_SHA256", "WTTH_TICKET_IDS", "WTTH_FAULT_DIR", "WTTH_SCHEMA_VERSION",
)


@dataclass(frozen=True, slots=True)
class ServerConfig:
    runs_root: Path
    db_path: Path
    fault_dir: Path
    tools_path: Path
    tools_sha256: str
    schema_version: int
    toolset: Toolset
    caps: Caps
    env_names: tuple[str, ...]
    receipts: tuple[str, str, Path, str] | None = None   # D-028: (url, stream, credentials file, shift key)


def _startup_fault(environ: Mapping[str, str], msg: str) -> None:
    """Best effort: a fault line for the turn, when the dir and turn id are usable."""
    try:
        fault_dir = environ.get("WTTH_FAULT_DIR", "")
        turn_id = environ.get("WTTH_TURN_ID", "")
        if fault_dir and Path(fault_dir).is_dir() and _HEX32_RE.match(turn_id):
            FaultSink(Path(fault_dir), turn_id).record("startup", {}, msg, None)
    except Exception:  # noqa: BLE001
        pass


def _fail(environ: Mapping[str, str], msg: str) -> "None":
    sys.stderr.write(f"wtth.mcp_server: {msg}\n")
    sys.stderr.flush()
    _startup_fault(environ, msg)
    sys.exit(2)


def _guard_mode() -> str:
    """mcp_env_guard from experiment.yaml ('refuse' default). Reads no environment variables."""
    try:
        from wtth.config import load_settings
        return load_settings().mcp_env_guard
    except Exception:  # noqa: BLE001 - unreadable config: stay strict
        return "refuse"


def scrub_secret_env(environ: "os._Environ[str] | dict[str, str]") -> list[str]:
    """Delete credential-like names from `environ` in place; one stderr line (names only). Returns the names."""
    bad = sorted(n for n in environ if SECRET_ENV_RE.search(n))
    for n in bad:
        del environ[n]
    if bad:
        sys.stderr.write("wtth.mcp_server: scrubbed credential-like env names: " + ",".join(bad) + "\n")
        sys.stderr.flush()
    return bad


def guard_env(environ: "os._Environ[str] | dict[str, str]") -> None:
    """Startup guard: refuse (exit 2) or scrub, per mcp_env_guard. Runs before anything else reads the env."""
    if _guard_mode() == "scrub":
        scrub_secret_env(environ)
    else:
        assert_no_secret_env(environ)


def assert_no_secret_env(environ: Mapping[str, str]) -> None:
    """Exit 2 if any env var name looks like a credential (names only are reported)."""
    bad = sorted(n for n in environ if SECRET_ENV_RE.search(n))
    if bad:
        _fail(environ, "refusing to run: credential-like environment variable names present: " + ",".join(bad))


def _argv_runs_root(argv: Sequence[str]) -> str | None:
    args = list(argv)
    for i, a in enumerate(args):
        if a == "--runs-root" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--runs-root="):
            return a.split("=", 1)[1]
    return None


def _int(environ: Mapping[str, str], name: str, lo: int, hi: int | None = None) -> int:
    raw = environ[name]
    if not re.fullmatch(r"-?\d+", raw):
        raise ValueError(f"{name} is not an integer")
    v = int(raw)
    if v < lo or (hi is not None and v > hi):
        raise ValueError(f"{name} out of range")
    return v


def identity_from_env(environ: Mapping[str, str], argv: Sequence[str]) -> tuple[Identity, ServerConfig]:
    """Validate the WTTH_* contract. Any failure: one stderr line + sys.exit(2)."""
    try:
        return _identity_from_env(environ, argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        _fail(environ, f"invalid environment: {type(exc).__name__}: {exc}")
        raise  # unreachable


def _identity_from_env(environ: Mapping[str, str], argv: Sequence[str]) -> tuple[Identity, ServerConfig]:
    from wtth.config import load_caps, load_toolsets

    missing = [n for n in REQUIRED_ENV if not environ.get(n)]
    if missing:
        raise ValueError("missing env: " + ",".join(missing))
    runs_arg = _argv_runs_root(argv)
    if not runs_arg:
        raise ValueError("missing --runs-root")
    runs_root = Path(runs_arg).resolve()

    db_path = Path(environ["WTTH_DB"])
    if not db_path.is_absolute() or not db_path.is_file():
        raise ValueError("WTTH_DB is not an existing absolute file path")
    if db_path.resolve().parent != runs_root:
        raise ValueError("WTTH_DB is not directly under --runs-root")

    run_id = environ["WTTH_RUN_ID"]
    episode_id = _int(environ, "WTTH_EPISODE_ID", 1)
    attempt = _int(environ, "WTTH_ATTEMPT", 1)
    rnd = _int(environ, "WTTH_ROUND", 1, 5)
    slot = _int(environ, "WTTH_SLOT", 1, 3)
    sender = environ["WTTH_SENDER"]
    if not _SENDER_RE.match(sender):
        raise ValueError("WTTH_SENDER invalid")
    clock = environ["WTTH_CLOCK"]
    m = _CLOCK_RE.match(clock)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("WTTH_CLOCK invalid")
    clock_min = int(m.group(1)) * 60 + int(m.group(2))
    visible = _int(environ, "WTTH_VISIBLE_FROM", 1)
    if visible != rnd + 1:
        raise ValueError("WTTH_VISIBLE_FROM != round + 1")
    if environ["WTTH_WORLD"] not in ("R0", "R1"):
        raise ValueError("WTTH_WORLD invalid")
    world = World(environ["WTTH_WORLD"])
    turn_id = environ["WTTH_TURN_ID"]
    if not _HEX32_RE.match(turn_id):
        raise ValueError("WTTH_TURN_ID invalid")
    ticket_ids = tuple(t.strip() for t in environ["WTTH_TICKET_IDS"].split(","))
    if len(ticket_ids) != 3 or not all(ticket_ids):
        raise ValueError("WTTH_TICKET_IDS must hold 3 ids")
    fault_dir = Path(environ["WTTH_FAULT_DIR"])
    if not fault_dir.is_dir():
        raise ValueError("WTTH_FAULT_DIR is not a directory")
    schema_env = _int(environ, "WTTH_SCHEMA_VERSION", 0)
    if schema_env != SCHEMA_VERSION:
        raise ValueError("WTTH_SCHEMA_VERSION mismatch")

    tools_path = Path(environ["WTTH_TOOLS_PATH"])
    if not tools_path.is_file():
        raise ValueError("WTTH_TOOLS_PATH is not a file")
    toolsets = load_toolsets(tools_path)
    tname = environ["WTTH_TOOLSET"]
    if tname not in toolsets:
        raise ValueError("WTTH_TOOLSET not in tools.yaml")
    toolset = toolsets[tname]
    if toolset.sha256 != environ["WTTH_TOOLS_SHA256"]:
        raise ValueError("WTTH_TOOLS_SHA256 does not match the toolset")
    caps = load_caps(tools_path)

    # D-028: the receipts store env, all three or none
    oo = {n: environ.get(n, "") for n in receipts_mod.OO_ENV}
    if any(oo.values()) and not all(oo.values()):
        raise ValueError("WTTH_OO_CONFIG, WTTH_OO_URL and WTTH_OO_STREAM come together")
    receipts_on = all(oo.values())
    if any(t.dynamic_reply for t in toolset.tools) and not receipts_on:
        raise ValueError("WTTH_TOOLSET reads the receipts store but WTTH_OO_* is not set")
    if receipts_on:
        if world is not World.R1:
            raise ValueError("receipts need WTTH_WORLD R1")
        cred = Path(oo["WTTH_OO_CONFIG"])
        if not cred.is_absolute() or not cred.is_file():
            raise ValueError("WTTH_OO_CONFIG is not an existing absolute file path")
        if cred.stat().st_mode & 0o077:
            raise ValueError("WTTH_OO_CONFIG is readable by group or others")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise ValueError(f"database user_version {version} != {SCHEMA_VERSION}")
        row = conn.execute("SELECT a.status, e.episode_key FROM attempts a JOIN episodes e USING(episode_id) "
                           "WHERE a.episode_id=? AND a.attempt=?", (episode_id, attempt)).fetchone()
    finally:
        conn.close()
    if row is None or row[0] != "running":
        raise ValueError("no running attempt row for WTTH_EPISODE_ID/WTTH_ATTEMPT")

    ident = Identity(run_id=run_id, episode_id=episode_id, attempt=attempt, round=rnd, slot=slot, sender=sender,
                     clock=clock, clock_min=clock_min, visible_from_round=visible, world=world, source="tool",
                     turn_id=turn_id, toolset=tname, ticket_ids=ticket_ids, receipts=receipts_on)
    rcfg = None
    if receipts_on:
        rcfg = (oo["WTTH_OO_URL"], oo["WTTH_OO_STREAM"], Path(oo["WTTH_OO_CONFIG"]),
                receipts_mod.episode_key(row[1], attempt, run_id))
    cfg = ServerConfig(runs_root=runs_root, db_path=db_path, fault_dir=fault_dir, tools_path=tools_path,
                       tools_sha256=toolset.sha256, schema_version=schema_env, toolset=toolset, caps=caps,
                       env_names=tuple(sorted(environ)), receipts=rcfg)
    return ident, cfg


def open_receipts(environ: Mapping[str, str], cfg: ServerConfig) -> "receipts_mod.ReceiptStore | None":
    """D-028: the shift's receipts store, read once (reachable, authorised, stream present) before the handshake.
    Any failure: one stderr line with the failure kind only, a fault line, exit 2."""
    if cfg.receipts is None:
        return None
    url, stream, cred, key = cfg.receipts
    try:
        store = receipts_mod.ReceiptStore(url, stream, cred, episode_key=key)
        store.fetch()
    except receipts_mod.ReceiptStoreError as exc:
        _fail(environ, f"receipts store: {exc.kind}")
        raise  # unreachable
    return store


class WorkspaceServer(FastMCP):
    """FastMCP transport/session with tool listing and calls taken over from tools.yaml (research/41 D1)."""

    def __init__(self, ident: Identity, cfg: ServerConfig, db, receipts=None) -> None:
        self._ident = ident
        self._cfg = cfg
        self._db = db
        self._receipts = receipts
        self._fault = FaultSink(cfg.fault_dir, ident.turn_id)
        super().__init__("workspace", log_level="WARNING")

    async def list_tools(self) -> list[Tool]:  # type: ignore[override]
        return [Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
                for t in self._cfg.toolset.tools]

    async def call_tool(self, name: str, arguments: dict) -> "list[TextContent] | CallToolResult":  # type: ignore[override]
        reply = self._cfg.toolset.tools[0].reply if self._cfg.toolset.tools else ""
        try:
            reply = dispatch(self._db, self._ident, self._cfg.toolset, name,
                             arguments if isinstance(arguments, dict) else {}, self._fault, self._cfg.caps,
                             receipts=self._receipts)
        except ToolUnavailable:
            # D-028: the receipts store failed; the fault line is written. An MCP error, never a success reply.
            return CallToolResult(content=[TextContent(type="text", text=receipts_mod.UNAVAILABLE)], isError=True)
        except BaseException as exc:  # noqa: BLE001 - never raise to FastMCP
            try:
                self._fault.record(str(name), {}, type(exc).__name__, self._ident)
            except BaseException:  # noqa: BLE001
                pass
        return [TextContent(type="text", text=reply)]


def main() -> None:
    from wtth.db import Db, WriteFailed
    from wtth.textnorm import canon_json

    guard_env(os.environ)
    ident, cfg = identity_from_env(os.environ, sys.argv[1:])
    store = open_receipts(os.environ, cfg)
    db = Db(cfg.db_path)

    def fn(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO server_starts(turn_id, pid, ts, env_names_json) VALUES (?,?,?,?)",
                     (ident.turn_id, os.getpid(), time.time(), canon_json(list(cfg.env_names))))

    try:
        db.write(fn)
    except (WriteFailed, sqlite3.Error) as exc:
        _fail(os.environ, f"server_starts write failed: {type(exc).__name__}")
    WorkspaceServer(ident, cfg, db, store).run()


if __name__ == "__main__":
    main()
