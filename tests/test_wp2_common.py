"""Shared WP2 test helpers (no tests in this module)."""
from __future__ import annotations

import uuid
from pathlib import Path

from tests.util import REPO, seed_running_episode, make_db
from wtth.config import load_caps, load_toolsets
from wtth.types import Identity, World

TOOLS_YAML = REPO / "configs" / "tools.yaml"
TICKETS = ("T-1201", "T-1202", "T-1203")


def toolsets():
    return load_toolsets(TOOLS_YAML)


def caps():
    return load_caps(TOOLS_YAML)


def make_ident(episode_id: int, attempt: int = 1, *, world: World = World.R0, sender: str = "rd-14", slot: int = 1,
               rnd: int = 2, clock: str = "21:34", toolset: str = "main", turn_id: str | None = None,
               source: str = "tool") -> Identity:
    h, m = clock.split(":")
    return Identity(run_id="run-1", episode_id=episode_id, attempt=attempt, round=rnd, slot=slot, sender=sender,
                    clock=clock, clock_min=int(h) * 60 + int(m), visible_from_round=rnd + 1, world=world,
                    source=source, turn_id=turn_id if turn_id is not None or source == "script" else uuid.uuid4().hex,
                    toolset=toolset, ticket_ids=TICKETS)


def make_world(name: str, world: str = "R0"):
    """Temp DB with a running episode. Returns (path, db, episode_id)."""
    path, db = make_db(name)
    ep, _ = seed_running_episode(db, world=world)
    return path, db, ep


def server_env(db_path: Path, fault_dir: Path, episode_id: int, *, world: str = "R0", toolset: str = "main",
               sender: str = "rd-14", rnd: int = 2, slot: int = 1, clock: str = "21:34",
               turn_id: str | None = None, attempt: int = 1) -> dict[str, str]:
    ts = toolsets()[toolset]
    return {
        "WTTH_DB": str(db_path), "WTTH_RUN_ID": "run-1", "WTTH_EPISODE_ID": str(episode_id),
        "WTTH_ATTEMPT": str(attempt), "WTTH_ROUND": str(rnd), "WTTH_SLOT": str(slot), "WTTH_SENDER": sender,
        "WTTH_CLOCK": clock, "WTTH_VISIBLE_FROM": str(rnd + 1), "WTTH_WORLD": world,
        "WTTH_TURN_ID": turn_id or uuid.uuid4().hex, "WTTH_TOOLSET": toolset, "WTTH_TOOLS_PATH": str(TOOLS_YAML),
        "WTTH_TOOLS_SHA256": ts.sha256, "WTTH_TICKET_IDS": ",".join(TICKETS), "WTTH_FAULT_DIR": str(fault_dir),
        "WTTH_SCHEMA_VERSION": "1",
    }
