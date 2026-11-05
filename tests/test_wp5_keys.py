"""T5.6: scripts/check_no_keys.sh over the repo and over everything a fake run wrote."""
from __future__ import annotations

import subprocess
import unittest

from tests.util import DUMMY_KEY, REPO, tmp_dir
from tests.wp5_support import G0Builder, Rig

SCRIPT = REPO / "scripts" / "check_no_keys.sh"
PREFIX = "sk-" + "ant"


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=300)


class TestCheckNoKeys(unittest.TestCase):
    def test_script_itself_never_contains_the_prefix(self):
        self.assertNotIn(PREFIX, SCRIPT.read_text(encoding="utf-8"))
        self.assertIn('P="sk-""ant"', SCRIPT.read_text(encoding="utf-8"))

    def test_detects_a_key_and_prints_only_the_file_name(self):
        d = tmp_dir("wp5_keys_hit")
        (d / "a.txt").write_text("nothing\n", encoding="utf-8")
        (d / "leak.txt").write_text("export K=" + DUMMY_KEY + "\n", encoding="utf-8")
        r = run_script("--no-git", str(d))
        self.assertEqual(r.returncode, 1)
        self.assertIn("leak.txt", r.stdout)
        self.assertNotIn("a.txt", r.stdout)
        self.assertNotIn(DUMMY_KEY, r.stdout + r.stderr)

    def test_detects_a_key_in_a_hidden_file_and_binary(self):
        d = tmp_dir("wp5_keys_hidden")
        (d / ".hidden").write_bytes(b"\x00\x01" + DUMMY_KEY.encode() + b"\x02")
        self.assertEqual(run_script("--no-git", str(d)).returncode, 1)

    def test_clean_dir_and_missing_path(self):
        d = tmp_dir("wp5_keys_clean")
        (d / "a.txt").write_text("ok", encoding="utf-8")
        r = run_script("--no-git", str(d))
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "check_no_keys: no hits"))
        self.assertEqual(run_script("--no-git", str(d / "nope")).returncode, 2)

    def test_repo_sources_and_git_history_are_clean(self):
        paths = [str(REPO / p) for p in ("src", "configs", "scripts")] + sorted(str(p) for p in (REPO / "tests").glob("*.py"))
        r = run_script(*paths)  # includes git history
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_everything_a_fake_run_wrote_is_clean(self):
        rig = Rig("wp5_keys_run", world="R1")
        b = G0Builder(rig)
        b.turn("witness")
        b.turn("debrief")
        # the dummy key is in the env of the fake and in the behaviour file's contract only; scan what the run wrote
        written = [str(p) for p in (rig.root / "turns", rig.slots, rig.faults, rig.cwd, rig.root / "wtth.sqlite") if p.exists()]
        r = run_script("--no-git", *written)
        self.assertEqual(r.returncode, 0, r.stdout)
        blob = b"".join(f.read_bytes() for p in (rig.root / "turns", rig.slots) for f in p.rglob("*") if f.is_file())
        self.assertNotIn(DUMMY_KEY.encode(), blob)
        self.assertNotIn(PREFIX.encode(), (rig.root / "wtth.sqlite").read_bytes())


if __name__ == "__main__":
    unittest.main()
