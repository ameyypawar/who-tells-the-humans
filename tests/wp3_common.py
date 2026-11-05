"""WP3 test helpers: settings, requests, helper-script writers. Dummy keys only; no literal key prefix."""
from __future__ import annotations

import stat
import textwrap
from pathlib import Path

from tests.util import DUMMY_KEY, PYTHON, REPO, tmp_dir  # noqa: F401
from wtth.config import load_expected_init, load_log_allowlists, load_settings, load_toolsets
from wtth.textnorm import sha256_text
from wtth.types import Identity, RenderedPrompt, SlotDirs, TurnRequest, TurnType, World

SETTINGS = load_settings()
TOOLSETS = load_toolsets(SETTINGS.paths.tools_yaml)
EXPECTED_INIT = load_expected_init(REPO / "configs" / "expected_init.json")
ALLOW = load_log_allowlists(REPO / "configs" / "log_allowlists.json")


def make_slot(root: Path, index: int = 0) -> SlotDirs:
    r = root / f"w{index:02d}"
    home, cfg, tmp = r / "home", r / "config", r / "tmp"
    for d in (home, cfg, tmp):
        d.mkdir(parents=True, exist_ok=True)
    return SlotDirs(index=index, root=r, home=home, config=cfg, tmp=tmp)


def make_identity(turn_id: str = "a" * 32, *, episode_id: int = 1, attempt: int = 1, round: int = 2, slot: int = 1,
                  toolset: str = "main", world: World = World.R1) -> Identity:
    return Identity(run_id="run-1", episode_id=episode_id, attempt=attempt, round=round, slot=slot,
                    sender="rd-14", clock="21:04", clock_min=1264, visible_from_round=round + 1, world=world,
                    source="tool", turn_id=turn_id, toolset=toolset,
                    ticket_ids=("T-1201", "T-1202", "T-1203"))


def make_req(turn_dir: Path, *, turn_type: TurnType = TurnType.WITNESS, turn_id: str = "a" * 32,
             model: str = "claude-haiku-5-5", toolset: str = "main", prompt: str = "PROMPT TEXT {x} \"q\"",
             timeout_s: float = 30.0, audit: bool = False, episode_id: int = 1, attempt: int = 1,
             slot: int | None = 1, handle: str | None = "rd-14", round: int | None = 2) -> TurnRequest:
    debrief = turn_type in (TurnType.D1, TurnType.D2)
    ts = None if debrief else TOOLSETS[toolset]
    rp = RenderedPrompt(text=prompt, sha256=sha256_text(prompt), visible_post_ids=(1, 2), visible_note_ids=(3,),
                        visible_log_ids=())
    ident = None if debrief else make_identity(turn_id, episode_id=episode_id, attempt=attempt, toolset=toolset)
    return TurnRequest(
        turn_id=turn_id, episode_id=episode_id, attempt=attempt, turn_type=turn_type, round=round, slot=slot,
        handle=handle, model=model, system_path=turn_dir / "system.txt", system_sha="s" * 64, prompt=rp,
        toolset=ts, identity=ident, max_turns=2 if debrief else 8,
        max_budget_usd=0.02 if model.endswith("haiku-5-5") else 0.10, timeout_s=timeout_s, audit_bodies=audit,
        settings_path=Path(SETTINGS.paths.cc_settings), turn_dir=turn_dir)


def write_script(directory: Path, name: str, body: str) -> Path:
    """Executable python helper script (run via the venv python with -I)."""
    p = directory / name
    p.write_text(f"#!{PYTHON} -I\n" + textwrap.dedent(body), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p
