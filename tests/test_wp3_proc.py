"""T3.3: process handling with helper scripts (no real claude)."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.wp3_common import PYTHON, make_slot, tmp_dir, write_script
from wtth.runner import proc as P

HANG = "import time\nprint('up', flush=True)\ntime.sleep(300)\n"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    out = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


class RunProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir("wp3_proc")
        self.out, self.err = self.d / "out.txt", self.d / "err.txt"

    def run_it(self, script: Path, *, timeout_s=10.0, gi=0.5, gt=0.5, env=None, cancel_after=None, **kw):
        async def go():
            cancel = asyncio.Event() if cancel_after is not None else None
            if cancel is not None:
                asyncio.get_running_loop().call_later(cancel_after, cancel.set)
            return await P.run_process([str(script)], env or {"PATH": "/usr/bin:/bin"}, self.d, self.out, self.err,
                                       timeout_s, gi, gt, cancel=cancel, **kw)
        return asyncio.run(go())

    def test_normal_exit_and_files(self):
        s = write_script(self.d, "ok.py", "import sys\nprint('hello out')\nprint('hello err', file=sys.stderr)\n")
        r = self.run_it(s)
        self.assertEqual((r.exit_code, r.term_signal, r.timed_out, r.kill_stage), (0, None, False, None))
        self.assertEqual(self.out.read_text(), "hello out\n")
        self.assertEqual(self.err.read_text(), "hello err\n")
        self.assertGreaterEqual(r.wall_ms, 0)
        self.assertGreater(r.pgid, 0)
        self.assertEqual(r.stdout_path, self.out)

    def test_exit_codes(self):
        s = write_script(self.d, "x143.py", "import sys\nsys.exit(143)\n")
        r = self.run_it(s)
        self.assertEqual((r.exit_code, r.term_signal, r.timed_out), (143, None, False))
        s = write_script(self.d, "sig.py", "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\nimport time; time.sleep(5)\n")
        r = self.run_it(s)
        self.assertEqual((r.exit_code, r.term_signal, r.timed_out), (None, signal.SIGTERM, False))

    def test_hang_ends_at_sigint(self):
        s = write_script(self.d, "hang.py", HANG)
        t0 = time.monotonic()
        r = self.run_it(s, timeout_s=0.5)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(r.timed_out)
        self.assertEqual(r.kill_stage, "sigint")
        self.assertIn(r.term_signal, (signal.SIGINT, None))
        self.assertFalse(alive(r.pgid))

    def test_ignoring_int_goes_to_sigterm(self):
        s = write_script(self.d, "int.py", "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\nprint('up', flush=True)\ntime.sleep(300)\n")
        r = self.run_it(s, timeout_s=0.6, gi=0.3, gt=2.0)
        self.assertTrue(r.timed_out)
        self.assertEqual(r.kill_stage, "sigterm")
        self.assertEqual(r.term_signal, signal.SIGTERM)

    def test_ignoring_int_and_term_reaches_sigkill(self):
        s = write_script(self.d, "stub.py", (
            "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('up', flush=True)\ntime.sleep(300)\n"))
        r = self.run_it(s, timeout_s=0.6, gi=0.3, gt=0.3)
        self.assertTrue(r.timed_out)
        self.assertEqual(r.kill_stage, "sigkill")
        self.assertEqual(r.term_signal, signal.SIGKILL)
        self.assertFalse(alive(r.pgid))

    def _grandchild_script(self, pidfile: Path, parent_hangs: bool) -> Path:
        tail = "time.sleep(300)\n" if parent_hangs else "print('parent done')\n"
        return write_script(self.d, f"gc_{parent_hangs}.py", (
            "import subprocess, sys, time\n"
            f"c = subprocess.Popen([{PYTHON!r}, '-I', '-c', 'import time; time.sleep(300)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
            "print('spawned', flush=True)\n" + tail))

    def test_grandchild_dies_on_timeout(self):
        pidfile = self.d / "gc.pid"
        r = self.run_it(self._grandchild_script(pidfile, True), timeout_s=1.0)
        self.assertTrue(r.timed_out)
        gc = int(pidfile.read_text())
        self.assertTrue(wait_dead(gc), "grandchild survived the group kill")

    def test_straggler_after_normal_exit_is_killed(self):
        pidfile = self.d / "gc2.pid"
        r = self.run_it(self._grandchild_script(pidfile, False), timeout_s=10.0)
        self.assertEqual(r.exit_code, 0)
        self.assertFalse(r.timed_out)
        self.assertTrue(str(r.kill_stage).startswith("straggler_"), r.kill_stage)
        self.assertTrue(wait_dead(int(pidfile.read_text())), "straggler survived")

    def test_cancel_kills_and_is_not_timeout(self):
        s = write_script(self.d, "hang2.py", HANG)
        t0 = time.monotonic()
        r = self.run_it(s, timeout_s=30.0, cancel_after=0.3)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertFalse(r.timed_out)
        self.assertEqual(r.kill_stage, "sigint")

    def test_cancel_after_exit_does_not_matter(self):
        s = write_script(self.d, "quick.py", "print('x')\n")
        r = self.run_it(s, cancel_after=60)
        self.assertEqual((r.exit_code, r.kill_stage), (0, None))

    def test_env_exact_and_stdin_devnull_and_new_session(self):
        s = write_script(self.d, "probe.py", (
            "import os, stat, json\n"
            "print(json.dumps({'env': sorted(os.environ), 'chr': stat.S_ISCHR(os.fstat(0).st_mode),\n"
            " 'sid': os.getsid(0) == os.getpid(), 'pg': os.getpgrp() == os.getpid(), 'cwd': os.getcwd()}))\n"))
        os.environ["WTTH_PARENT_ONLY"] = "1"
        try:
            self.run_it(s, env={"PATH": "/usr/bin:/bin", "FOO": "bar"})
        finally:
            del os.environ["WTTH_PARENT_ONLY"]
        import json
        got = json.loads(self.out.read_text())
        self.assertNotIn("WTTH_PARENT_ONLY", got["env"])
        self.assertIn("FOO", got["env"])
        self.assertTrue(got["chr"] and got["sid"] and got["pg"])
        self.assertEqual(os.path.realpath(got["cwd"]), os.path.realpath(self.d))

    def test_rss_sampler(self):
        s = write_script(self.d, "slp.py", "import time\ntime.sleep(1.2)\n")
        r = self.run_it(s, rss_sampler=True)
        self.assertIsNotNone(r.peak_rss_kb)
        self.assertGreater(r.peak_rss_kb, 1000)
        self.assertIsNone(self.run_it(s).peak_rss_kb)

    def test_no_zombies(self):
        code = (
            "import asyncio, os, sys\n"
            "from pathlib import Path\n"
            "from wtth.runner import proc as P\n"
            "d = Path(sys.argv[1])\n"
            "async def main():\n"
            "    for name in ('ok', 'hang', 'x143'):\n"
            "        s = d / (name + '.py')\n"
            "        await P.run_process([str(s)], {'PATH': '/usr/bin:/bin'}, d, d / 'o', d / 'e',\n"
            "                            0.4 if name == 'hang' else 5.0, 0.3, 0.3)\n"
            "asyncio.run(main())\n"
            "try:\n"
            "    os.waitpid(-1, os.WNOHANG)\n"
            "except ChildProcessError:\n"
            "    print('NO_CHILDREN')\n"
            "else:\n"
            "    print('CHILD_LEFT')\n")
        write_script(self.d, "ok.py", "print('x')\n")
        write_script(self.d, "hang.py", HANG)
        write_script(self.d, "x143.py", "import sys\nsys.exit(143)\n")
        r = subprocess.run([PYTHON, "-I", "-c", code, str(self.d)], capture_output=True, text=True, timeout=60)
        self.assertIn("NO_CHILDREN", r.stdout, r.stdout + r.stderr)


class KillGroupTest(unittest.TestCase):
    def test_already_gone(self):
        p = subprocess.Popen([PYTHON, "-I", "-c", "pass"], start_new_session=True)
        p.wait()
        self.assertEqual(P.kill_group(p.pid, 0.1, 0.1), "none")


class OrphanSweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir("wp3_orphan")
        self.runs = self.d / "runs"
        self.other = self.d / "other_runs"
        self.binary = self.d / "bin" / "claude-fake"
        self.procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def spawn(self, *extra: str, session: bool = True) -> subprocess.Popen:
        p = subprocess.Popen([PYTHON, "-I", "-c", "import time; time.sleep(120)", *extra],
                             start_new_session=session, stdin=subprocess.DEVNULL)
        self.procs.append(p)
        time.sleep(0.2)
        return p

    def test_kills_only_matching_orphans(self):
        cli = self.spawn(str(self.binary), "-p", "x", "--system-prompt-file", f"{self.runs}/stimuli/sys.txt")
        srv = self.spawn("-m", "wtth.mcp_server", "--runs-root", str(self.runs))
        live = self.spawn(str(self.binary), f"{self.runs}/stimuli/live.txt")
        bin_only = self.spawn(str(self.binary), "--version")
        other_root = self.spawn(str(self.binary), f"{self.other}/stimuli/x.txt")
        other_srv = self.spawn("-m", "wtth.mcp_server", "--runs-root", str(self.other))
        prefix_root = self.spawn("-m", "wtth.mcp_server", "--runs-root", str(self.runs) + "_more")
        killed = P.orphan_sweep(self.binary, self.runs, {live.pid})
        self.assertEqual(sorted(killed), sorted([cli.pid, srv.pid]))
        for p in (cli, srv):
            p.wait(timeout=10)
            self.assertIsNotNone(p.returncode)
        for p in (live, bin_only, other_root, other_srv, prefix_root):
            self.assertIsNone(p.poll(), f"pid {p.pid} should survive")

    def test_nothing_to_kill(self):
        self.assertEqual(P.orphan_sweep(self.binary, self.runs, set()), [])

    def test_sigterm_ignoring_orphan_gets_sigkill(self):
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"
        p = subprocess.Popen([PYTHON, "-I", "-c", code, str(self.binary), f"{self.runs}/stimuli/s.txt"],
                             start_new_session=True)
        self.procs.append(p)
        time.sleep(0.3)
        t0 = time.monotonic()
        self.assertEqual(P.orphan_sweep(self.binary, self.runs, set()), [p.pid])
        p.wait(timeout=10)
        self.assertLess(time.monotonic() - t0, 8)
        self.assertEqual(p.returncode, -signal.SIGKILL)

    def test_never_kills_self_or_own_group(self):
        # a same-group process matching the patterns (started without a new session) is left alone
        p = self.spawn(str(self.binary), f"{self.runs}/stimuli/g.txt", session=False)
        self.assertEqual(P.orphan_sweep(self.binary, self.runs, set()), [])
        self.assertIsNone(p.poll())


class HarvestTest(unittest.TestCase):
    def test_moves_only_new_logs(self):
        d = tmp_dir("wp3_harvest")
        slot = make_slot(d)
        cwd = "/private/tmp/ws/run"
        src = slot.home / "Library" / "Caches" / "claude-cli-nodejs" / "-private-tmp-ws-run" / "mcp-logs-workspace"
        src.mkdir(parents=True)
        old, new = src / "old.jsonl", src / "new.jsonl"
        old.write_text("old\n")
        new.write_text("new\n")
        t = time.time()
        os.utime(old, (t - 100, t - 100))
        dest = d / "turn" / "mcp"
        got = P.harvest_mcp_log(slot, cwd, dest, t - 50)
        self.assertEqual(got, [dest / "new.jsonl"])
        self.assertEqual((dest / "new.jsonl").read_text(), "new\n")
        self.assertFalse(new.exists())
        self.assertTrue(old.exists())

    def test_missing_dir(self):
        d = tmp_dir("wp3_harvest2")
        self.assertEqual(P.harvest_mcp_log(make_slot(d), "/private/tmp/ws/run", d / "x", 0.0), [])


class SamplingTest(unittest.TestCase):
    def test_rss_of_own_group(self):
        p = subprocess.Popen([PYTHON, "-I", "-c", "import time; time.sleep(5)"], start_new_session=True)
        try:
            time.sleep(0.2)
            got = P.sample_rss_kb([p.pid, 999999])
            self.assertGreater(got.get(p.pid, 0), 1000)
            self.assertNotIn(999999, got)
            self.assertEqual(P.sample_rss_kb([]), {})
        finally:
            p.kill()
            p.wait()

    def test_pressure_level(self):
        self.assertIn(P.memory_pressure_level(), (1, 2, 4))


if __name__ == "__main__":
    unittest.main()
