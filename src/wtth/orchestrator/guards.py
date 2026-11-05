"""Run-integrity helpers: code/config fingerprints and the raw-body audit hook. Owner: WP4."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any


def _tree_sha(files: list[Path], root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(str(f.relative_to(root)).encode())
        h.update(b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def src_tree_sha(settings: Any) -> str:
    root = settings.paths.repo_root / "src"
    return _tree_sha([p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                      and p.suffix in (".py", ".sql")], root)


def config_fingerprint(settings: Any) -> str:
    """configs/* plus the public pre-registered text (PREREG.md and prompts/*). Private files are never read here."""
    root = settings.paths.repo_root
    files = [p for p in (root / "configs").glob("*") if p.is_file()]
    pre = settings.paths.prereg_dir
    if (pre / "PREREG.md").is_file():
        files.append(pre / "PREREG.md")
    files += [p for p in (pre / "prompts").glob("*") if p.is_file()]
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(str(f).encode() + b"\0" + hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def wtth_import_problem(settings: Any) -> str | None:
    """None when `wtth` was imported from this repo's src/ (a stray wtth.pth would point elsewhere)."""
    import wtth

    here = Path(os.path.realpath(wtth.__file__)).parent.parent
    want = Path(os.path.realpath(settings.paths.repo_root / "src"))
    if here != want:
        return f"wtth imported from {here}, expected {want} (stray wtth.pth? re-run scripts/install_pth.sh)"
    return None


def body_audit(ctx: Any, req: Any) -> list[str]:
    """B10: production raw-body sample. Calls audit.check_turn_bodies(turn_dir, exposure_row, tools_yaml) when
    WP5 provides it; returns its problems (empty list = fine or not available)."""
    s = ctx.settings
    if not req.audit_bodies or req.turn_type.value == "probe":
        return []
    try:
        from wtth import audit
        audit.set_server_name(s.mcp_server_name)
        check = getattr(audit, "check_turn_bodies", None)
        if check is None:
            return []
        row = ctx.db.read(lambda c: c.execute("SELECT * FROM exposures WHERE turn_id=?", (req.turn_id,)).fetchone())
        return list(check(req.turn_dir, row, s.paths.tools_yaml) or [])
    except Exception as exc:  # an unreadable body is itself a problem worth a STOP
        print(f"wtth: body audit error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return [f"body audit raised {type(exc).__name__}: {exc}"]
