"""SQLite access (research/41 section 2.4). Only create_db() runs DDL.

Every Db connection sets busy timeout, foreign keys and synchronous=NORMAL (not persistent in the file).
Write = BEGIN IMMEDIATE + fn + COMMIT with jittered retry. Reads use a separate autocommit connection.
"""
from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from wtth import SCHEMA_VERSION

T = TypeVar("T")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Tables that reject every UPDATE (rows are immutable once written).
NO_UPDATE_TABLES = (
    "posts", "tool_calls", "notes", "escalation_log", "reviews", "stream_tool_uses", "debrief_answers",
    "cost_ledger", "server_starts", "fallback_rows", "events", "gate_log",
)

_sleep = time.sleep  # patched by tests


class WriteFailed(Exception):
    """Db.write gave up after all retries. `error_class` is a short machine label."""

    def __init__(self, error_class: str, detail: str = "") -> None:
        super().__init__(f"{error_class}: {detail}" if detail else error_class)
        self.error_class = error_class
        self.detail = detail


class SchemaMismatch(RuntimeError):
    """PRAGMA user_version differs from wtth.SCHEMA_VERSION."""


def _error_class(exc: sqlite3.Error) -> str:
    msg = str(exc).lower()
    if "locked" in msg:
        return "sqlite_locked"
    if "busy" in msg:
        return "sqlite_busy"
    if "i/o" in msg or "disk" in msg:
        return "sqlite_io"
    return "sqlite_operational"


