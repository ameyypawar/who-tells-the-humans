"""T0.5: install_pth.sh makes `import wtth.types` work under `python -I` from /."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = "/Users/amey/.local/wtth/venv/bin/python"


class PthTests(unittest.TestCase):
    def test_install_then_import_from_root(self) -> None:
        r = subprocess.run([str(REPO / "scripts" / "install_pth.sh")], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, r.returncode, r.stderr)
        again = subprocess.run([str(REPO / "scripts" / "install_pth.sh")], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, again.returncode, "second run (idempotent)")
        for mod in ("wtth.types", "wtth.config", "wtth.db", "wtth.textnorm", "wtth.mcp_server", "tests.util"):
            with self.subTest(module=mod):
                p = subprocess.run([PYTHON, "-I", "-c", f"import {mod}"], cwd="/", capture_output=True, text=True,
                                   timeout=60)
                self.assertEqual(0, p.returncode, p.stderr)

    def test_package_resolves_to_repo_src(self) -> None:
        p = subprocess.run([PYTHON, "-I", "-c", "import wtth; print(wtth.__file__)"], cwd="/", capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(str(REPO / "src" / "wtth" / "__init__.py"), p.stdout.strip())


if __name__ == "__main__":
    unittest.main()
