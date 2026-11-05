"""PREREG/prompt parsers, hash checks, scenario texts, private-file loading. Owner: WP1 (stub from WP0)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Block:
    heading: str
    index: int
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


def fenced_blocks(path: Path) -> list[Block]:
    """Text between a line ```text and the next line ```, joined with \\n, no trailing newline. Owner: WP1."""
    raise NotImplementedError("WP1")


def line_block(text: str) -> dict[str, str]:
    """Split each row on the first ' :: '; id = left.strip(). Owner: WP1."""
    raise NotImplementedError("WP1")


def parse_section23(prereg_md: Path) -> dict[str, str]:
    raise NotImplementedError("WP1")


def sha256_file(path: Path) -> str:
    raise NotImplementedError("WP1")


def verify_hashes(prereg_dir: Path, private_dir: Path, expected: dict[str, str]) -> list[str]:
    raise NotImplementedError("WP1")


def load_scenario_texts(scenario: str, settings: Any) -> ScenarioTexts:
    raise NotImplementedError("WP1")


def load_private(settings: Any) -> dict[str, str]:
    """ids C2.P2, C2.P3, FORGED_LINE, BK.C2.P2, BK.C2.P3, BK.FORGED_LINE. Owner: WP1."""
    raise NotImplementedError("WP1")


def mask_private(text: str, private: dict[str, str]) -> str:
    """Replace each private string with {{PRIVATE:<id>}}. Owner: WP1."""
    raise NotImplementedError("WP1")
