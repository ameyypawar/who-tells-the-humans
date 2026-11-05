"""events-table logging that never swallows a failure silently. Owner: WP4."""
from __future__ import annotations

import sys
import time
from typing import Any

from wtth.textnorm import canon_json


def log_event(db: Any, run_id: str, level: str, kind: str, *, episode_id: int | None = None,
              attempt: int | None = None, turn_id: str | None = None, detail: Any = None,
              _retry: bool = True) -> bool:
    """Insert an events row. On failure: print to stderr and (once) try to record 'log_event_failed'.
    Returns True when the row was written."""
    def fn(c: Any) -> None:
        c.execute("INSERT INTO events(ts, run_id, level, kind, episode_id, attempt, turn_id, detail_json) "
                  "VALUES (?,?,?,?,?,?,?,?)", (time.time(), run_id, level, kind, episode_id, attempt, turn_id,
                                               canon_json(detail) if detail is not None else None))
    try:
        db.write(fn)
        return True
    except Exception as exc:
        print(f"wtth: events row {kind!r} not written: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if _retry:
            log_event(db, run_id, "error", "log_event_failed", detail={"kind": kind, "error": str(exc)[:200]},
                      _retry=False)
        return False
