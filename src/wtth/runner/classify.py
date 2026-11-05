"""Turn classification (research/41 section 2.6 table). Owner: WP3 (stub from WP0)."""
from __future__ import annotations

from wtth.types import FailClass, ProcResult, RefusalFlags, StreamRecord, TurnClass


def classify(proc: ProcResult, rec: StreamRecord, *, init_v: list[str], result_v: list[str], reply_v: list[str],
             reconcile_v: list[str], server_fault: bool, cancelled: bool,
             refusal: RefusalFlags) -> tuple[TurnClass, FailClass | None, str]:
    raise NotImplementedError("WP3")
