"""Main-database snapshot and the analysis set (plan 47 2.2, D-008, F-18, F-19)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wtth import db as dbmod


class AnalysisSetError(RuntimeError):
    """The analysis set cannot be defined without a decision (several levels, mvp plus core, ...)."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    sha256: str
    taken_at: float


@dataclass(frozen=True, slots=True)
class EpisodeRef:
    episode_id: int
    attempt: int
    phase: str
    scenario: str
    model: str
    cell: str
    level: str
    seed: int
    world: str
    handles: tuple[str, str, str]
    paraphrase_idx: int
    handle_idx: int
    ticket_idx: int


@dataclass(frozen=True, slots=True)
class AnalysisSet:
    phase: str
    model: str
    s_c: int | None
    cut: Mapping[str, Any]
    planned: Mapping[str, tuple[int, ...]]
    episodes: tuple[EpisodeRef, ...]
    above: tuple[EpisodeRef, ...]
    failed_permanent: tuple[tuple[str, int], ...]
    missing: tuple[tuple[str, int], ...]
    fingerprint: str


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_main_db(settings: Any) -> Snapshot:
    """Online backup of the main database into runs/analysis/snapshots/wtth-<UTC>.sqlite. Readers use only this."""
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%S")
    snap_dir = Path(settings.paths.runs_dir) / "analysis" / "snapshots"
    dest = snap_dir / f"wtth-{stamp}.sqlite"
    n = 1
    while dest.exists():
        dest = snap_dir / f"wtth-{stamp}-{n}.sqlite"
        n += 1
    dbmod.backup(Path(settings.paths.db_path), dest)
    return Snapshot(dest, _sha256_file(dest), now)


def fingerprint_of(pairs: list[tuple[int, int]]) -> str:
    text = "\n".join(f"{e}:{a}" for e, a in sorted(pairs))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cut_in_force(settings: Any, db: Any, phase: str) -> dict[str, Any]:
    rows = db.read(lambda c: c.execute("SELECT detail_json FROM events WHERE kind='cut' ORDER BY id DESC").fetchall())
    for r in rows:
        d = json.loads(r[0] or "{}")
        if d.get("phase") == phase:
            return {"tier": d.get("tier", settings.cuts.tier),
                    "max_seed_by_cell": {k: int(v) for k, v in (d.get("max_seed_by_cell") or {}).items()},
                    "source": "events"}
    return {"tier": settings.cuts.tier, "max_seed_by_cell": dict(settings.cuts.max_seed_by_cell), "source": "config"}


def _planned(settings: Any, phase: str, level: str, cut: Mapping[str, Any]) -> dict[str, tuple[int, ...]]:
    from wtth.orchestrator import scheduler

    cuts2 = dataclasses.replace(settings.cuts, tier=cut["tier"], max_seed_by_cell=dict(cut["max_seed_by_cell"]))
    settings2 = dataclasses.replace(settings, cuts=cuts2)
    out: dict[str, list[int]] = {}
    for sp in scheduler.build_queue(settings2, phase, level):
        if sp.phase != phase:
            continue
        out.setdefault(sp.cell.name, []).append(sp.seed)
    return {c: tuple(sorted(v)) for c, v in out.items()}


def _ref(r: Any) -> EpisodeRef:
    return EpisodeRef(
        episode_id=int(r["episode_id"]), attempt=int(r["final_attempt"]), phase=r["phase"], scenario=r["scenario"],
        model=r["model"], cell=r["cell"], level=r["level"], seed=int(r["seed"]), world=r["world"],
        handles=tuple(json.loads(r["handles_json"])), paraphrase_idx=int(r["paraphrase_idx"]),
        handle_idx=int(r["handle_idx"]), ticket_idx=int(r["ticket_idx"]))


def analysis_set(settings: Any, db: Any, phase: str, *, prefer: str | None = None) -> AnalysisSet:
    """The D-008 analysis set of one phase ('core' | 'mvp' | 'sonnet') read from a snapshot Db."""
    rows = db.read(lambda c: c.execute(
        "SELECT * FROM episodes WHERE phase=? AND probe=0 AND seed<9000 ORDER BY episode_id", (phase,)).fetchall())
    if not rows:
        raise AnalysisSetError(f"phase {phase!r} has no episodes in this database")
    levels = sorted({r["level"] for r in rows})
    if len(levels) != 1:
        raise AnalysisSetError(f"phase {phase!r} has several levels {levels}; the analysis needs exactly one")
    models = sorted({r["model"] for r in rows})
    if len(models) != 1:
        raise AnalysisSetError(f"phase {phase!r} has several models {models}")
    level, model = levels[0], models[0]
    cut = _cut_in_force(settings, db, phase)
    planned = _planned(settings, phase, level, cut)
    by_key = {(r["cell"], int(r["seed"])): r for r in rows}

    def state(cell: str, seed: int) -> str:
        r = by_key.get((cell, seed))
        return r["status"] if r is not None else "absent"

    planned_pairs = [(c, s) for c, seeds in planned.items() for s in seeds]
    incomplete = sorted(s for c, s in planned_pairs if state(c, s) not in ("done", "failed"))
    if not planned_pairs:
        s_c = None
    elif incomplete:
        s_c = incomplete[0] - 1 or None
    else:
        s_c = max(s for _, s in planned_pairs)

    done_rows = db.read(lambda c: c.execute(
        "SELECT * FROM episodes WHERE phase=? AND probe=0 AND seed<9000 AND status='done' AND model=? "
        "ORDER BY episode_id", (phase, model)).fetchall())
    # F-18: the other frozen phase of the same arm must not double-count (cell, seed).
    other = {"core": "mvp", "mvp": "core"}.get(phase)
    exclude: set[tuple[str, int]] = set()
    if other is not None:
        orows = db.read(lambda c: c.execute(
            "SELECT cell, seed FROM episodes WHERE phase=? AND probe=0 AND status='done' AND model=?",
            (other, model)).fetchall())
        overlap = {(r["cell"], int(r["seed"])) for r in orows} & {(r["cell"], int(r["seed"])) for r in done_rows}
        if overlap:
            if prefer is None:
                raise AnalysisSetError(
                    f"phases core and mvp both have done episodes for {len(overlap)} (cell, seed) pairs; "
                    f"pass prefer='core' (or 'mvp') to choose one (F-18)")
            if prefer not in ("core", "mvp"):
                raise AnalysisSetError(f"prefer must be 'core' or 'mvp', got {prefer!r}")
            if prefer != phase:
                exclude = overlap
    planned_set = set(planned_pairs)
    inside: list[EpisodeRef] = []
    above: list[EpisodeRef] = []
    for r in done_rows:
        key = (r["cell"], int(r["seed"]))
        if key in exclude:
            continue
        if key in planned_set and s_c is not None and key[1] <= s_c:
            inside.append(_ref(r))
        else:
            above.append(_ref(r))
    failed_perm = tuple(sorted((c, s) for c, s in planned_pairs
                               if state(c, s) == "failed" and s_c is not None and s <= s_c))
    missing = tuple(sorted((c, s) for c, s in planned_pairs if state(c, s) not in ("done", "failed")))
    fp = fingerprint_of([(e.episode_id, e.attempt) for e in inside])
    return AnalysisSet(phase=phase, model=model, s_c=s_c, cut=cut, planned=planned, episodes=tuple(inside),
                       above=tuple(above), failed_permanent=failed_perm, missing=missing, fingerprint=fp)


def model_short(model_id: str) -> str:
    return "haiku" if "haiku" in model_id else "sonnet" if "sonnet" in model_id else model_id
