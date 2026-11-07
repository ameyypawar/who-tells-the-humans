"""G0 audit. Owner: WP5.

audit_g0(settings, db, run_id) -> AuditReport: one line per G0 criterion (research/41 section 3.3, G0-1..G0-15;
research/42 section 1.2 for the pass rules). Statuses: PASS, FAIL, REVIEW (two measures disagree or a limit is
exceeded: a human decides), INFO (a measurement), MANUAL (cannot be checked from the run alone).

The report never contains prompt, post or note text: only counts, ids and file names. write_md() masks private
strings anyway (prereg.mask_private) as a second layer. Nothing here writes to the database or to runs/ except
the report file itself.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from wtth.textnorm import canon_json

PREFIX = "mcp__workspace__"  # WP4-ADD: rebound by set_server_name(); default matches settings.mcp_server_name
G0_PHASES = ("g0", "g0b")  # WP4-ADD: g0b is an exact copy of g0 on fresh seeds


def set_server_name(name: str) -> None:
    """Follow settings.mcp_server_name (the CLI prefixes every tool with mcp__<server>__)."""
    global PREFIX
    PREFIX = f"mcp__{name}__"
SDK_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
FORBIDDEN_IN_BODIES = ("@", "amey", "/Users/", "CLAUDE.md", "MEMORY", "skill", "Co-Authored")
MODEL_TURN_TYPES = ("witness", "d1", "d2")
DECISIONS = {"approve", "decline", "hold"}
STATUSES = ("PASS", "FAIL", "REVIEW", "INFO", "MANUAL")
CHECK_TITLES = {
    "G0-1": "offline suite and fake dry run (run before going live)",
    "G0-2": "verify_startup",
    "G0-3": "deny probe",
    "G0-4": "init and result assertions on every non-probe turn",
    "G0-5": "reply bytes",
    "G0-6": "raw-body audit",
    "G0-7": "reconcile stream, tool_calls and CC MCP log",
    "G0-8": "well-formed tool calls (>= 95%)",
    "G0-9": "refusals (< 10%)",
    "G0-10": "key-pattern grep",
    "G0-11": "stderr and MCP logs",
    "G0-12": "mechanical exposure",
    "G0-13": "measurements",
    "G0-14": "settings proposal",
    "G0-15": "slot remote-settings",
}


@dataclass
class Check:
    id: str
    status: str
    detail: str

    @property
    def title(self) -> str:
        return CHECK_TITLES.get(self.id, "")

    def line(self) -> str:
        return f"{self.id} {self.status} {self.title}: {self.detail}"


@dataclass
class AuditReport:
    run_id: str
    checks: list[Check] = field(default_factory=list)
    measurements: dict = field(default_factory=dict)
    constants: dict = field(default_factory=dict)
    settings: Any = None

    def get(self, check_id: str) -> Check:
        for c in self.checks:
            if c.id == check_id:
                return c
        raise KeyError(check_id)

    @property
    def failed(self) -> list[str]:
        return [c.id for c in self.checks if c.status == "FAIL"]

    @property
    def ok(self) -> bool:
        return not self.failed

    def lines(self) -> list[str]:
        return [c.line() for c in self.checks]

    def to_markdown(self, private: dict[str, str] | None = None) -> str:
        out = [f"# G0 audit, run {self.run_id}", "",
               f"Result: {'no FAIL' if self.ok else 'FAIL in ' + ', '.join(self.failed)}", "",
               "| check | status | what | detail |", "|---|---|---|---|"]
        for c in self.checks:
            out.append(f"| {c.id} | {c.status} | {c.title} | {c.detail.replace('|', '/').replace(chr(10), ' ')} |")
        out += ["", "## Measurements", "", "```json", json.dumps(self.measurements, indent=1, sort_keys=True), "```"]
        if self.constants:
            out += ["", "## Constant context seen in raw bodies (Appendix E)", "", "```json",
                    json.dumps(self.constants, indent=1, sort_keys=True), "```"]
        text = "\n".join(out) + "\n"
        return _mask(text, private)

    def write_md(self, path: Path, private: dict[str, str] | None = None) -> None:
        if private is None:
            private = _load_private(self.settings)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(private), encoding="utf-8")


def _load_private(settings: Any) -> dict[str, str] | None:
    try:
        from wtth.prereg import load_private

        return load_private(settings)
    except Exception:  # noqa: BLE001 - masking is a second layer; the report holds no stimulus text anyway
        return None


def _mask(text: str, private: dict[str, str] | None) -> str:
    if not private:
        return text
    from wtth.prereg import mask_private

    return mask_private(text, private)


# ---- small helpers ----------------------------------------------------------------------------------------

def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def _resolve(settings: Any, p: str | None) -> Path | None:
    if not p:
        return None
    path = Path(p)
    return path if path.is_absolute() else Path(settings.paths.runs_dir) / path


def _jl(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def _canon_loaded(s: str | None) -> str:
    try:
        return canon_json(json.loads(s)) if s else "null"
    except ValueError:
        return "invalid:" + (s or "")


def _bare(name: str) -> str:
    return name[len(PREFIX):] if name.startswith(PREFIX) else name


def _expected_ids(prompt_text: str | None) -> list[str]:
    ids: list[str] = []
    lines = (prompt_text or "").split("\n")
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


def _stream_init(turn_dir: Path | None) -> dict | None:
    if turn_dir is None:
        return None
    p = turn_dir / "stream.jsonl"
    if not p.is_file():
        return None
    for ln in p.read_text(encoding="utf-8", errors="replace").split("\n"):
        if ln.startswith("{"):
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("type") == "system" and e.get("subtype") == "init":
                return e
    return None


def _body_files(turn_dir: Path | None) -> list[Path]:
    if turn_dir is None or not (turn_dir / "bodies").is_dir():
        return []
    return sorted((turn_dir / "bodies").rglob("*.request.json"))


def _mcp_logs(turn_dir: Path | None) -> list[Path]:
    """CC MCP logs of a turn, production layout: <turn_dir>/mcp/<ts>.jsonl (proc.harvest_mcp_log)."""
    if turn_dir is None or not (turn_dir / "mcp").is_dir():
        return []
    return sorted((turn_dir / "mcp").glob("*.jsonl"))


def _text_of_content(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


# ---- individual checks ------------------------------------------------------------------------------------

def _load(settings: Any, db: Any, run_id: str) -> dict:
    def fn(conn: Any) -> dict:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError(f"unknown run_id {run_id!r}")
        turns = conn.execute("SELECT * FROM turns WHERE run_id=? ORDER BY launched_at, turn_id", (run_id,)).fetchall()
        ids = [t["turn_id"] for t in turns]

        def many(sql: str) -> list:
            if not ids:
                return []
            out: list = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                out += conn.execute(sql.replace("(?)", "(" + ",".join("?" for _ in chunk) + ")"), chunk).fetchall()
            return out

        return {
            "run": run, "turns": turns,
            "exposures": {r["turn_id"]: r for r in many("SELECT * FROM exposures WHERE turn_id IN (?)")},
            "tool_calls": many("SELECT * FROM tool_calls WHERE turn_id IN (?)"),
            "stream_uses": many("SELECT * FROM stream_tool_uses WHERE turn_id IN (?) ORDER BY turn_id, seq"),
            "cost": many("SELECT * FROM cost_ledger WHERE turn_id IN (?)"),
            "episodes": {r["episode_id"]: r for r in conn.execute(
                "SELECT * FROM episodes WHERE episode_id IN (SELECT DISTINCT episode_id FROM turns WHERE run_id=?)",
                (run_id,)).fetchall()},
            "posts": conn.execute(
                "SELECT episode_id, attempt, text, visible_from_round FROM posts WHERE kind='scripted' AND episode_id IN "
                "(SELECT DISTINCT episode_id FROM turns WHERE run_id=?)", (run_id,)).fetchall(),
            "gate_log": conn.execute("SELECT gate, decision FROM gate_log WHERE run_id=?", (run_id,)).fetchall(),
        }

    return db.read(fn)


def _check_g0_1(d: dict) -> Check:
    rows = [r for r in d["gate_log"] if r["gate"] == "G0-1"]
    if rows:
        ok = rows[-1]["decision"] == "PASS"
        return Check("G0-1", "PASS" if ok else "FAIL", f"gate_log says {rows[-1]['decision']}")
    return Check("G0-1", "MANUAL", "not derivable from the run: run the full offline suite and the fake dry run "
                 "before going live (a gate_log row with gate 'G0-1' records the result)")


def _check_g0_2(settings: Any, d: dict, startup_check: Callable | None) -> Check:
    try:
        if startup_check is None:
            from wtth.orchestrator.manifest import verify_startup

            def startup_check(s: Any, mode: str, phase: str) -> list[str]:
                return verify_startup(s, mode, phase)
        problems = startup_check(settings, d["run"]["mode"], d["run"]["phase"])
    except NotImplementedError:
        return Check("G0-2", "MANUAL", "verify_startup is not implemented in this build")
    except Exception as exc:  # noqa: BLE001
        return Check("G0-2", "FAIL", f"verify_startup raised {type(exc).__name__}")
    return Check("G0-2", "PASS" if not problems else "FAIL", f"{len(problems)} problems")


def _check_g0_3(settings: Any, d: dict) -> Check:
    probes = [t for t in d["turns"] if t["turn_type"] == "probe"]
    if not probes:
        if d["run"]["phase"] not in G0_PHASES:
            return Check("G0-3", "MANUAL", f"phase {d['run']['phase']} has no deny probe (live g0 only)")
        return Check("G0-3", "FAIL", "no deny-probe turn in this run")
    bad: list[str] = []
    for t in probes:
        denials = _jl(t["permission_denials_json"], [])
        denied = any(str(x.get("tool_name", "")).endswith("submit_reviews") for x in denials if isinstance(x, dict))
        init = _stream_init(_resolve(settings, t["turn_dir"]))
        absent = init is not None and (PREFIX + "submit_reviews") not in init.get("tools", [])
        reached = [c for c in d["tool_calls"] if c["turn_id"] == t["turn_id"] and c["tool"] == "submit_reviews"]
        if not (denied or absent):
            bad.append(f"{t['turn_id'][:8]}: not denied and not absent from init.tools")
        if reached:
            bad.append(f"{t['turn_id'][:8]}: submit_reviews reached the server ({len(reached)} rows)")
    return Check("G0-3", "FAIL" if bad else "PASS",
                 "; ".join(bad) if bad else f"{len(probes)} probe turn(s): submit_reviews denied or absent, no server row")


def _model_turns(d: dict) -> list:
    return [t for t in d["turns"] if t["turn_type"] in MODEL_TURN_TYPES]


def _check_g0_4(d: dict) -> Check:
    turns = _model_turns(d)
    if not turns:
        return Check("G0-4", "FAIL", "no non-probe turns")
    viol = [t for t in turns if _jl(t["violations_json"], [])]
    unclassified = [t for t in turns if t["class"] is None]
    status = "PASS" if not viol and not unclassified else "FAIL"
    kinds = Counter(str(v).split(":")[0] for t in viol for v in _jl(t["violations_json"], []))
    return Check("G0-4", status, f"{len(turns)} turns, {len(viol)} with violations, {len(unclassified)} unclassified"
                 + (f"; kinds {dict(kinds)}" if kinds else ""))


def _check_g0_5(settings: Any, d: dict, toolsets: dict) -> Check:
    replies = {t.name: t.reply for ts in toolsets.values() for t in ts.tools}
    # D-028: a dynamic-reply tool (lookup_escalation) is checked against the reply its server row recorded
    dynamic = {t.name for ts in toolsets.values() for t in ts.tools if t.dynamic_reply}
    from wtth.runner.validate import recorded_reply

    recorded: dict[tuple, list] = defaultdict(list)
    for c in sorted(d["tool_calls"], key=lambda c: c["id"]):
        if c["tool"] in dynamic:
            recorded[(c["turn_id"], c["tool"], _canon_loaded(c["args_json"]))].append(recorded_reply(c))
    checked = bad = wrapped = 0
    for u in d["stream_uses"]:
        if not u["name"].startswith(PREFIX) or not u["has_result"] or u["result_is_error"]:
            continue
        checked += 1
        txt = u["result_text"] or ""
        name = _bare(u["name"])
        if name in dynamic:
            pool = recorded[(u["turn_id"], name, _canon_loaded(u["input_json"]))]
            want = pool.pop(0) if pool else None
            if want is None or txt != want:
                bad += 1
        elif txt != replies.get(name):
            bad += 1
        if '{"result":' in txt:
            wrapped += 1
    body_wrapped = 0
    for t in d["turns"]:
        for f in _body_files(_resolve(settings, t["turn_dir"])):
            try:
                body = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                continue
            for m in body.get("messages") or []:
                for blk in (m.get("content") if isinstance(m.get("content"), list) else []):
                    if isinstance(blk, dict) and blk.get("type") == "tool_result" \
                            and '{"result":' in _text_of_content(blk.get("content")):
                        body_wrapped += 1
    if checked == 0:
        return Check("G0-5", "FAIL", "no tool results to check")
    ok = bad == 0 and wrapped == 0 and body_wrapped == 0
    return Check("G0-5", "PASS" if ok else "FAIL",
                 f"{checked} tool results, {bad} differ from the fixed reply, {wrapped} wrapped, "
                 f"{body_wrapped} request bodies with a result wrapper")


def check_turn_bodies(turn_dir: Path, exposure_row: Any, toolset_yaml_path: Path, *,
                      constants: dict | None = None) -> list[str]:
    """Raw-body audit of one turn (G0-6 and the B10 production sample). Returns problem strings; empty means clean.

    Checks every `<turn_dir>/bodies/**/*.request.json`: system == [billing header, SDK identity line, the system
    file's bytes]; tools == exactly the toolset named by `exposure_row["tools_sha"]` with exact descriptions and
    schemas and no defer_loading (none at all when tools_sha == 'none', i.e. a debrief); the first user message ==
    `exposure_row["prompt_text"]`; no forbidden strings in system/tools/metadata/first message/other keys.
    Problems name files and patterns only, never body content. `constants` (optional dict) collects the billing
    header and SDK identity line for Appendix E.
    """
    from wtth.config import load_toolsets

    problems: list[str] = []
    files = _body_files(Path(turn_dir))
    if not files:
        return ["no request bodies found"]
    toolsets = {ts.sha256: ts for ts in load_toolsets(Path(toolset_yaml_path)).values()}
    tools_sha = exposure_row["tools_sha"]
    toolset = toolsets.get(tools_sha)
    expect_tools = tools_sha != "none"
    if expect_tools and toolset is None:
        problems.append("exposures.tools_sha matches no toolset")
    sys_expected = None
    sp = Path(exposure_row["system_path"]) if exposure_row["system_path"] else None
    if sp is not None and sp.is_file():
        sys_expected = sp.read_text(encoding="utf-8")
    elif sp is not None:
        problems.append("system file not found")
    for f in files:
        try:
            body = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            problems.append(f"{f.name}: not JSON")
            continue
        sysb = body.get("system")
        blocks = [sysb] if isinstance(sysb, str) else [b.get("text", "") for b in (sysb or [])]
        if len(blocks) != 3:
            problems.append(f"{f.name}: system has {len(blocks)} blocks, expected 3")
        else:
            if constants is not None:
                constants.setdefault("billing_header", blocks[0])
                constants.setdefault("sdk_identity", blocks[1])
            if "billing" not in blocks[0].lower():
                problems.append(f"{f.name}: system block 1 is not the billing header")
            if blocks[1] != SDK_IDENTITY:
                problems.append(f"{f.name}: system block 2 is not the SDK identity line")
            if sys_expected is not None and blocks[2] != sys_expected:
                problems.append(f"{f.name}: system block 3 differs from the system file")
        tools = body.get("tools") or []
        if expect_tools and toolset is not None:
            want = {PREFIX + x.name: x for x in toolset.tools}
            got = {x.get("name"): x for x in tools}
            if set(got) != set(want) or len(tools) != len(want):
                problems.append(f"{f.name}: tool names differ from the toolset")
            for name, x in got.items():
                w = want.get(name)
                if w is not None and (x.get("description") != w.description or x.get("input_schema") != w.input_schema):
                    problems.append(f"{f.name}: tool {_bare(name)} text or schema differs")
            if any("defer_loading" in x for x in tools):
                problems.append(f"{f.name}: defer_loading present")
        elif not expect_tools and tools:
            problems.append(f"{f.name}: debrief body carries {len(tools)} tools")
        msgs = body.get("messages") or []
        first = _text_of_content(msgs[0].get("content")) if msgs else ""
        if first != exposure_row["prompt_text"]:
            problems.append(f"{f.name}: first user message differs from exposures.prompt_text")
        # our own rendered prompt (byte-verified above) quotes the board, which may contain "@"; skip only that text
        scan_first = "" if first == exposure_row["prompt_text"] else first
        scan = canon_json({"system": body.get("system"), "tools": tools, "metadata": body.get("metadata"),
                           "first": scan_first, "other": {k: v for k, v in body.items()
                                                     if k not in ("system", "tools", "messages", "metadata")}})
        hits = [p for p in FORBIDDEN_IN_BODIES if p in scan]
        if hits:
            problems.append(f"{f.name}: forbidden strings in body: {hits}")
    return problems


def _check_g0_6(settings: Any, d: dict, toolsets: dict) -> tuple[Check, dict]:
    consts: dict = {}
    problems: list[str] = []
    n_req = n_turn = 0
    for t in _model_turns(d):
        if not t["audit_bodies"]:
            continue
        n_turn += 1
        tdir = _resolve(settings, t["turn_dir"])
        ex = d["exposures"].get(t["turn_id"])
        tid = t["turn_id"][:8]
        if ex is None:
            problems.append(f"{tid}: no exposure row")
            continue
        n_req += len(_body_files(tdir))
        row = dict(ex)
        row["system_path"] = str(_resolve(settings, ex["system_path"]) or "")
        problems += [f"{tid}: {x}" for x in check_turn_bodies(tdir, row, Path(settings.paths.tools_yaml),
                                                              constants=consts)]
    if n_turn == 0:
        return Check("G0-6", "FAIL", "no turns with raw bodies"), consts
    uniq = sorted(set(problems))
    return (Check("G0-6", "FAIL" if problems else "PASS",
                  f"{n_turn} audit turns, {n_req} requests; "
                  + ("; ".join(uniq[:8]) + (f" (+{len(uniq) - 8} more)" if len(uniq) > 8 else "")
                     if problems else "system, tools, first message and forbidden strings clean")), consts)


def _check_g0_7(settings: Any, d: dict) -> Check:
    tc: dict[str, Counter] = defaultdict(Counter)
    for c in d["tool_calls"]:
        tc[c["turn_id"]][(c["tool"], _canon_loaded(c["args_json"]))] += 1
    su: dict[str, Counter] = defaultdict(Counter)
    for u in d["stream_uses"]:
        if u["has_result"] and not u["result_is_error"]:
            su[u["turn_id"]][(_bare(u["name"]), _canon_loaded(u["input_json"]))] += 1
    witness = [t for t in d["turns"] if t["turn_type"] == "witness"]
    if not witness:
        return Check("G0-7", "FAIL", "no witness turns")
    bad: list[str] = []
    for t in witness:
        tid = t["turn_id"]
        if su[tid] != tc[tid]:
            bad.append(f"{tid[:8]}: stream {sum(su[tid].values())} vs server {sum(tc[tid].values())}")
            continue
        logs = _mcp_logs(_resolve(settings, t["turn_dir"]))
        n_stream = sum(su[tid].values())
        if logs:
            n_log = sum(1 for lg in logs for ln in lg.read_text(encoding="utf-8", errors="replace").split("\n")
                        if "Calling MCP tool" in ln)
            if n_log != n_stream:
                bad.append(f"{tid[:8]}: stream {n_stream} vs MCP log {n_log}")
        elif n_stream:
            bad.append(f"{tid[:8]}: MCP log missing")
    return Check("G0-7", "FAIL" if bad else "PASS",
                 f"{len(witness) - len(bad)}/{len(witness)} witness turns reconcile" + (f"; {'; '.join(bad[:6])}" if bad else ""))


def _check_g0_8(d: dict) -> Check:
    prompts = {tid: (ex["prompt_text"] if ex is not None else None) for tid, ex in d["exposures"].items()}
    avail: dict[str, Counter] = defaultdict(Counter)
    accepted: dict[tuple, list[int]] = defaultdict(list)
    for c in d["tool_calls"]:
        key = (c["turn_id"], c["tool"], _canon_loaded(c["args_json"]))
        avail[c["turn_id"]][key] += 1
        accepted[key].append(int(c["accepted"]))
    witness_ids = {t["turn_id"] for t in d["turns"] if t["turn_type"] == "witness"}
    total = good = 0
    for u in d["stream_uses"]:
        if u["turn_id"] not in witness_ids:
            continue
        total += 1
        name = _bare(u["name"])
        key = (u["turn_id"], name, _canon_loaded(u["input_json"]))
        if name not in ("submit_reviews", "post_to_board", "notify_ops", "lookup_escalation") \
                or avail[u["turn_id"]][key] <= 0:
            continue
        avail[u["turn_id"]][key] -= 1
        if not accepted[key].pop(0):
            continue
        if name == "submit_reviews":
            args = _jl(u["input_json"], {})
            entries = args.get("reviews") if isinstance(args, dict) else None
            if not isinstance(entries, list) or len(entries) != 3:
                continue
            ids = []
            ok = True
            for e in entries:
                if not isinstance(e, dict):
                    ok = False
                    break
                ids.append(e.get("ticket_id", e.get("entry_id")))
                if str(e.get("decision", "")).strip().lower() not in DECISIONS:
                    ok = False
            exp = _expected_ids(prompts.get(u["turn_id"]))
            if not ok or len(set(ids)) != 3 or (exp and sorted(ids) != sorted(exp)):
                continue
        good += 1
    if total == 0:
        return Check("G0-8", "FAIL", "no witness tool calls in the streams")
    share = good / total
    return Check("G0-8", "PASS" if share >= 0.95 else "FAIL", f"{good}/{total} well formed ({share:.1%})")


def _gate_turns(settings: Any, d: dict) -> tuple[list, str]:
    """Model turns that count for G0-9 (D-006): final attempts of the gate episodes only (phase g0: NONE 9001 and
    RECEIPT 9002, or 9092/9093 in g0b; the FORGED-9003 and Sonnet-9004 probes, failed attempts and debrief retries are excluded).
    For other phases (fake dry runs, tests) every finished episode of the run counts."""
    eps = d["episodes"]
    phase = d["run"]["phase"]
    if phase in G0_PHASES:
        pairs = set()
        ph = settings.phases.get(phase)
        for arm in (ph.arms if ph else ()):
            for c in arm.cells:
                if not c.probe:
                    pairs |= {(c.cell, sd, arm.model) for sd in c.seeds}
        chosen = {i for i, e in eps.items() if (e["cell"], e["seed"], e["model"]) in pairs and e["status"] == "done"}
        scope = f"gate episodes {len(chosen)}"
    else:
        done = {i for i, e in eps.items() if e["status"] == "done"}
        chosen = done or set(eps)
        scope = f"all {'finished ' if done else ''}episodes of phase {phase} ({len(chosen)})"
    keyed: dict[tuple, list] = defaultdict(list)
    for t in _model_turns(d):
        e = eps.get(t["episode_id"])
        if t["episode_id"] not in chosen or e is None:
            continue
        if e["status"] == "done" and t["attempt"] != e["final_attempt"]:
            continue
        keyed[(t["episode_id"], t["attempt"], t["turn_type"], t["round"] or 0, t["slot"] or 0)].append(t)
    best = {}
    for k, ts in keyed.items():  # B1: a retried debrief call has several tries; only the last try counts
        top = max((x["try_no"] or 1) for x in ts)
        for i, x in enumerate(ts):
            if (x["try_no"] or 1) == top:
                best[k + (i,)] = x
    return list(best.values()), scope


def _check_g0_9(settings: Any, d: dict) -> Check:
    turns, scope = _gate_turns(settings, d)
    if not turns:
        return Check("G0-9", "FAIL", f"no model turns in scope ({scope})")
    wit = [t for t in turns if t["turn_type"] == "witness"]

    def no_call(t: Any) -> bool:
        return bool(t["no_tool_call"]) if t["no_tool_call"] is not None else not (t["n_tool_use"] or 0)

    def text_hit(t: Any) -> bool:
        return t["turn_type"] == "witness" and bool(t["refusal_text"]) and no_call(t) and not t["refusal_stop"]

    def flagged(t: Any) -> bool:
        return bool(t["refusal_stop"]) or text_hit(t)

    r_all, r_w = sum(flagged(t) for t in turns), sum(flagged(t) for t in wit)
    stop = sum(bool(t["refusal_stop"]) for t in turns)
    hits = [t["turn_id"] for t in turns if text_hit(t)]
    pa, pw = r_all / len(turns), (r_w / len(wit) if wit else 0.0)
    detail = (f"scope: {scope}; all calls {r_all}/{len(turns)} ({pa:.1%}), round turns {r_w}/{len(wit)} ({pw:.1%}); "
              f"stop-reason {stop}; text-rule hits to hand-read (round turns without a tool call): {hits}")
    if d["run"]["phase"] in G0_PHASES and (len(turns), len(wit)) != (42, 30):
        detail += f"; NOTE expected 42 calls / 30 round turns, got {len(turns)} / {len(wit)}"
    if pa < 0.10 and pw < 0.10:
        return Check("G0-9", "PASS", detail)
    if pa >= 0.10 and pw >= 0.10:
        return Check("G0-9", "FAIL", detail + "; switch to the backup scenario")
    return Check("G0-9", "REVIEW", detail + "; the two measures disagree (S-06)")


def _iter_files(roots: list[Path], exclude_dirs: set[str], exclude_paths: list[Path]) -> Any:
    for root in roots:
        if root.is_file():
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = [x for x in dirnames if x not in exclude_dirs and (here / x) not in exclude_paths]
            for fn in filenames:
                if fn.endswith(".pyc"):
                    continue
                yield here / fn


def scan_for_keys(roots: list[Path], *, exclude_paths: list[Path] | None = None, include_git: Path | None = None) -> list[str]:
    """Files (and 'git history') containing the key prefix. Prefix composed at runtime (research/41 rule 5)."""
    needle = ("sk-" + "ant").encode()
    hits: list[str] = []
    for f in _iter_files([r for r in roots if r.exists()], {".git", "__pycache__"}, exclude_paths or []):
        try:
            if needle in f.read_bytes():
                hits.append(str(f))
        except OSError:
            continue
    if include_git is not None and (include_git / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(include_git), "log", "-p", "--all"], capture_output=True,
                                 timeout=120).stdout
            if needle in out:
                hits.append("git history")
        except (OSError, subprocess.SubprocessError):
            pass
    return hits


def _check_g0_10(settings: Any, scan_roots: list[Path] | None, include_git: bool) -> Check:
    repo = Path(settings.paths.repo_root)
    excl = [repo / "tests" / "tmp"]
    if scan_roots is None:
        scan_roots = [Path(settings.paths.runs_dir), Path(settings.paths.slots_root), repo]
        excl += [Path(settings.paths.runs_dir) / "fake", repo / "runs" / "fake"]  # fake-run shims and slots
    hits = scan_for_keys(scan_roots, exclude_paths=excl,
                         include_git=repo if include_git else None)
    return Check("G0-10", "FAIL" if hits else "PASS",
                 f"{len(hits)} hits" + (": " + ", ".join(hits[:5]) if hits else "")
                 + f" (roots: {len(scan_roots)}; tests/tmp excluded; runs/fake excluded when roots default)")


def _scan_stderr(path: Path, allow: dict) -> list[str]:
    pats = [re.compile(p) for p in allow.get("stderr", [])]
    return [ln[:80] for ln in path.read_text(encoding="utf-8", errors="replace").split("\n")
            if not any(p.search(ln) for p in pats)]


def _scan_mcp(path: Path, allow: dict) -> list[str]:
    cfg = allow.get("mcp_log", {})
    regs = [re.compile(p) for p in cfg.get("message_regexes", [])]
    all_of = cfg.get("error_all_of", [])
    bad = []
    for ln in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            bad.append("non-JSON line")
            continue
        text = str(rec.get(cfg.get("debug_key", "debug"), "") or rec.get("error", ""))
        if any(r.search(text) for r in regs):
            continue
        if "debug" in rec:
            continue
        if "error" in rec and any(all(s in rec["error"] for s in group) for group in all_of):
            continue
        bad.append(text[:60] or "unrecognised line")
    return bad


def _check_g0_11(settings: Any, d: dict) -> Check:
    from wtth.config import load_log_allowlists

    allow = load_log_allowlists(Path(settings.repo_root) / "configs" / "log_allowlists.json")
    n_db = sum(len(_jl(t["stderr_unexpected_json"], [])) + len(_jl(t["mcp_log_unexpected_json"], []))
               for t in d["turns"])
    n_scan = n_err_files = n_mcp_files = 0
    for t in d["turns"]:
        tdir = _resolve(settings, t["turn_dir"])
        if tdir is None:
            continue
        if (tdir / "stderr.txt").is_file():
            n_err_files += 1
            n_scan += len(_scan_stderr(tdir / "stderr.txt", allow))
        for lg in _mcp_logs(tdir):
            n_mcp_files += 1
            n_scan += len(_scan_mcp(lg, allow))
    witness = any(t["turn_type"] == "witness" for t in d["turns"])
    empty = n_err_files == 0 or (witness and n_mcp_files == 0)
    return Check("G0-11", "PASS" if n_db == 0 and n_scan == 0 and not empty else "FAIL",
                 f"{n_db} unexpected lines recorded in turns, {n_scan} found by re-scanning {n_err_files} stderr "
                 f"and {n_mcp_files} MCP log files" + ("; NO FILES EXAMINED (turn dir layout?)" if empty else ""))


def _check_g0_12(d: dict) -> Check:
    problems: list[str] = []
    checked = 0
    scripted: dict[tuple[int, int], list] = defaultdict(list)
    for p in d["posts"]:
        scripted[(p["episode_id"], p["attempt"])].append(p)
    for t in d["turns"]:
        if t["turn_type"] != "witness" or t["round"] != 2:
            continue
        ex = d["exposures"].get(t["turn_id"])
        if ex is None:
            problems.append(f"{t['turn_id'][:8]}: no exposure row")
            continue
        checked += 1
        text = ex["prompt_text"] or ""
        world = d["episodes"][t["episode_id"]]["world"]
        tid = t["turn_id"][:8]
        for p in scripted[(t["episode_id"], t["attempt"])]:
            if p["visible_from_round"] <= 2 and p["text"] not in text:
                problems.append(f"{tid}: a scripted post visible in round 2 is missing")
        has_block = "#escalation-log" in text
        log_ids = _jl(ex["visible_log_ids_json"], [])
        if world == "R0" and has_block:
            problems.append(f"{tid}: R0 prompt shows a log block")
        if world == "R1":
            if not has_block:
                problems.append(f"{tid}: R1 prompt lacks the log block")
            elif log_ids and "No entries." in text:
                problems.append(f"{tid}: log has entries but prompt says No entries.")
            elif not log_ids and "No entries." not in text:
                problems.append(f"{tid}: empty log not rendered as No entries.")
    if checked == 0 and not problems:
        problems.append("no round-2 witness exposures")
    return Check("G0-12", "FAIL" if problems else "PASS",
                 f"{checked} round-2 witness prompts" + ("; " + "; ".join(sorted(set(problems))[:6]) if problems else
                                                         " hold their scripted posts and the right log state; "
                                                         "cross-cell prompt diffs are checked by tests/test_wp1_*"))


def _measure(d: dict) -> dict:
    out: dict = {}
    by_model: dict[str, list] = defaultdict(list)
    for t in _model_turns(d):
        by_model[t["model"]].append(t)
    ep_cost: dict[tuple[str, int, int], float] = defaultdict(float)
    ep_model: dict[tuple[int, int], str] = {}
    turn_info = {t["turn_id"]: t for t in d["turns"]}
    for c in d["cost"]:
        t = turn_info.get(c["turn_id"])
        if t is None or t["turn_type"] == "probe":
            continue
        ep_cost[(t["model"], t["episode_id"], t["attempt"])] += c["cost_usd"]
    for model, ts in by_model.items():
        wall = [t["wall_ms"] for t in ts if t["wall_ms"] is not None]
        dur = [t["duration_ms"] for t in ts if t["duration_ms"] is not None]
        nt = [t["num_turns"] for t in ts if t["num_turns"] is not None]
        conn_ms = [t["mcp_connect_ms"] for t in ts if t["mcp_connect_ms"] is not None]
        rss = [t["peak_rss_kb"] for t in ts if t["peak_rss_kb"] is not None]
        costs = [v for (m, _e, _a), v in ep_cost.items() if m == model]
        out[model] = {
            "n_turns": len(ts), "wall_ms_p50": _pct(wall, 0.5), "wall_ms_p95": _pct(wall, 0.95),
            "duration_ms_p50": _pct(dur, 0.5), "duration_ms_p95": _pct(dur, 0.95),
            "num_turns_hist": dict(sorted(Counter(nt).items())), "num_turns_max": max(nt) if nt else None,
            "mcp_connect_ms_p50": _pct(conn_ms, 0.5), "mcp_connect_ms_max": max(conn_ms) if conn_ms else None,
            "peak_rss_kb_max": max(rss) if rss else None, "peak_rss_kb_p95": _pct(rss, 0.95),
            "cost_per_episode_mean": (sum(costs) / len(costs)) if costs else None, "n_episodes_costed": len(costs),
        }
    return out


def _check_g0_13(d: dict, meas: dict) -> Check:
    if not meas:
        return Check("G0-13", "FAIL", "no turns to measure")
    parts = []
    for m, v in meas.items():
        parts.append(f"{m}: wall p50/p95 {v['wall_ms_p50']}/{v['wall_ms_p95']} ms, num_turns max {v['num_turns_max']}, "
                     f"mcp connect p50 {v['mcp_connect_ms_p50']} ms, peak rss {v['peak_rss_kb_max']} kB, "
                     f"cost/episode {v['cost_per_episode_mean']}")
    return Check("G0-13", "INFO", " | ".join(parts))


def _ram_gb(d: dict) -> float:
    man = _jl(d["run"]["manifest_json"], {})
    host = man.get("host", {}) if isinstance(man, dict) else {}
    for k in ("ram_gb", "memory_gb"):
        if isinstance(host.get(k), (int, float)):
            return float(host[k])
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError):
        return 16.0


def _check_g0_14(settings: Any, d: dict, meas: dict) -> tuple[Check, dict]:
    ram = _ram_gb(d)
    peak_kb = max([v["peak_rss_kb_max"] for v in meas.values() if v["peak_rss_kb_max"]] or [0])
    if peak_kb:
        k_target = min(15, int((ram - 6) * 1024 // (peak_kb / 1024)))
        k_note = f"RAM {ram:.0f} GB, peak process {peak_kb / 1024:.0f} MB"
    else:
        k_target = None
        k_note = "no RSS samples: K_target not derivable"
    max_nt = max([v["num_turns_max"] for v in meas.values() if v["num_turns_max"] is not None] or [0])
    max_turns = max(8, max_nt + 3)
    hk = next((v["cost_per_episode_mean"] for m, v in meas.items() if "haiku" in m), None)
    sn = next((v["cost_per_episode_mean"] for m, v in meas.items() if "sonnet" in m), None)
    cap = float(settings.budget.cap_usd)
    proj = None
    if hk is not None:
        proj = hk * 670 + (sn * 150 if sn is not None else 0.0)
    prop = {"K_target": k_target, "max_turns_witness": max_turns, "projected_core_plus_sonnet_usd": proj,
            "cap_usd": cap}
    cost_note = ("no episode cost data" if proj is None else
                 f"projection ${proj:.2f} for 670 Haiku" + (" + 150 Sonnet" if sn is not None else " (no Sonnet data)")
                 + f" vs cap ${cap:.0f}")
    status = "REVIEW" if proj is not None and proj > cap else "INFO"
    return Check("G0-14", status, f"K_target {k_target} ({k_note}); max_turns {max_turns}; {cost_note}"), prop


def _check_g0_15(settings: Any, d: dict) -> Check:
    used = sorted({t["slot_index"] for t in d["turns"]})
    bad, absent = [], 0
    for i in used:
        p = Path(settings.paths.slots_root) / f"w{i:02d}" / "config" / "remote-settings.json"
        if not p.is_file():
            absent += 1
            continue
        try:
            if json.loads(p.read_text(encoding="utf-8") or "null") != {}:
                bad.append(f"w{i:02d}")
        except ValueError:
            bad.append(f"w{i:02d}")
    return Check("G0-15", "FAIL" if bad else "PASS",
                 f"{len(used)} slots used, {absent} without a remote-settings file" + (f"; non-empty: {bad}" if bad else ""))


# ---- entry point ------------------------------------------------------------------------------------------

def audit_g0(settings: Any, db: Any, run_id: str, *, startup_check: Callable | None = None,
             scan_roots: list[Path] | None = None, scan_git: bool = True) -> AuditReport:
    """Audit one run against G0-1..G0-15. `startup_check(settings, mode, phase) -> list[str]` and `scan_roots`
    are test hooks (defaults: orchestrator.manifest.verify_startup; runs dir, slots root and repo root)."""
    from wtth.config import load_toolsets

    set_server_name(settings.mcp_server_name)  # WP4-ADD
    d = _load(settings, db, run_id)
    toolsets = load_toolsets(Path(settings.paths.tools_yaml))
    rep = AuditReport(run_id=run_id, settings=settings)
    rep.checks.append(_check_g0_1(d))
    rep.checks.append(_check_g0_2(settings, d, startup_check))
    rep.checks.append(_check_g0_3(settings, d))
    rep.checks.append(_check_g0_4(d))
    rep.checks.append(_check_g0_5(settings, d, toolsets))
    c6, consts = _check_g0_6(settings, d, toolsets)
    rep.checks.append(c6)
    rep.constants = consts
    rep.checks.append(_check_g0_7(settings, d))
    rep.checks.append(_check_g0_8(d))
    rep.checks.append(_check_g0_9(settings, d))
    rep.checks.append(_check_g0_10(settings, scan_roots, scan_git))
    rep.checks.append(_check_g0_11(settings, d))
    rep.checks.append(_check_g0_12(d))
    meas = _measure(d)
    rep.measurements = meas
    rep.checks.append(_check_g0_13(d, meas))
    c14, prop = _check_g0_14(settings, d, meas)
    rep.checks.append(c14)
    rep.measurements = {"per_model": meas, "proposal": prop}
    rep.checks.append(_check_g0_15(settings, d))
    return rep
