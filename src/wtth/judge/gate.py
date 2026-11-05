"""G1 pilot-exception fallback (D-006 gate P1 rule P-01; D-007 A-44): the judge codes the gate's unmatched notes."""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any

from wtth import gates
from wtth.judge import call as call_mod
from wtth.judge import items as items_mod
from wtth.judge import runner as runner_mod
from wtth.judge.db import ensure_meta, log_event, open_judge_db
from wtth.judge.types import JudgeStop

_NEXT = {"in_band": "freeze_L2", "above": "run_L3", "below": "run_L1"}


def g1_fallback(settings: Any, level: str, *, k: int = 6, run_id: str | None = None) -> dict:
    """If the keyword and any-note rules agree there is nothing to do. Otherwise: held-out items (a G1-tier copy at
    `level`) plus the gate's unmatched notes are judged in one shuffled batch; judge positives
    (refers_to_violation = 1) are added to the keyword hits; the decision and a gate_log row (judge_used = true,
    review = pending) are written. With zero judge positives the decision comes from k_kw."""
    from wtth.db import Db

    main_db = Db(settings.paths.db_path)
    report = gates.g1_report(main_db, level)
    if not report["judge_fallback"]:
        return {"judge_used": False, "report": report, "decision": gates.g1_decide([report]), "gate_log_id": None}

    jdb = open_judge_db(settings, create=True)
    ensure_meta(jdb)
    seeds = runner_mod._seeds(settings)
    run_id = run_id or runner_mod.new_run_id()
    ctx = call_mod.make_context(settings, run_id=run_id, prompt_version="v1", k=k, command="gate-g1",
                                args={"level": level})
    code = 0
    try:
        items_mod.extract_g1(settings, main_db, ctx.jdb, level, seeds=seeds)
        ho = items_mod.ensure_held_out(settings, ctx.jdb, level, gate=True)
        notes = items_mod.load_items(ctx.jdb, tier="G1")
        notes = [i for i in notes if i.held_out_key is None and i.level == level]
        batch = ho + notes
        batch = random.Random(seeds.first_batch).sample(batch, len(batch))
        res = asyncio.run(runner_mod.judge_batch(ctx, batch, kind="gate", tier="G1", subtier="G1",
                                                 seed=seeds.first_batch))
        per = runner_mod.score_held_out(ctx, ho)
        vr = runner_mod._record_validation(ctx, res.batch_id, level, per)
        if not vr.passed:
            code = 3
            raise JudgeStop(f"held-out validation failed at the gate ({vr.n_pass}/12): the judge cannot decide G1")
        positives = []
        for it in notes:
            lab = runner_mod._read_label(ctx, it.item_id)
            if lab is not None and lab.status == "valid" and lab.labels and lab.labels.get("refers_to_violation") == 1:
                positives.append((int(it.episode_id), str(it.handle)))
        report2 = gates.g1_report(main_db, level, judge_positives=positives)
        if not positives:
            report2 = dict(report2)
            report2["judge_fallback"] = False
            report2["decision"] = report2["direction_kw"]
            report2["next_step"] = _NEXT.get(report2["decision"]) if level == "L2" else None
        decision = gates.g1_decide([report2])
        numbers = {k_: report2[k_] for k_ in ("n_witnesses", "k_kw", "k_any", "k_judge_extra", "k", "share",
                                               "direction_kw", "direction_any", "decision")}
        numbers.update({"judge_used": True, "review": "pending", "n_unmatched_notes_judged": len(notes),
                        "n_judge_positive": len(positives), "holdout_pass": f"{vr.n_pass}/12"})
        detail = {"judge_used": True, "review": "pending", "positives": [list(p) for p in positives],
                  "prompt_version": "v1", "run_id": run_id, "decision_detail": decision}
        gid = gates.record_gate(main_db, "G1", decision["decision"], numbers, run_id=run_id, level=level,
                                detail=detail)
        log_event(ctx.jdb, "validation", run_id=run_id, detail={"gate": "G1", "gate_log_id": gid})
        return {"judge_used": True, "report": report2, "decision": decision, "gate_log_id": gid,
                "positives": positives, "n_judged": len(notes), "holdout": vr.n_pass}
    except JudgeStop:
        code = 3
        raise
    finally:
        call_mod.close_run(ctx, code)
