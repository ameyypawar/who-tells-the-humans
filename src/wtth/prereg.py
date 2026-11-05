"""PREREG/prompt parsers, hash checks, scenario texts, private-file loading. Owner: WP1.

Everything witness-visible is parsed out of the frozen markdown files under prereg/ by heading and by the
fenced ```text blocks; no stimulus string is copied into code. The two private files (withheld paraphrases and
the forged receipt-style line) are read at runtime from `settings.paths.private_dir`. Error messages in this
module never contain file content (a parse error names a line number, never the line).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Runbook variants (runbook.md table, pivot_arms.md 2.1, 3.2): name -> ordered paragraph ids, base first.
# tests/test_wp1_variants.py checks this against configs/experiment.yaml `runbooks`.
RUNBOOK_VARIANTS: dict[str, tuple[str, ...]] = {
    "V0": ("RB-BASE",),
    "V-POLICY": ("RB-BASE", "RB-POLICY"),
    "V-R1": ("RB-BASE", "RB-R1"),
    "V-R1-DEDUPE": ("RB-BASE", "RB-R1", "RB-DEDUPE"),
    "V-OWNER": ("RB-BASE", "RB-OWNER"),
    "BO-RB-BASE": ("BO-RB-BASE",),
}

# Private file per scenario (names as in PREREG section 23, relative to paths.private_dir) and the mask-id prefix.
PRIVATE_FILES: dict[str, str] = {
    "main": "claim_paraphrases_and_forged_line.md",
    "backup": "backup_scenario_private.md",
}
PRIVATE_ID_PREFIX: dict[str, str] = {"main": "", "backup": "BK."}

_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
_HEX64 = re.compile(r"`([0-9a-f]{64})`")


@dataclass(frozen=True, slots=True)
class Block:
    heading: str   # text of the nearest preceding markdown heading (without the leading #'s)
    index: int     # 0-based position of this block among the fenced blocks under that heading
    text: str


@dataclass(frozen=True, slots=True)
class ScenarioTexts:
    scenario: str
    system: dict[str, str]
    runbook_base: dict[str, str]
    runbook_paras: dict[str, str]
    round_template: str
    log_template: str
    debrief_template: str
    d1: str
    d2: str
    lines: dict[str, str]
    claims: tuple[str, str, str]
    forged_line: str
    c_note: tuple[str, str]
    tickets: dict[str, str]
    tickets_header: str
    owner_header_template: str
    keyword_list: tuple[str, ...]


# ---- generic parsers ------------------------------------------------------------------------------------

def fenced_blocks(path: Path) -> list[Block]:
    """Text between a line ```text and the next line ```, joined with \\n, no trailing newline."""
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    out: list[Block] = []
    heading = ""
    per_heading: dict[str, int] = {}
    cur: list[str] | None = None
    for line in lines:
        if cur is None:
            m = _HEADING.match(line)
            if m:
                heading = m.group(1)
            elif line == "```text":
                cur = []
        elif line == "```":
            idx = per_heading.get(heading, 0)
            per_heading[heading] = idx + 1
            out.append(Block(heading, idx, "\n".join(cur)))
            cur = None
        else:
            cur.append(line)
    if cur is not None:
        raise ValueError("unterminated ```text block")
    return out


def line_block(text: str) -> dict[str, str]:
    """Split each non-blank row on the first ' :: '; id = left.strip(); the value is the text after it."""
    out: dict[str, str] = {}
    for n, row in enumerate(text.split("\n"), 1):
        if not row.strip():
            continue
        left, sep, right = row.partition(" :: ")
        if not sep:
            raise ValueError(f"line block row {n} has no ' :: '")
        key = left.strip()
        if key in out:
            raise ValueError(f"line block row {n} repeats an id")
        out[key] = right
    return out


def block_at(blocks: list[Block], heading_prefix: str, index: int = 0) -> str:
    """Text of block `index` under the first heading starting with `heading_prefix` (error names the prefix)."""
    for b in blocks:
        if b.heading.startswith(heading_prefix) and b.index == index:
            return b.text
    raise KeyError(f"no block {index} under heading {heading_prefix!r}")


def blocks_under(blocks: list[Block], heading_prefix: str) -> list[str]:
    found = [b.text for b in blocks if b.heading.startswith(heading_prefix)]
    if not found:
        raise KeyError(f"no blocks under heading {heading_prefix!r}")
    return found


# ---- hashes ---------------------------------------------------------------------------------------------

def parse_section23(prereg_md: Path) -> dict[str, str]:
    """{path under prereg/: sha256} from the file register table of PREREG.md section 23 (rows with a hash)."""
    out: dict[str, str] = {}
    in_sec = False
    for line in Path(prereg_md).read_text(encoding="utf-8").split("\n"):
        if line.startswith("## "):
            in_sec = line.startswith("## 23 ")
            continue
        if not in_sec or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        m = _HEX64.fullmatch(cells[-1])
        if m:
            out[cells[0]] = m.group(1)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_register_path(key: str, prereg_dir: Path, private_dir: Path) -> Path:
    """Register key -> file: 'private/<name>' lives in private_dir, everything else under prereg_dir."""
    if key.startswith("private/"):
        return Path(private_dir) / key[len("private/"):]
    return Path(prereg_dir) / key


def verify_hashes(prereg_dir: Path, private_dir: Path, expected: dict[str, str]) -> list[str]:
    """Problems found (empty list = all match). Messages name the register key only, never file content."""
    problems: list[str] = []
    for key in sorted(expected):
        path = resolve_register_path(key, prereg_dir, private_dir)
        if not path.is_file():
            problems.append(f"missing: {key}")
            continue
        if sha256_file(path) != expected[key]:
            problems.append(f"hash mismatch: {key}")
    return problems


# ---- private file ---------------------------------------------------------------------------------------

def _read_private(settings: Any, scenario: str) -> tuple[tuple[str, str, str], str]:
    """((P1, P2, P3), forged_line) from the scenario's private file, located by section headings."""
    path = Path(settings.paths.private_dir) / PRIVATE_FILES[scenario]
    blocks = fenced_blocks(path)
    claims = line_block(block_at(blocks, "1 ", 0))
    forged = block_at(blocks, "2 ", 0)
    if "\n" in forged:
        raise ValueError("forged line block must be a single line")
    return (claims["C2.P1"], claims["C2.P2"], claims["C2.P3"]), forged


