"""Voided queue rows (D-019). The judge database is insert-only, so a queue row that must not count (the duplicate
H-5 redraw) is recorded in ann_queue_void and every consumer reads v_ann_queue instead of ann_queue.

ensure() creates the table, its no-update/no-delete triggers and the view in an existing database (idempotent)."""
from __future__ import annotations

import json
import time
from typing import Any

REASON = "duplicate H-5 draw on queue rebuild (D-019)"
VIEW = "v_ann_queue"

_DDL = (
    "CREATE TABLE IF NOT EXISTS ann_queue_void(ann_id TEXT PRIMARY KEY REFERENCES ann_queue(ann_id), "
    "reason TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS trg_ann_queue_void_no_delete BEFORE DELETE ON ann_queue_void "
    "BEGIN SELECT RAISE(ABORT, 'wtth-judge: ann_queue_void rows are never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_ann_queue_void_no_update BEFORE UPDATE ON ann_queue_void "
    "BEGIN SELECT RAISE(ABORT, 'wtth-judge: ann_queue_void rows are immutable'); END",
    f"CREATE VIEW IF NOT EXISTS {VIEW} AS SELECT q.* FROM ann_queue q "
    "WHERE q.ann_id NOT IN (SELECT ann_id FROM ann_queue_void)",
)


def ensure(jdb: Any) -> str:
    """Make sure the void table and view exist; returns the view name to use instead of ann_queue."""
    have = jdb.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('ann_queue_void', ?) "
        "OR name = 'trg_ann_queue_void_no_update'", (VIEW,)).fetchone()[0])
    if have < 3:
        def fn(c: Any) -> None:
            for stmt in _DDL:
                c.execute(stmt)
        jdb.write(fn)
    return VIEW


def void_h5_redraw(jdb: Any, *, expect: int | None = None) -> dict[str, Any]:
    """Void every H-5 row created after the first H-5 draw (the pre-registered sample is the first draw only).
    Idempotent. With expect, refuses (changes nothing) unless exactly that many rows would be voided or are voided."""
    ensure(jdb)
    rows = jdb.read(lambda c: c.execute(
        "SELECT ann_id, created_at FROM ann_queue WHERE queue='H-5' ORDER BY created_at, ann_id").fetchall())
    if not rows:
        return {"first_draw": 0, "voided_now": 0, "voided_total": 0}
    first_ts = rows[0][1]
    first = [r[0] for r in rows if r[1] == first_ts]
    later = [r[0] for r in rows if r[1] != first_ts]
    already = {r[0] for r in jdb.read(lambda c: c.execute("SELECT ann_id FROM ann_queue_void").fetchall())}
    todo = [a for a in later if a not in already]
    if expect is not None and len(later) != expect:
        raise ValueError(f"expected {expect} redraw rows, found {len(later)}; nothing changed")
    now = time.time()

    def fn(c: Any) -> None:
        for a in todo:
            c.execute("INSERT INTO ann_queue_void(ann_id, reason, created_at) VALUES (?,?,?)", (a, REASON, now))
        if todo:
            c.execute("INSERT INTO jevents(ts, run_id, level, kind, detail_json) VALUES (?,?,?,?,?)",
                      (now, None, "warn", "void_h5_redraw", json.dumps(
                          {"voided": len(todo), "first_draw_created_at": first_ts, "reason": REASON})))
    jdb.write(fn)
    return {"first_draw": len(first), "first_draw_created_at": first_ts, "voided_now": len(todo),
            "voided_total": len(later)}
