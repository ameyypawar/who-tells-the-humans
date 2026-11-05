"""`python -m wtth` entry point."""
from __future__ import annotations

import sys

from wtth import cli


if __name__ == "__main__":
    sys.exit(cli.main())
