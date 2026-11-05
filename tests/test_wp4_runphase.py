"""scripts/run_phase.sh with a stub tmux: environment scrubbing, exact session match, argument quoting."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.util import REPO, tmp_dir


class RunPhaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir(f"wp4_runphase_{os.getpid()}")
        self.bin = self.d / "bin"
        self.bin.mkdir()
        self.calls = self.d / "tmux.calls"
        (self.bin / "tmux").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TMUX_CALLS"\n'
            '[ "$1" = has-session ] && exit "${TMUX_HAS:-1}"\nexit 0\n')
        (self.bin / "tmux").chmod(0o755)
        # a copy of the repo scripts dir so runs/logs lands in the temp dir
        self.repo = self.d / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(REPO / "scripts" / "run_phase.sh", self.repo / "scripts" / "run_phase.sh")

    def run_script(self, *args: str, env_extra: dict | None = None, has: str = "1") -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.d), "TMUX_CALLS": str(self.calls),
               "TMUX_HAS": has, **(env_extra or {})}
        return subprocess.run([str(self.repo / "scripts" / "run_phase.sh"), *args], env=env, capture_output=True, text=True)

    def test_works_from_a_claude_code_shell_and_scrubs_env(self) -> None:
        r = self.run_script("g1", "--level", "L2", env_extra={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                                                               "ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "http://x"})
        self.assertEqual(0, r.returncode, r.stderr)
        launch = next((self.repo / "runs" / "logs").glob("g1-*.launch.sh")).read_text()
        self.assertIn("env -i HOME=", launch)
        for var in ("CLAUDECODE", "ANTHROPIC_API_KEY", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_BASE_URL"):
            self.assertNotIn(var, launch)
        self.assertIn("caffeinate -dims", launch)
        self.assertIn("-m wtth run --phase 'g1' --live '--level' 'L2'", launch)
        self.assertIn("tee -i -a", launch)

    def test_tmux_calls(self) -> None:
        self.run_script("g1")
        calls = self.calls.read_text().splitlines()
        self.assertEqual("has-session -t =wtth-g1", calls[0])  # exact match, not a prefix match
        self.assertTrue(calls[1].startswith("new-session -d -s wtth-g1 "))
        self.assertIn("set-window-option -t =wtth-g1: remain-on-exit on", calls[1])

    def test_existing_session_exits_5(self) -> None:
        r = self.run_script("g1", has="0")
        self.assertEqual(5, r.returncode)
        self.assertEqual(1, len(self.calls.read_text().splitlines()))  # no new-session

    def test_arguments_are_quoted(self) -> None:
        self.run_script("core", "--stop-at", "07:30", "--max-seed", "B-NONE=20", "--set", "x=a b'c")
        launch = next((self.repo / "runs" / "logs").glob("core-*.launch.sh")).read_text()
        self.assertIn("'x=a b'\\''c'", launch)
        self.assertIn("'--max-seed' 'B-NONE=20'", launch)

    def test_fake_flag_drops_live(self) -> None:
        self.run_script("g0dry", "--fake", "b.json")
        launch = next((self.repo / "runs" / "logs").glob("g0dry-*.launch.sh")).read_text()
        self.assertNotIn("--live", launch)

    def test_no_phase_is_usage_error(self) -> None:
        self.assertEqual(2, self.run_script().returncode)


if __name__ == "__main__":
    unittest.main()


class ExitCodeTests(unittest.TestCase):
    def test_launcher_preserves_the_orchestrator_exit_code_through_tee(self) -> None:
        d = tmp_dir(f"wp4_runphase_rc_{os.getpid()}")
        py = d / "python"
        py.write_text("#!/bin/sh\necho stub-run\nexit 3\n")
        py.chmod(0o755)
        bindir = d / "bin"
        bindir.mkdir()
        (bindir / "tmux").write_text("#!/bin/sh\n[ \"$1\" = has-session ] && exit 1\nexit 0\n")
        (bindir / "tmux").chmod(0o755)
        repo = d / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(REPO / "scripts" / "run_phase.sh", repo / "scripts" / "run_phase.sh")
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(d), "WTTH_PYTHON": str(py)}
        subprocess.run([str(repo / "scripts" / "run_phase.sh"), "g1"], env=env, check=True, capture_output=True)
        launch = next((repo / "runs" / "logs").glob("g1-*.launch.sh"))
        r = subprocess.run(["/bin/bash", str(launch)], env={"PATH": "/usr/bin:/bin", "HOME": str(d)},
                           capture_output=True, text=True)
        self.assertEqual(3, r.returncode, r.stderr)
        self.assertIn("stub-run", r.stdout)
        self.assertIn("wtth exit code: 3", r.stdout)
