"""T0.5: install_pth.sh makes `import wtth.types` work under `python -I` from /.

The installer is run against a TEMP site-packages directory (WTTH_SITE_PACKAGES): the real venv's wtth.pth is a
global code pointer and a test must never rewrite it (review-2 P1-6).
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.util import tmp_dir

REPO = Path(__file__).resolve().parents[1]
WTTH_HOME = Path(os.environ.get("WTTH_HOME", "/Users/amey/.local/wtth"))
PYTHON = os.environ.get("WTTH_PYTHON") or sys.executable
REAL_PTH = WTTH_HOME / "venv/lib/python3.12/site-packages/wtth.pth"


class PthTests(unittest.TestCase):
    def install(self, sp: Path) -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "WTTH_SITE_PACKAGES": str(sp)}
        return subprocess.run([str(REPO / "scripts" / "install_pth.sh")], env=env, capture_output=True, text=True, timeout=60)

    def test_installer_writes_both_lines_idempotently_to_a_temp_dir(self) -> None:
        sp = tmp_dir(f"wp0_pth_{os.getpid()}")
        before = REAL_PTH.read_bytes() if REAL_PTH.exists() else None
        self.assertEqual(0, self.install(sp).returncode)
        self.assertEqual(0, self.install(sp).returncode)
        self.assertEqual([str(REPO / "src"), str(REPO)], (sp / "wtth.pth").read_text().splitlines())
        self.assertEqual(before, REAL_PTH.read_bytes() if REAL_PTH.exists() else None)  # real file untouched

    def test_temp_pth_makes_wtth_importable_without_the_venv_site_packages(self) -> None:
        sp = tmp_dir(f"wp0_pth2_{os.getpid()}")
        self.install(sp)
        code = ("import site, sys; site.addsitedir(sys.argv[1]); import wtth.types, tests.util; "
                "print(wtth.types.__file__)")
        p = subprocess.run([PYTHON, "-I", "-S", "-c", code, str(sp)], cwd="/", capture_output=True, text=True, timeout=60)
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertEqual(str(REPO / "src" / "wtth" / "types.py"), p.stdout.strip())

    def test_real_venv_imports_from_the_repo(self) -> None:
        for mod in ("wtth.types", "wtth.config", "wtth.db", "wtth.textnorm", "wtth.mcp_server", "tests.util"):
            with self.subTest(module=mod):
                p = subprocess.run([PYTHON, "-I", "-c", f"import {mod}"], cwd="/", capture_output=True, text=True,
                                   timeout=60)
                self.assertEqual(0, p.returncode, p.stderr)
        p = subprocess.run([PYTHON, "-I", "-c", "import wtth; print(wtth.__file__)"], cwd="/", capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(str(REPO / "src" / "wtth" / "__init__.py"), p.stdout.strip())


if __name__ == "__main__":
    unittest.main()
