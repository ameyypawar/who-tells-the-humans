"""Worker for the T2.3 contention test: N notify_ops calls through handlers.dispatch. Prints a JSON summary."""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from wtth.config import load_caps, load_toolsets
from wtth.db import Db
from wtth.handlers import FaultSink, dispatch
from wtth.types import Identity, World


def main() -> None:
    db_path, fault_dir, tools, idx, n, start_file = sys.argv[1:7]
    idx, n = int(idx), int(n)
    ts = load_toolsets(Path(tools))["main"]
    caps = load_caps(Path(tools))
    turn = uuid.uuid4().hex
    ident = Identity("run-1", 1, 1, 2, 1 + idx % 3, f"rd-{idx:02d}", "21:34", 21 * 60 + 34, 3, World.R1, "tool", turn,
                     "main", ("T-1201", "T-1202", "T-1203"))
    db = Db(Path(db_path))
    fault = FaultSink(Path(fault_dir), turn)
    while not Path(start_file).exists():
        time.sleep(0.005)
    bad = 0
    for i in range(n):
        r = dispatch(db, ident, ts, "notify_ops", {"subject": f"w{idx} n{i}", "message": f"body {idx}/{i}"}, fault, caps)
        if r != "Sent to ops.":
            bad += 1
    db.close()
    print(json.dumps({"idx": idx, "bad_replies": bad, "tripped": fault.tripped}))


if __name__ == "__main__":
    main()