def _connect(path: Path, busy_timeout_s: float, *, query_only: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=busy_timeout_s, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    if query_only:
        conn.execute("PRAGMA query_only=ON")
    return conn


class Db:
    def __init__(self, path: Path, *, busy_timeout_s: float = 5.0) -> None:
        self.path = Path(path)
        self.busy_timeout_s = busy_timeout_s
        self._wconn: sqlite3.Connection | None = None
        self._rconn: sqlite3.Connection | None = None
        self._wlock = threading.RLock()
        self._rlock = threading.RLock()

    def _open(self, *, query_only: bool = False) -> sqlite3.Connection:
        if not self.path.exists():
            raise FileNotFoundError(f"database not found: {self.path} (run create_db first)")
        return _connect(self.path, self.busy_timeout_s, query_only=query_only)

    def write(self, fn: Callable[[sqlite3.Connection], T], *, attempts: int = 8, base_s: float = 0.05,
              cap_s: float = 2.0) -> T:
        """BEGIN IMMEDIATE -> fn(conn) -> COMMIT. Retries OperationalError with jittered backoff."""
        last: sqlite3.Error | None = None
        with self._wlock:
            for i in range(attempts):
                try:
                    if self._wconn is None:
                        self._wconn = self._open()
                    conn = self._wconn
                    conn.execute("BEGIN IMMEDIATE")
                    result = fn(conn)
                    conn.execute("COMMIT")
                    return result
                except sqlite3.OperationalError as exc:
                    last = exc
                    self._rollback_and_reset()
                    if i < attempts - 1:
                        _sleep(min(cap_s, base_s * (2 ** i)) + random.uniform(0, base_s))
                except BaseException:
                    self._rollback()
                    raise
        assert last is not None
        raise WriteFailed(_error_class(last), str(last))

    def _rollback(self) -> None:
        conn = self._wconn
        if conn is not None and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    def _rollback_and_reset(self) -> None:
        self._rollback()
        conn, self._wconn = self._wconn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._rlock:
            if self._rconn is None:
                self._rconn = self._open(query_only=True)
            return fn(self._rconn)

    def close(self) -> None:
        with self._wlock:
            if self._wconn is not None:
                self._wconn.close()
                self._wconn = None
        with self._rlock:
            if self._rconn is not None:
                self._rconn.close()
                self._rconn = None


def check_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise SchemaMismatch(f"user_version {version} != expected {SCHEMA_VERSION}")


def _generated_triggers(conn: sqlite3.Connection) -> list[str]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    stmts: list[str] = []
    for t in tables:
        stmts.append(f"CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} "
                     f"BEGIN SELECT RAISE(ABORT, 'wtth: {t} rows are never deleted'); END")
    for t in NO_UPDATE_TABLES:
        stmts.append(f"CREATE TRIGGER trg_{t}_no_update BEFORE UPDATE ON {t} "
                     f"BEGIN SELECT RAISE(ABORT, 'wtth: {t} rows are immutable'); END")
    # exposures: only request_ids_json, session_id, init_uuid may change, and only while NULL.
    stmts.append(
        "CREATE TRIGGER trg_exposures_guard BEFORE UPDATE ON exposures WHEN "
        "NEW.turn_id IS NOT OLD.turn_id OR NEW.system_path IS NOT OLD.system_path "
        "OR NEW.system_sha IS NOT OLD.system_sha OR NEW.tools_sha IS NOT OLD.tools_sha "
        "OR NEW.prompt_sha IS NOT OLD.prompt_sha OR NEW.prompt_text IS NOT OLD.prompt_text "
        "OR NEW.visible_post_ids_json IS NOT OLD.visible_post_ids_json "
        "OR NEW.visible_note_ids_json IS NOT OLD.visible_note_ids_json "
        "OR NEW.visible_log_ids_json IS NOT OLD.visible_log_ids_json "
        "OR NEW.checks_json IS NOT OLD.checks_json "
        "OR (OLD.request_ids_json IS NOT NULL AND NEW.request_ids_json IS NOT OLD.request_ids_json) "
        "OR (OLD.session_id IS NOT NULL AND NEW.session_id IS NOT OLD.session_id) "
        "OR (OLD.init_uuid IS NOT NULL AND NEW.init_uuid IS NOT OLD.init_uuid) "
        "BEGIN SELECT RAISE(ABORT, 'wtth: exposures are immutable'); END")
    # attempts: running -> done | failed | abandoned, and nothing reopens.
    stmts.append(
        "CREATE TRIGGER trg_attempts_status_guard BEFORE UPDATE OF status ON attempts "
        "WHEN OLD.status <> 'running' "
        "BEGIN SELECT RAISE(ABORT, 'wtth: a closed attempt cannot change status'); END")
    # episodes: done is terminal; failed -> anything needs a manual_requeue event; queued -> done is illegal.
    stmts.append(
        "CREATE TRIGGER trg_episodes_done_terminal BEFORE UPDATE ON episodes WHEN OLD.status = 'done' "
        "BEGIN SELECT RAISE(ABORT, 'wtth: a done episode is terminal'); END")
    stmts.append(
        "CREATE TRIGGER trg_episodes_failed_requeue BEFORE UPDATE OF status ON episodes "
        "WHEN OLD.status = 'failed' AND NEW.status <> 'failed' AND NOT EXISTS ("
        "SELECT 1 FROM events WHERE kind = 'manual_requeue' AND episode_id = OLD.episode_id AND ts >= OLD.updated_at) "
        "BEGIN SELECT RAISE(ABORT, 'wtth: failed episode needs a manual_requeue event'); END")
    stmts.append(
        "CREATE TRIGGER trg_episodes_queued_done BEFORE UPDATE OF status ON episodes "
        "WHEN OLD.status = 'queued' AND NEW.status = 'done' "
        "BEGIN SELECT RAISE(ABORT, 'wtth: queued episode cannot jump to done'); END")
    return stmts


def create_db(path: Path) -> None:
    """Create the database file: schema.sql, generated triggers, WAL, user_version. Idempotent.

    Built in a temp file and renamed into place, so a crash never leaves a half-built database.
    If `path` already holds a wtth database, only the schema version is checked.
    """
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        conn = _connect(path, 5.0)
        try:
            has_runs = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
            if has_runs:
                check_schema(conn)
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
        for stmt in _generated_triggers(conn):
            conn.execute(stmt)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"could not enable WAL (got {mode})")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    os.replace(tmp, path)


def backup(path: Path, dest: Path) -> None:
    """Online backup of `path` to `dest` (atomic rename)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(tmp, dest)