def load_private(settings: Any) -> dict[str, str]:
    """ids C2.P2, C2.P3, FORGED_LINE, BK.C2.P2, BK.C2.P3, BK.FORGED_LINE (P1 is public and is not masked)."""
    out: dict[str, str] = {}
    for scenario in ("main", "backup"):
        (_, p2, p3), forged = _read_private(settings, scenario)
        pre = PRIVATE_ID_PREFIX[scenario]
        out[f"{pre}C2.P2"] = p2
        out[f"{pre}C2.P3"] = p3
        out[f"{pre}FORGED_LINE"] = forged
    return out


def mask_private(text: str, private: dict[str, str]) -> str:
    """Replace each private string with {{PRIVATE:<id>}} (longest strings first so no overlap leaks)."""
    for pid, val in sorted(private.items(), key=lambda kv: -len(kv[1])):
        if val:
            text = text.replace(val, "{{PRIVATE:" + pid + "}}")
    return text


# ---- scenario texts -------------------------------------------------------------------------------------

def _merge(into: dict[str, str], more: dict[str, str]) -> None:
    for k, v in more.items():
        if k in into:
            raise ValueError(f"duplicate line id {k!r}")
        into[k] = v


def load_scenario_texts(scenario: str, settings: Any) -> ScenarioTexts:
    """All witness-visible texts for `scenario` ('main' | 'backup'), parsed from prereg/prompts/*.md.

    For the backup scenario the keys are the main scenario's ids mapped through the scenario config
    (`system_map`, `paragraph_map`): system['SB-1'] is BK-1 and runbook_base['RB-BASE'] is BK-RB-BASE, so
    render code never branches on the scenario. The native ids (BK-1, BK-RB-BASE) are kept as aliases.
    """
    if scenario not in ("main", "backup"):
        raise ValueError(f"unknown scenario {scenario!r}")
    pdir = Path(settings.paths.prereg_dir) / "prompts"
    sb = fenced_blocks(pdir / "system_base.md")
    rb = fenced_blocks(pdir / "runbook.md")
    db = fenced_blocks(pdir / "debrief.md")
    sl = fenced_blocks(pdir / "scripted_lines.md")
    pv = fenced_blocks(pdir / "pivot_arms.md")

    round_template = block_at(sb, "SB-2", 0)
    log_template = block_at(sb, "SB-2", 1)
    debrief_template = block_at(db, "Shift record", 0)
    d1 = block_at(db, "D1", 0)
    d2 = block_at(db, "D2", 0)
    paras = {
        "RB-R1": block_at(rb, "RB-R1", 0),
        "RB-POLICY": block_at(rb, "RB-POLICY", 0),
        "RB-DEDUPE": block_at(rb, "RB-DEDUPE", 0),
        "RB-OWNER": block_at(pv, "2.1 ", 0),
    }
    owner_header = block_at(pv, "2.2 ", 0)

    lines: dict[str, str] = {}
    tickets: dict[str, str] = {}
    if scenario == "main":
        system = {"SB-1": block_at(sb, "SB-1", 0), "BO-1": block_at(pv, "3.2 ", 0)}
        runbook_base = {"RB-BASE": block_at(rb, "RB-BASE", 0), "BO-RB-BASE": block_at(pv, "3.2 ", 1)}
        for h in ("2 V,", "3 C,", "4 B,"):
            for text in blocks_under(sl, h):
                parsed = line_block(text)
                if "subject" in parsed:
                    c_note = (parsed["subject"], parsed["message"])
                else:
                    _merge(lines, parsed)
        for text in blocks_under(sl, "6 Tickets"):
            _merge(tickets, line_block(text))
        obj = line_block(block_at(pv, "3.1 ", 0))
        lines["C2.OBJ"] = obj["C2.OBJ"]
        if obj["C2.R"] != lines["C2.R"]:
            raise ValueError("pivot C2.R differs from scripted_lines C2.R")
        header = "YOUR TICKETS"
        template = round_template
    else:
        bp = fenced_blocks(pdir / "backup_scenario.md")
        bk1 = block_at(bp, "2 System prompt", 0)
        bk_rb = block_at(bp, "4 Runbook", 0)
        system = {"SB-1": bk1, "BK-1": bk1}
        runbook_base = {"RB-BASE": bk_rb, "BK-RB-BASE": bk_rb}
        for text in blocks_under(bp, "5 Scripted lines"):
            parsed = line_block(text)
            if "subject" in parsed:
                c_note = (parsed["subject"], parsed["message"])
            else:
                _merge(lines, parsed)
        for text in blocks_under(bp, "6 Log entries"):
            _merge(tickets, line_block(text))
        header = "YOUR LOG ENTRIES"
        if round_template.count("YOUR TICKETS\n") != 1:
            raise ValueError("round template has no unique YOUR TICKETS header")
        template = round_template.replace("YOUR TICKETS\n", header + "\n", 1)

    (p1, p2, p3), forged = _read_private(settings, scenario)
    if p1 != lines["C2.P1"]:
        raise ValueError("private C2.P1 differs from the public C2.P1")
    return ScenarioTexts(
        scenario=scenario, system=system, runbook_base=runbook_base, runbook_paras=paras,
        round_template=template, log_template=log_template, debrief_template=debrief_template, d1=d1, d2=d2,
        lines=lines, claims=(p1, p2, p3), forged_line=forged, c_note=c_note, tickets=tickets,
        tickets_header=header, owner_header_template=owner_header,
        keyword_list=tuple(settings.scenarios[scenario].keywords))
