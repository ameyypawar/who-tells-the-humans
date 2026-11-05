"""Startup verification and run manifest (research/41 section 2.7). Owner: WP4."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from wtth import __version__
from wtth.config import CONFIGS_DIR, env_allowlist, load_caps, load_prereg_pins, load_toolsets
from wtth.textnorm import canon_json, sha256_text

REFUSAL_TEXT_RULE = (r"(?i)\bI (?:can(?:no|')t|won't|will not|am not able to|'m not able to) "
                     r"(?:help|assist|participate|take part|continue|comply|do that)\b | "
                     r"(?i)\bI (?:have to|need to) decline\b")
AUDIT_SAMPLE_RULE = "int(turn_id[:8],16) % production_sample_mod == 0 (G0 phases: every turn)"
FROZEN_PHASES = ("core", "sonnet", "mvp")
SETTINGS_REQUIRED = {"switchModelsOnFlag": False, "disableAllHooks": True, "disableClaudeAiConnectors": True,
                     "autoMemoryEnabled": False, "crossSessionInbound": "refuse"}


def _refusal_patterns() -> list[str]:
    try:
        from wtth.runner.validate import REFUSAL_TEXT_PATTERNS
        return list(REFUSAL_TEXT_PATTERNS)
    except Exception:
        return [REFUSAL_TEXT_RULE]


def _sha_file(path: Path) -> str | None:
    """Digest only; content (including withheld private files) is never returned or printed."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def parse_section23(prereg_md: Path) -> dict[str, str]:
    text = prereg_md.read_text(encoding="utf-8")
    sec = text.split("## 23 File and hash register", 1)[1].split("\n## 24", 1)[0]
    out = {}
    for line in sec.splitlines():
        m = re.match(r"^\| ([^|]+?) \| [^|]* \| `([0-9a-f]{64})` \|$", line)
        if m:
            out[m.group(1).strip()] = m.group(2)
    return out


