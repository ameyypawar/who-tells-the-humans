"""WP6 test runner (plan 47 G-4). Usage: python -I scripts/wp6_test.py [pattern]

Puts this worktree's src/ and root first on sys.path (the venv .pth points at the main checkout),
then runs unittest discovery for tests/test_wp6_<pattern>.py (default test_wp6_*.py).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def main(argv: list[str]) -> int:
    pattern = argv[1] if len(argv) > 1 else "test_wp6_*"
    if not pattern.endswith(".py"):
        pattern += ".py"
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=pattern, top_level_dir=str(ROOT))
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
