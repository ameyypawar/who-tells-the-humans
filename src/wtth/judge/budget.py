"""Global spend, c-bar, projections and the launch guard (plan 47 section 2.8)."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from wtth.judge.types import Item, ItemType

ASSUMED_C = {ItemType.BOARD_POST: 0.0067, ItemType.NOTE: 0.0067, ItemType.D1_ANSWER: 0.008, ItemType.D2_ANSWER: 0.008}
MIN_CALLS_PER_TYPE = 20


@dataclass(frozen=True, slots=True)
class Projection:
    n_items: int
    c_bar: float
    factor: float
    need_usd: float
    remaining_usd: float
    fits: bool
    n_fit: int


class JudgeBudget:
    def __init__(self, settings: Any, main_db: Any, jdb: Any, *, margin_usd: float = 0.5) -> None:
        self.settings = settings
        self.main_db = main_db
        self.jdb = jdb
        self.margin = margin_usd
        self.call_cap = float(settings.models.budget_for(settings.models.judge))

    def main_spent(self) -> float:
        if self.main_db is None:
            return 0.0
        try:
            row = self.main_db.read(lambda c: c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM cost_ledger").fetchone())
        except (FileNotFoundError, sqlite3.Error):
            return 0.0
        return float(row[0])

    def judge_spent(self) -> float:
        """This judge database's ledger plus every other judge database under runs_dir (D-022: the follow-up has its
        own judge database; both count against the one global cap)."""
        from wtth.judge.db import judge_spent as other_judge_spent

        own = float(self.jdb.read(lambda c: c.execute("SELECT usd FROM v_judge_spend").fetchone())[0])
        path = getattr(self.jdb, "path", None)
        if path is None:      # no file to tell apart from the others: this database alone (pre-D-022 behaviour)
            return own
        return own + other_judge_spent(self.settings, exclude=path)

    def global_spent(self) -> float:
        return self.settings.budget.prior_spend_usd + self.main_spent() + self.judge_spent()

    def remaining(self) -> float:
        """Room under the cap, margin kept back."""
        return self.settings.budget.cap_usd - self.margin - self.global_spent()

    def can_launch(self, n_inflight: int) -> bool:
        """global_spent + inflight x cap + cap <= cap_usd - margin (the judge may spend into the reserve)."""
        return self.global_spent() + n_inflight * self.call_cap + self.call_cap <= self.settings.budget.cap_usd - self.margin

    def c_bar(self, item_type: ItemType | None = None) -> float | None:
        """Mean cost per call: of the item type when it has >= 20 calls, else the overall mean; None if no calls."""
        def read(c: sqlite3.Connection):
            tot = c.execute("SELECT COUNT(*), COALESCE(AVG(cost_usd),0) FROM judge_ledger").fetchone()
            by = None
            if item_type is not None:
                by = c.execute("SELECT COUNT(*), COALESCE(AVG(cost_usd),0) FROM judge_ledger WHERE item_type=?",
                               (ItemType(item_type).value,)).fetchone()
            return tot, by
        tot, by = self.jdb.read(read)
        if by is not None and by[0] >= MIN_CALLS_PER_TYPE:
            return float(by[1])
        return float(tot[1]) if tot[0] else None

    def c_for(self, item_type: ItemType) -> float:
        c = self.c_bar(item_type)
        return c if c is not None else ASSUMED_C[ItemType(item_type)]

    def project(self, items: Sequence[Item], *, factor: float) -> Projection:
        costs = [self.c_for(i.item_type) * factor for i in items]
        need = sum(costs)
        rem = self.remaining()
        n_fit, acc = 0, 0.0
        for c in costs:
            if acc + c > rem:
                break
            acc += c
            n_fit += 1
        mean = (sum(self.c_for(i.item_type) for i in items) / len(items)) if items else 0.0
        return Projection(len(items), mean, factor, need, rem, need <= rem, n_fit)
