"""Judge prompt (plan 47 section 2.4): built from the hash-checked rubric with the A-36 omissions."""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wtth.judge.labels import parse_held_out_expected
from wtth.judge.types import ItemType
from wtth.textnorm import sha256_text
from wtth.types import PostRow

FORMAT_LINE = ("Format: one JSON object with the keys id, labels, quote, confidence, uncertain, justification; "
               "labels holds only the labels of this item's type.")
REVISIONS_DIR = Path(__file__).with_name("revisions")
_RUBRIC_KEY = "prompts/judge_rubric.md"
_RETURN = "Return nothing else."
_BOARD_MARK = "Board so far"

_PH_LABELS = "{section 3 labels, copied unchanged}"
_PH_EXAMPLES = "{the examples of section 4, copied unchanged}"
_PH_RB = "{RB-BASE}"
_PH_BOARD = "{board lines up to the round of the item}"
_PH_TYPE = "{board_post | note | final_text | d1_answer | d2_answer}"
_PH_ID = "{item id}"
_PH_TEXT = "{item text}"


@dataclass(frozen=True, slots=True)
class JudgePrompt:
    version: str
    system_text: str
    system_sha256: str
    user_template: str
    user_template_sha256: str
    rubric_sha256: str


def _rubric_path(settings: Any) -> Path:
    return Path(settings.paths.prereg_dir) / "prompts" / "judge_rubric.md"


def _fill_once(template: str, mapping: dict[str, str]) -> str:
    """One re.sub pass over the escaped literal placeholders; each must occur exactly once. Model text is never
    re-scanned."""
    for ph in mapping:
        if template.count(ph) != 1:
            raise ValueError(f"placeholder {ph!r} occurs {template.count(ph)} times in the template")
    pat = re.compile("|".join(re.escape(p) for p in mapping))
    return pat.sub(lambda m: mapping[m.group(0)], template)


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start) + len(start)
    j = text.index(end, i)
    return text[i:j].strip()


def _remove_once(text: str, pattern: str, *, flags: int = 0) -> str:
    found = re.findall(pattern, text, flags)
    if len(found) != 1:
        raise ValueError(f"A-36 omission matched {len(found)} times (expected 1): {pattern[:40]!r}")
    return re.sub(pattern, "", text, count=1, flags=flags)


def _apply_omissions(labels: str, examples: str) -> tuple[str, str]:
    labels = _remove_once(labels, re.escape(" for P2′") + r"(?=\n)")           # 1: heading of A.
    labels = _remove_once(labels, re.escape(
        "6. Posts by scripted agents are never coded. Only witness posts are.") + r"\n?")  # 2: rule A6
    labels = _remove_once(labels, r" ?The harness, not the judge.*?manipulation\.", flags=re.DOTALL)  # 3: S1
    labels = _remove_once(labels, r" ?Used for the PRIMARY outcome.*?\(Updated 2026-10-30:[^)]*\)",
                          flags=re.DOTALL)                                            # 4: S2
    examples = _remove_once(examples, re.escape(
        "Synthetic witness posts. The expected labels are the ones the judge is asked to return.") + r"\n*")  # 5
    return labels.strip(), examples.strip()


def _rubric_sha_ok(settings: Any, path: Path) -> str:
    from wtth.config import CONFIGS_DIR, load_prereg_pins
    from wtth.prereg import sha256_file

    pinned = load_prereg_pins(CONFIGS_DIR / "prereg_hashes.json")["files"][_RUBRIC_KEY]
    got = sha256_file(path)
    if got != pinned:
        raise RuntimeError(f"judge_rubric.md sha256 {got} != pinned {pinned}")
    return got


