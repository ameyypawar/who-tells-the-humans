"""Paging: runs/ALERT.json, a local notification, an events row. Owner: WP4."""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from wtth.textnorm import canon_json


def page(settings: Any, db: Any, run_id: str, phase: str, reason: str, detail: dict, exit_code: int) -> None:
    """Write ALERT.json (overwritten), notify locally (osascript, no network), print STOP line, log an event.

    The notification is skipped in fake mode or when WTTH_NO_NOTIFY is set.
    """
    alert = {"ts": time.time(), "run_id": run_id, "phase": phase, "reason": reason, "detail": detail,
             "exit_code": exit_code}
    path = settings.paths.alert_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(alert, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, path)
    print(f"STOP: {reason}", flush=True)
    if not getattr(settings, "fake", False) and not os.environ.get("WTTH_NO_NOTIFY"):
        msg = f"wtth {phase}: {reason}".replace('"', "'")[:180]
        try:
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "wtth"'],
                           timeout=5, capture_output=True, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    if db is not None:
        def fn(c):
            c.execute("INSERT INTO events(ts, run_id, level, kind, detail_json) VALUES (?,?,?,?,?)",
                      (alert["ts"], run_id, "page", reason, canon_json(detail if _jsonable(detail) else str(detail))))
        try:
            db.write(fn)
        except Exception:  # a failing DB must not hide the alert file
            pass


def _jsonable(x: Any) -> bool:
    try:
        json.dumps(x)
        return True
    except (TypeError, ValueError):
        return False
