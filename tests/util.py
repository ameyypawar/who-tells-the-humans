"""Shared test helpers (research/41 section 3.1, WP0).

Temp files live under tests/tmp/<name>/ (gitignored). Nothing here touches runs/, ~/.local/wtth/slots or
/private/tmp/ws/run.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import sys
import time
import unittest
from pathlib import Path
from typing import Any

from wtth.db import Db, create_db

REPO = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO / "tests" / "tmp"
PYTHON = os.environ.get("WTTH_PYTHON") or sys.executable

# A dummy API key that is never a real credential. Composed at runtime so the literal prefix never
# appears in the repo (research/41 rule 5).
DUMMY_KEY = "sk-" + "ant-" + "TESTDUMMY0000"


def tmp_dir(name: str) -> Path:
    """Fresh empty directory tests/tmp/<name>."""
    d = TMP_ROOT / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def make_db(name: str) -> tuple[Path, Db]:
    """Fresh temp database tests/tmp/<name>/wtth.sqlite created by create_db; returns (path, Db)."""
    d = tmp_dir(name)
    path = d / "wtth.sqlite"
    create_db(path)
    return path, Db(path)


def write_key_file(directory: Path, key: str = DUMMY_KEY, mode: int = 0o600) -> Path:
    """Write ANTHROPIC_API_KEY=<key> to <directory>/anthropic.env with the given mode."""
    p = directory / "anthropic.env"
    p.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
    os.chmod(p, mode)
    return p


def write_shim(directory: Path, behavior_json: Path, fake_claude: Path | None = None) -> Path:
    """Executable shim <directory>/claude that execs the fake CLI with a behaviour file (research/41 3.2.1)."""
    fake = fake_claude or (REPO / "tests" / "fake_claude.py")
    shim = directory / "claude"
    shim.write_text(f'#!/bin/sh\nexec {PYTHON} -I {fake} --behavior {behavior_json} "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def assert_masked_equal(tc: unittest.TestCase, actual: str, expected: str, private: dict[str, str]) -> None:
    """Compare two texts after prereg.mask_private so a failure never prints a private string."""
    from wtth.prereg import mask_private

    a, e = mask_private(actual, private), mask_private(expected, private)
    if a != e:
        import difflib

        diff = "\n".join(difflib.unified_diff(e.splitlines(), a.splitlines(), "expected", "actual", lineterm=""))
        tc.fail("masked texts differ:\n" + diff)


# ---- row seeding (so tests need not repeat the long column lists) -----------------------------------------

def seed_run(conn: sqlite3.Connection, run_id: str = "run-1", phase: str = "core", mode: str = "fake") -> str:
    conn.execute("INSERT INTO runs(run_id, phase, mode, started_at, manifest_sha, manifest_json) "
                 "VALUES (?,?,?,?,?,?)", (run_id, phase, mode, time.time(), "0" * 64, "{}"))
    return run_id


def seed_episode(conn: sqlite3.Connection, key: str = "core/main/claude-haiku-5-5/NONE/L2/s1", *,
                 phase: str = "core", scenario: str = "main", model: str = "claude-haiku-5-5", cell: str = "NONE",
                 level: str = "L2", seed: int = 1, world: str = "R0", status: str = "queued",
                 queue_pos: int = 1) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO episodes(episode_key, phase, scenario, model, cell, level, seed, world, handle_idx, "
        "ticket_idx, paraphrase_idx, handles_json, ticket_sets_json, queue_pos, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,?)",
        (key, phase, scenario, model, cell, level, seed, world, '["rd-14","rd-21","rd-26"]', '["A","B","C"]',
         queue_pos, status, now, now))
    return cur.lastrowid


def seed_attempt(conn: sqlite3.Connection, episode_id: int, attempt: int = 1, run_id: str = "run-1",
                 status: str = "running") -> None:
    conn.execute("INSERT INTO attempts(episode_id, attempt, run_id, status, started_at) VALUES (?,?,?,?,?)",
                 (episode_id, attempt, run_id, status, time.time()))


def seed_running_episode(db: Db, **kw: Any) -> tuple[int, int]:
    """Insert a run, an episode (status running) and attempt 1 (running). Returns (episode_id, attempt)."""
    def fn(conn: sqlite3.Connection) -> int:
        seed_run(conn)
        ep = seed_episode(conn, status="running", **kw)
        seed_attempt(conn, ep, 1)
        return ep
    return db.write(fn), 1


def seed_turn(conn: sqlite3.Connection, turn_id: str, episode_id: int, attempt: int = 1, *,
              turn_type: str = "witness", round: int | None = 1, slot: int | None = 1, handle: str | None = "rd-14",
              try_no: int = 1, cls: str | None = "ok", model: str = "claude-haiku-5-5", run_id: str = "run-1") -> None:
    conn.execute(
        "INSERT INTO turns(turn_id, run_id, episode_id, attempt, turn_type, round, slot, handle, model, slot_index, "
        "turn_dir, argv_redacted_json, env_names_json, audit_bodies, launched_at, class, try_no) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,'d','[]','[]',0,?,?,?)",
        (turn_id, run_id, episode_id, attempt, turn_type, round, slot, handle, model, time.time(), cls, try_no))
