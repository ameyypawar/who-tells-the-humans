"""Holds BEGIN IMMEDIATE on a database for N seconds, signalling readiness through a file."""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

db_path, seconds, ready = sys.argv[1], float(sys.argv[2]), sys.argv[3]
conn = sqlite3.connect(db_path, isolation_level=None)
conn.execute("BEGIN IMMEDIATE")
Path(ready).write_text("1")
time.sleep(seconds)
conn.execute("COMMIT")
conn.close()
