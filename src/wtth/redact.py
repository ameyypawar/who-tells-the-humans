"""Release redaction. Owner: WP5.

build_release(settings, src_runs, dest) writes a release bundle in which every withheld (private) string is
replaced by `{{PRIVATE:<id>}}`, in its raw form and in both JSON-escaped forms (ensure_ascii True and False).
The source runs/ directory is only read: SQLite files are copied aside first and never opened in place, so
runs/ stays byte-identical. Private strings are never logged, printed or placed in exceptions.

SQLite: the copy has its triggers dropped (the release is a read-only artefact), every text value is rewritten,
secure_delete + VACUUM leave no old bytes in free pages, and journal_mode=DELETE leaves a single file.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

SKIP_NAMES = {"wtth.lock"}
SKIP_SUFFIXES = (".sqlite-wal", ".sqlite-shm", ".sqlite-journal")
SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")


class RedactionError(RuntimeError):
    """A private string survived redaction (nothing is left behind) or the destination is unusable."""


def _variants(value: str) -> list[str]:
    """Raw form plus the JSON-escaped forms (ASCII-escaped and UTF-8), distinct, longest first."""
    forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    return sorted((f for f in forms if f), key=len, reverse=True)


class _Redactor:
    def __init__(self, private: dict[str, str]) -> None:
        self.private = {k: v for k, v in private.items() if v}
        pairs = [(pid, v) for pid, val in self.private.items() for v in _variants(val)]
        pairs.sort(key=lambda p: -len(p[1]))
        self.str_pairs = [(pid, v, "{{PRIVATE:" + pid + "}}") for pid, v in pairs]
        self.byte_pairs = [(pid, v.encode("utf-8"), ("{{PRIVATE:" + pid + "}}").encode("utf-8"))
                           for pid, v in pairs]
        self.counts: dict[str, int] = {pid: 0 for pid in self.private}

    def text(self, s: str) -> str:
        for pid, needle, repl in self.str_pairs:
            n = s.count(needle)
            if n:
                self.counts[pid] += n
                s = s.replace(needle, repl)
        return s

    def data(self, b: bytes) -> bytes:
        for pid, needle, repl in self.byte_pairs:
            n = b.count(needle)
            if n:
                self.counts[pid] += n
                b = b.replace(needle, repl)
        return b

    def residual(self, b: bytes) -> dict[str, int]:
        out: dict[str, int] = {}
        for pid, needle, _ in self.byte_pairs:
            n = b.count(needle)
            if n:
                out[pid] = out.get(pid, 0) + n
        return out


def _redact_sqlite(src: Path, dest: Path, red: _Redactor, scratch: Path) -> int:
    """Copy src (plus a -wal if present) into scratch, rewrite text values, vacuum, move to dest."""
    work = scratch / src.name
    shutil.copyfile(src, work)
    wal = src.with_name(src.name + "-wal")
    if wal.exists():
        shutil.copyfile(wal, work.with_name(work.name + "-wal"))
    conn = sqlite3.connect(work, isolation_level=None)
    changed = 0
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA secure_delete=ON")
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            conn.execute(f'DROP TRIGGER "{name}"')
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        needles = [n for _, n, _ in red.str_pairs]
        conn.execute("BEGIN")
        for t in tables:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
            for c in cols:
                cond = " OR ".join('instr("%s", ?) > 0' % c for _ in needles)
                if not cond:
                    continue
                rows = conn.execute(
                    f'SELECT rowid, "{c}" FROM "{t}" WHERE typeof("{c}")=\'text\' AND ({cond})', needles).fetchall()
                for rowid, val in rows:
                    conn.execute(f'UPDATE "{t}" SET "{c}"=? WHERE rowid=?', (red.text(val), rowid))
                    changed += 1
        conn.execute("COMMIT")
        conn.execute("VACUUM")
    finally:
        conn.close()
    for extra in (work.with_name(work.name + "-wal"), work.with_name(work.name + "-shm"),
                  work.with_name(work.name + "-journal")):
        extra.unlink(missing_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(work), str(dest))
    return changed


def build_release(settings: Any, src_runs: Path, dest: Path) -> dict:
    """Redacted copy of `src_runs` at `dest` (must not exist or be empty). Returns a summary dict:

    counts: replacements per private id; residual: occurrences left per id (always {} on success);
    files, sqlite_files, rows_rewritten, skipped. Raises RedactionError (after removing dest) on any residual.
    """
    from wtth.prereg import load_private

    src_runs, dest = Path(src_runs), Path(dest)
    if not src_runs.is_dir():
        raise RedactionError("source runs directory does not exist")
    if dest.exists() and any(dest.iterdir()):
        raise RedactionError("destination exists and is not empty")
    try:
        dest.resolve().relative_to(src_runs.resolve())
        raise RedactionError("destination must not be inside the source runs directory")
    except ValueError:
        pass
    red = _Redactor(load_private(settings))
    dest.mkdir(parents=True, exist_ok=True)
    scratch = Path(str(dest) + ".scratch")
    scratch.mkdir(parents=True, exist_ok=False)
    files = sqlite_files = rows = 0
    skipped: list[str] = []
    try:
        for p in sorted(src_runs.rglob("*")):
            rel = p.relative_to(src_runs)
            if p.is_dir() or p.is_symlink():
                if p.is_dir():
                    (dest / rel).mkdir(parents=True, exist_ok=True)
                continue
            if p.name in SKIP_NAMES or p.name.endswith(SKIP_SUFFIXES):
                skipped.append(str(rel))
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if p.name.endswith(SQLITE_SUFFIXES):
                rows += _redact_sqlite(p, out, red, scratch)
                sqlite_files += 1
            else:
                out.write_bytes(red.data(p.read_bytes()))
            files += 1
        shutil.rmtree(scratch, ignore_errors=True)
        residual: dict[str, int] = {}
        for p in dest.rglob("*"):
            if p.is_file():
                for pid, n in red.residual(p.read_bytes()).items():
                    residual[pid] = residual.get(pid, 0) + n
        if residual:
            raise RedactionError("private strings survived redaction for ids: " + ",".join(sorted(residual)))
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {"counts": dict(red.counts), "residual": {}, "files": files, "sqlite_files": sqlite_files,
            "rows_rewritten": rows, "skipped": skipped}