def build_system_prompt(settings: Any, *, scenario: str = "main") -> JudgePrompt:
    """The v1 prompt. Two builds are byte-identical."""
    if scenario == "backup":
        raise NotImplementedError("S-05: the rubric needs adapting before it can judge the backup scenario")
    from wtth import prereg

    path = _rubric_path(settings)
    rubric_sha = _rubric_sha_ok(settings, path)
    text = path.read_text(encoding="utf-8")
    blocks = prereg.fenced_blocks(path)
    template = prereg.block_at(blocks, "6 Judge prompt template", 0)
    labels = _section(text, "## 3 Labels\n", "## 4 Examples (given to the judge)")
    examples = _section(text, "## 4 Examples (given to the judge)\n", "## 5 Held-out validation items")
    labels, examples = _apply_omissions(labels, examples)
    rb = prereg.load_scenario_texts(scenario, settings).runbook_base["RB-BASE"]

    if template.count(_RETURN) != 1:
        raise ValueError("template: 'Return nothing else.' not unique")
    template = template.replace(_RETURN, _RETURN + "\n" + FORMAT_LINE, 1)
    marker = "\n" + _BOARD_MARK + "\n"
    if template.count(marker) != 1:
        raise ValueError("template: 'Board so far' line not unique")
    head, tail = template.split(marker, 1)
    user_template = _BOARD_MARK + "\n" + tail
    system = _fill_once(head, {_PH_LABELS: labels, _PH_EXAMPLES: examples, _PH_RB: rb}).rstrip()
    return JudgePrompt("v1", system, sha256_text(system), user_template, sha256_text(user_template), rubric_sha)


def load_revision(settings: Any, version: str) -> JudgePrompt:
    """v1 is rebuilt; v2 is the lead-written revisions/system_v2.txt with v1's user template."""
    base = build_system_prompt(settings)
    if version == "v1":
        return base
    if version != "v2":
        raise ValueError(f"unknown prompt version {version!r}")
    p = REVISIONS_DIR / "system_v2.txt"
    if not p.is_file():
        raise FileNotFoundError(f"{p} does not exist (written by the lead after a failed validation)")
    system = p.read_text(encoding="utf-8").rstrip()
    return JudgePrompt("v2", system, sha256_text(system), base.user_template, base.user_template_sha256,
                       base.rubric_sha256)


def write_system_file(settings: Any, prompt: JudgePrompt) -> Path:
    """runs/judge/prompts/system_<v>.txt, mode 0444. Rewriting different content is refused."""
    d = Path(settings.paths.runs_dir) / "judge" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"system_{prompt.version}.txt"
    if path.exists():
        if sha256_text(path.read_text(encoding="utf-8")) != prompt.system_sha256:
            raise RuntimeError(f"{path} exists with different content")
        return path
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(prompt.system_text, encoding="utf-8")
    os.chmod(tmp, 0o444)
    os.replace(tmp, path)
    return path


def user_message(prompt: JudgePrompt, *, item_type: ItemType, item_id: str, item_text: str,
                 board_lines: str) -> str:
    return _fill_once(prompt.user_template, {
        _PH_BOARD: board_lines, _PH_TYPE: str(ItemType(item_type).value), _PH_ID: item_id, _PH_TEXT: item_text})


def held_out_specs(settings: Any) -> list[tuple[str, str, dict[str, Any]]]:
    """(key, post text, expected) parsed from the rubric section 5 table."""
    text = _rubric_path(settings).read_text(encoding="utf-8")
    sec = _section(text, "## 5 Held-out validation items", "## 6 Judge prompt template")
    out = []
    for line in sec.splitlines():
        m = re.match(r"^\| (H\d+) \| (.*) \| ([^|]*) \|$", line)
        if m:
            out.append((m.group(1), m.group(2), parse_held_out_expected(m.group(3))))
    if len(out) != 12:
        raise ValueError(f"expected 12 held-out rows, found {len(out)}")
    return out


def board_context(posts: Sequence[PostRow], *, upto_round: int | None, exclude_post_id: int | None) -> str:
    from wtth import render

    sel = [p for p in posts if (upto_round is None or p.round <= upto_round)
           and (exclude_post_id is None or p.id != exclude_post_id)]
    return render.board_lines(sel)


def held_out_board(settings: Any, level: str) -> str:
    """Round-3 board of public scripted lines (CLAIM, seed 1: paraphrase 0 = the public C2.P1)."""
    from wtth import prereg, stimuli

    texts = prereg.load_scenario_texts("main", settings)
    spec = stimuli.episode_spec("heldout", "main", settings.models.judge, settings.cells["CLAIM"], level, 1, 0,
                                settings.scenarios["main"].handle_prefix)
    rows: list[PostRow] = []
    n = 0
    for r in (1, 2, 3):
        for sp in stimuli.scripted_posts(spec, texts, r):
            if sp.is_private:
                raise RuntimeError("held-out board would contain a private line")
            n += 1
            rows.append(PostRow(n, sp.round, sp.sender, None, sp.clock, stimuli.clock_min(sp.clock), "scripted",
                                sp.line_id, sp.text, stimuli.scripted_visible_from(r)))
    return board_context(rows, upto_round=3, exclude_post_id=None)
