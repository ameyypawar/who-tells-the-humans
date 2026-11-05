"""Judge database (plan 47 section 2.1). Standard library only at import time (orchestrator/context.py imports it)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

JUDGE_SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema_judge.sql")
# Tables whose rows may be updated only while OLD.ended_at IS NULL.
OPEN_UPDATE_TABLES = ("calls", "judge_runs")
FREE_TABLES = ("jmeta",)


def judge_db_path(settings: Any) -> Path:
    return Path(settings.paths.runs_dir) / "judge" / "judge.sqlite"


def _triggers(conn: sqlite3.Connection) -> list[str]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    out: list[str] = []
    for t in tables:
        out.append(f"CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, 'wtth-judge: {t} rows are never deleted'); END")
        if t in FREE_TABLES:
            continue
        if t in OPEN_UPDATE_TABLES:
            out.append(f"CREATE TRIGGER trg_{t}_closed_no_update BEFORE UPDATE ON {t} WHEN OLD.ended_at IS NOT NULL "
                       f"BEGIN SELECT RAISE(ABORT, 'wtth-judge: closed {t} row is immutable'); END")
        else:
            out.append(f"CREATE TRIGGER trg_{t}_no_update BEFORE UPDATE ON {t} "
                       f"BEGIN SELECT RAISE(ABORT, 'wtth-judge: {t} rows are immutable'); END")
    return out


def create_judge_db(path: Path) -> None:
    """Create the judge database (temp file renamed into place, WAL, user_version). Idempotent."""
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        conn = sqlite3.connect(str(path))
        try:
            has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone()
            if has:
                v = conn.execute("PRAGMA user_version").fetchone()[0]
                if v != JUDGE_SCHEMA_VERSION:
                    raise RuntimeError(f"judge user_version {v} != {JUDGE_SCHEMA_VERSION}")
                return
        finally:
            conn.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".creating")
    for suffix in ("", "-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(str(tmp), isolation_level=None)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for stmt in _triggers(conn):
            conn.execute(stmt)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"could not enable WAL (got {mode})")
        conn.execute(f"PRAGMA user_version={JUDGE_SCHEMA_VERSION}")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    os.replace(tmp, path)


def open_judge_db(settings: Any, *, create: bool = False):
    """A wtth.db.Db on the judge database (created first when `create`)."""
    from wtth.db import Db

    path = judge_db_path(settings)
    if create:
        create_judge_db(path)
    return Db(path)


def judge_spent(settings: Any) -> float:
    """SUM(judge_ledger.cost_usd); 0.0 if the file does not exist."""
    path = judge_db_path(settings)
    if not path.exists():
        return 0.0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return 0.0
    try:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd),0.0) FROM judge_ledger").fetchone()
        return float(row[0])
    except sqlite3.Error:
        return 0.0
    finally:
        conn.close()
