"""G0 audit. Owner: WP5 (stub from WP0)."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class AuditReport:
    def write_md(self, path: Path) -> None:
        raise NotImplementedError("WP5")


def audit_g0(settings: Any, db: Any, run_id: str) -> AuditReport:
    raise NotImplementedError("WP5")
