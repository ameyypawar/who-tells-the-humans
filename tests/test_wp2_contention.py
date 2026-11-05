"""WP2 T2.3: 15 OS processes x 200 notify_ops (R1) plus a 16th holding BEGIN IMMEDIATE for 3 s."""
from __future__ import annotations

import json
import subprocess
import time
import unittest
from pathlib import Path

from tests.test_wp2_common import TOOLS_YAML, make_world
from tests.util import PYTHON, REPO

N_PROC, N_CALLS, HOLD_S = 15, 200, 3.0


class Contention(unittest.TestCase):
    def test_15x200_with_lock_holder(self):
        path, db, ep = make_world("wp2_contention", "R1")
        self.assertEqual(ep, 1)
        d = path.parent
        faults = d / "faults"
        faults.mkdir()
        start, ready = d / "start", d / "holder_ready"
        t0 = time.monotonic()
        holder = subprocess.Popen([PYTHON, "-I", str(REPO / "tests" / "wp2_holder.py"), str(path), str(HOLD_S), str(ready)])
        while not ready.exists():
            time.sleep(0.01)
            self.assertLess(time.monotonic() - t0, 30)
        workers = [subprocess.Popen(
            [PYTHON, "-I", str(REPO / "tests" / "wp2_worker.py"), str(path), str(faults), str(TOOLS_YAML), str(i + 1),
             str(N_CALLS), str(start)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(N_PROC)]
        time.sleep(0.5)
        start.write_text("go")  # workers start while the holder still owns the write lock
        outs = [w.communicate(timeout=120) for w in workers]
        self.assertEqual(holder.wait(timeout=30), 0)
        wall = time.monotonic() - t0
        for w, (o, e) in zip(workers, outs):
            self.assertEqual(w.returncode, 0, e[-500:])
            r = json.loads(o)
            self.assertEqual((r["bad_replies"], r["tripped"]), (0, False), r)
        n_notes = db.read(lambda c: c.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        n_log = db.read(lambda c: c.execute("SELECT COUNT(*) FROM escalation_log").fetchone()[0])
        n_tc = db.read(lambda c: c.execute("SELECT COUNT(*) FROM tool_calls WHERE accepted=1").fetchone()[0])
        db.close()
        self.assertEqual((n_notes, n_log, n_tc), (N_PROC * N_CALLS,) * 3)
        self.assertEqual(list(faults.glob("*")), [])
        self.assertLess(wall, 60)
        self.assertGreaterEqual(wall, HOLD_S)  # the holder really blocked the start
        print(f"\ncontention wall time: {wall:.2f}s", flush=True)


if __name__ == "__main__":
    unittest.main()
