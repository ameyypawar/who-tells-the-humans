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
    """<runs_dir>/<paths.judge_subdir>/judge.sqlite: runs/judge/judge.sqlite for the core and the Sonnet arm,
    runs/judge_exp1/judge.sqlite for the D-022 follow-up (configs/exp1.yaml), so exploratory labels never enter the
    database the confirmatory analysis reads."""
    sub = getattr(settings.paths, "judge_subdir", "judge") or "judge"
    return Path(settings.paths.runs_dir) / sub / "judge.sqlite"


def core_judge_db_path(settings: Any) -> Path:
    """The confirmatory judge database (runs/judge/judge.sqlite), whatever paths.judge_subdir says."""
    return Path(settings.paths.runs_dir) / "judge" / "judge.sqlite"


def judge_db_paths(settings: Any) -> list[Path]:
    """Every judge database under runs_dir (<runs>/judge*/judge.sqlite, the configured one included). They all
    count against the one global ledger (B5)."""
    runs = Path(settings.paths.runs_dir)
    found = {p for p in runs.glob("judge*/judge.sqlite") if p.is_file()}
    own = judge_db_path(settings)
    if own.is_file():
        found.add(own)
    return sorted(found)


def _ledger_usd(path: Path) -> float:
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


def judge_spent(settings: Any, *, exclude: Path | None = None) -> float:
    """SUM(judge_ledger.cost_usd) over every judge database under runs_dir (judge_db_paths); 0.0 if there is none.
    `exclude`: one database left out (the caller adds its own ledger itself)."""
    ex = Path(exclude).resolve() if exclude is not None else None
    return sum(_ledger_usd(p) for p in judge_db_paths(settings) if ex is None or p.resolve() != ex)


# ---- jmeta helpers (the table is free to update) -------------------------------------------------------------

META_DEFAULTS = {"active_prompt_version": "v1", "validation_passed": "0", "validation_level": "",
                 "blind_declined": "0", "analysis_yaml_sha256": ""}


def ensure_meta(jdb: Any) -> None:
    """Insert the default jmeta rows that are missing (idempotent)."""
    import time

    def fn(c: sqlite3.Connection) -> None:
        c.execute("INSERT OR IGNORE INTO jmeta(key, value) VALUES ('created_at', ?)", (repr(time.time()),))
        for k, v in META_DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO jmeta(key, value) VALUES (?, ?)", (k, v))

    jdb.write(fn)


def get_meta(jdb: Any, key: str, default: str | None = None) -> str | None:
    row = jdb.read(lambda c: c.execute("SELECT value FROM jmeta WHERE key=?", (key,)).fetchone())
    return row[0] if row else default


def set_meta(jdb: Any, key: str, value: str) -> None:
    jdb.write(lambda c: c.execute(
        "INSERT INTO jmeta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)))


def active_version(jdb: Any) -> str:
    return get_meta(jdb, "active_prompt_version", "v1") or "v1"


def log_event(jdb: Any, kind: str, *, level: str = "info", run_id: str | None = None, item_id: str | None = None,
              call_id: str | None = None, detail: dict | None = None) -> None:
    import json
    import time

    jdb.write(lambda c: c.execute(
        "INSERT INTO jevents(ts, run_id, level, kind, item_id, call_id, detail_json) VALUES (?,?,?,?,?,?,?)",
        (time.time(), run_id, level, kind, item_id, call_id,
         json.dumps(detail, sort_keys=True, default=str) if detail is not None else None)))