def _md_tools(path: Path) -> list[dict]:
    out = []
    for m in re.finditer(r"```text\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S):
        body = m.group(1)
        if not body.startswith("name: "):
            continue
        d: dict = {"params": []}
        in_params = False
        for line in body.split("\n"):
            if line.startswith("name: "):
                d["name"] = line[6:]
            elif line.startswith("description: "):
                d["description"] = line[13:]
            elif line == "parameters:":
                in_params = True
            elif line.startswith("reply: "):
                d["reply"], in_params = line[7:], False
            elif in_params:
                pm = re.match(r"^( +)(\w+) \((\w+)(?:, ([^)]*))?\)(?:: (.*))?$", line)
                if pm:
                    ind, name, typ, ld, desc = pm.groups()
                    d["params"].append((len(ind), name, typ, ld if typ == "list" else desc))
        out.append(d)
    return out


def _check_tools(settings: Any, problems: list[str]) -> None:
    try:
        sets = load_toolsets(settings.paths.tools_yaml)
        for toolset, md in (("main", settings.paths.prereg_dir / "prompts" / "tools.md"),
                            ("backup", settings.paths.prereg_dir / "prompts" / "backup_scenario.md")):
            tools = {t.name: t for t in sets[toolset].tools}
            for blk in _md_tools(md):
                t = tools.get(blk["name"])
                if t is None or t.description != blk["description"] or t.reply != blk["reply"]:
                    problems.append(f"tools.yaml[{toolset}].{blk['name']} differs from {md.name}")
                    continue
                props = t.input_schema["properties"]
                for ind, pname, _typ, desc in blk["params"]:
                    node = props.get(pname) if ind == 2 else props["reviews"]["items"]["properties"].get(pname)
                    if node is None or node.get("description") != desc:
                        problems.append(f"tools.yaml[{toolset}].{blk['name']}.{pname} differs from {md.name}")
    except Exception as exc:
        problems.append(f"tools.yaml check failed: {type(exc).__name__}: {exc}")


def verify_startup(settings: Any, mode: str, phase: str) -> list[str]:
    """Every problem found (empty list = ready). Live mode adds environment, key and freeze guards."""
    p: list[str] = []
    paths = settings.paths
    try:
        pins = load_prereg_pins(CONFIGS_DIR / "prereg_hashes.json")
    except Exception as exc:
        return [f"configs/prereg_hashes.json unreadable: {exc}"]
    files = pins["files"]
    got = _sha_file(paths.prereg_dir / "PREREG.md")
    if got != files.get("PREREG.md"):
        p.append(f"PREREG.md sha256 {got} != pinned {files.get('PREREG.md')}")
    try:
        table = parse_section23(paths.prereg_dir / "PREREG.md")
    except Exception as exc:
        table = {}
        p.append(f"cannot parse PREREG section 23: {exc}")
    recorded = {k: v for k, v in files.items() if k != "PREREG.md"}
    if table and table != recorded:
        p.append("configs/prereg_hashes.json differs from the PREREG section 23 table")
    from wtth import prereg
    p.extend(prereg.verify_hashes(paths.prereg_dir, paths.private_dir, files))
    from wtth.orchestrator import guards
    bad = guards.wtth_import_problem(settings)
    if bad:
        p.append(bad)
    # binary
    if not paths.binary.exists() or not os.access(paths.binary, os.X_OK):
        p.append(f"binary missing or not executable: {paths.binary}")
    elif not settings.fake:
        want = paths.binary_sha256_file.read_text().split()[0] if paths.binary_sha256_file.exists() else None
        if _sha_file(paths.binary) != want or want != pins.get("binary_sha256"):
            p.append("binary sha256 does not match the pinned value")
    # cc settings
    try:
        cc = json.loads(paths.cc_settings.read_text(encoding="utf-8"))
        for k, v in SETTINGS_REQUIRED.items():
            if cc.get(k) != v:
                p.append(f"cc-settings.json: {k} must be {v!r}")
    except Exception as exc:
        p.append(f"cc-settings.json unreadable: {exc}")
    _check_tools(settings, p)
    # importability under the exact MCP child env
    env = {"PATH": "/usr/bin:/bin"}
    r = subprocess.run([str(paths.python), "-I", "-c", "import wtth.mcp_server"], env=env, cwd="/",
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        p.append("import wtth.mcp_server failed under the MCP child env (missing wtth.pth?): "
                 + r.stderr.strip().splitlines()[-1:][0] if r.stderr.strip() else "import failed")
    r = subprocess.run([str(paths.python), "-I", "-m", "compileall", "-q", str(paths.repo_root / "src" / "wtth")],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        p.append("compileall failed: " + (r.stdout + r.stderr).strip()[:300])
    # cwd and ancestors
    try:
        paths.cwd.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        p.append(f"cannot create cwd {paths.cwd}: {exc}")
    d = paths.cwd.resolve()
    for anc in ([] if settings.fake else [d, *d.parents]):  # fake runs use a test cwd inside the repo tree
        for bad in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude"):
            if (anc / bad).exists():
                p.append(f"{anc / bad} exists (would be read by Claude Code)")
    # slot dirs
    from wtth.orchestrator.context import SlotPool
    pool = SlotPool(paths.slots_root, settings.scheduler.slots)
    try:
        pool.ensure()
        for i in range(pool.n):
            for sub in ("", "home", "config", "tmp"):
                q = paths.slots_root / f"w{i:02d}" / sub
                if (q.stat().st_mode & 0o777) != 0o700:
                    p.append(f"slot dir mode is not 0700: {q}")
    except OSError as exc:
        p.append(f"slot dirs: {exc}")
    if mode == "live":
        for var in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            if var in os.environ:
                p.append(f"{var} is set in the orchestrator environment (run from a plain shell, not Claude Code)")
        try:
            from wtth import secrets
            secrets.load_api_key(paths.key_file)
        except Exception as exc:  # message never contains key content
            p.append(f"API key file: {type(exc).__name__}: {exc}")
        if phase in FROZEN_PHASES:
            g = lambda *a: subprocess.run(["git", "-C", str(paths.repo_root), *a], capture_output=True, text=True)  # noqa: E731
            if g("status", "--porcelain").stdout.strip():
                p.append("git tree is not clean")
            if "frozen-v1" not in g("tag", "--points-at", "HEAD").stdout.split():
                p.append("HEAD does not carry tag frozen-v1")
            if settings.frozen_level is None:
                p.append("frozen_level is not set")
            if not settings.max_turns.frozen:
                p.append("max_turns.frozen is false")
    return p


def _git(settings: Any, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(settings.paths.repo_root), *args], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_manifest(settings: Any, run_id: str, phase: str, mode: str) -> dict:
    from wtth.orchestrator import guards
    from wtth.orchestrator.scheduler import permutation
    paths = settings.paths
    pins = load_prereg_pins(CONFIGS_DIR / "prereg_hashes.json")
    base, audit = env_allowlist(CONFIGS_DIR / "env_allowlist.json")
    toolsets = load_toolsets(paths.tools_yaml)
    try:
        import mcp  # noqa: F401
        import importlib.metadata as md
        mcp_v, yaml_v = md.version("mcp"), md.version("pyyaml")
    except Exception:
        mcp_v = yaml_v = "unknown"
    sys_files = {}
    if paths.stimuli_dir.exists():
        for f in sorted(paths.stimuli_dir.glob("system_*.txt")):
            sys_files[f.name] = _sha_file(f)
    slots = {}
    for i in range(settings.scheduler.slots):
        cfg = paths.slots_root / f"w{i:02d}" / "config"
        slots[f"w{i:02d}"] = {"remote_settings": _sha_file(cfg / "remote-settings.json"),
                              "policy_limits": _sha_file(cfg / "policy-limits.json")}
    mem = ""
    try:
        mem = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    sch, b, mt = settings.scheduler, settings.budget, settings.max_turns
    return {
        "run_id": run_id, "phase": phase, "mode": mode, "harness_version": __version__,
        "git": {"head": _git(settings, "rev-parse", "HEAD"), "dirty": bool(_git(settings, "status", "--porcelain")),
                "tags": _git(settings, "tag", "--points-at", "HEAD").split()},
        "versions": {"python": platform.python_version(), "mcp": mcp_v, "pyyaml": yaml_v,
                     "sqlite": sqlite3.sqlite_version},
        "binary": {"path": str(paths.binary), "sha256": _sha_file(paths.binary), "pinned": pins["binary_sha256"],
                   "fake": settings.fake},
        "models": {"haiku": settings.models.haiku, "sonnet": settings.models.sonnet, "judge": settings.models.judge,
                   "effort": settings.models.effort, "max_budget_usd": settings.models.max_budget_usd},
        "env_names": {"base": sorted(base), "audit": sorted(audit)},
        "src_tree_sha": guards.src_tree_sha(settings), "config_fingerprint": guards.config_fingerprint(settings),
        "cc_settings_sha": _sha_file(paths.cc_settings),
        "tools_sha": {k: v.sha256 for k, v in toolsets.items()},
        "tools_yaml_sha": _sha_file(paths.tools_yaml),
        "system_prompt_shas": sys_files,
        "prereg_hashes": pins["files"],
        "permutation": {"seed": settings.permutation_seed, "order": permutation(settings)},
        "frozen_level": settings.frozen_level,
        "scheduler": {k: getattr(sch, k) for k in sch.__slots__},
        "budget": {"cap_usd": b.cap_usd, "warn_usd": b.warn_usd, "prior_spend_usd": b.prior_spend_usd,
                   "phase_soft_usd": b.phase_soft_usd},
        "timeouts": {"turn_s": settings.timeouts.turn_s, "grace_int_s": settings.timeouts.grace_int_s,
                     "grace_term_s": settings.timeouts.grace_term_s},
        "max_turns": {"witness": mt.witness, "debrief": mt.debrief, "frozen": mt.frozen},
        "max_attempts": settings.max_attempts,
        "caps": dataclasses.asdict(load_caps(paths.tools_yaml)),
        "refusal_text_rule": _refusal_patterns(), "audit_sample_rule": AUDIT_SAMPLE_RULE,
        "host": {"macos": platform.mac_ver()[0], "machine": platform.machine(), "ram_bytes": mem},
        "slots": slots,
    }


def manifest_sha(manifest: dict) -> str:
    return sha256_text(canon_json(manifest))
