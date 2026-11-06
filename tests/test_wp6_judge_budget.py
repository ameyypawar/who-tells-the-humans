"""WP6-A acceptance test 6: global spend, c-bar, projections, the J1 reserve rule, --partial, CostTracker."""
from __future__ import annotations

import json
import sqlite3
import time
import unittest

from tests.test_wp6_judge_items import World
from tests.wp6_fakejudge import Rig, SEEDS, patch_seeds, valid_answer
from tests.wp6_synth import SynthSpec
from wtth import db as dbmod
from wtth.judge import call as C
from wtth.judge import items as IM
from wtth.judge import runner as R
from wtth.judge.budget import ASSUMED_C, JudgeBudget
from wtth.judge.db import create_judge_db, judge_db_path, judge_spent, open_judge_db, set_meta
from wtth.judge.types import BudgetBinds, ItemType
from wtth.orchestrator.context import CostTracker


def put_ledger(jdb_path, rows):
    """Raw ledger rows (foreign keys are off on a plain sqlite3 connection)."""
    con = sqlite3.connect(jdb_path)
    for i, (cost, itype, est) in enumerate(rows):
        con.execute("INSERT INTO judge_ledger(call_id, run_id, model, tier, item_type, cost_usd, estimated, ts) "
                    "VALUES (?,?,?,?,?,?,?,?)", (f"x{time.time_ns()}{i}", "r", "m", "J1", itype, cost, est, 0.0))
    con.commit()
    con.close()


class Spend(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(self.id().split(".")[-1][:36], {"budget.prior_spend_usd": 1.25})
        self.s = self.rig.settings
        dbmod.create_db(self.s.paths.db_path)
        self.main = dbmod.Db(self.s.paths.db_path)
        create_judge_db(judge_db_path(self.s))
        self.jdb = open_judge_db(self.s)

    def add_main(self, usd):
        from tests.util import seed_episode, seed_run, seed_turn, seed_attempt

        def fn(c):
            if c.execute("SELECT 1 FROM runs").fetchone() is None:
                seed_run(c)
            ep = seed_episode(c, f"k{time.time_ns()}", status="running", seed=int(time.time_ns() % 100000))
            seed_attempt(c, ep)
            tid = f"t{time.time_ns()}"
            seed_turn(c, tid, ep)
            c.execute("INSERT INTO cost_ledger(turn_id, run_id, phase, model, cost_usd, estimated, ts) "
                      "VALUES (?,?,?,?,?,0,?)", (tid, "run-1", "core", "m", usd, time.time()))
        self.main.write(fn)

    def test_global_spent_is_prior_plus_both_ledgers(self):
        b = JudgeBudget(self.s, self.main, self.jdb)
        self.assertAlmostEqual(b.global_spent(), 1.25)
        self.add_main(3.0)
        self.add_main(0.5)
        put_ledger(judge_db_path(self.s), [(0.25, "note", 0), (0.125, "board_post", 1)])
        self.assertAlmostEqual(b.main_spent(), 3.5)
        self.assertAlmostEqual(b.judge_spent(), 0.375)
        self.assertAlmostEqual(b.global_spent(), 1.25 + 3.5 + 0.375)
        self.assertAlmostEqual(b.remaining(), 80 - 0.5 - (1.25 + 3.5 + 0.375))

    def test_cost_tracker_includes_judge_spend(self):
        ct = CostTracker(self.s, self.main)
        self.assertAlmostEqual(ct.spent(), 1.25)
        put_ledger(judge_db_path(self.s), [(2.0, "note", 0)])
        self.assertAlmostEqual(ct.spent(), 3.25)
        self.assertAlmostEqual(judge_spent(self.s), 2.0)

    def test_cost_tracker_unchanged_without_judge_database(self):
        rig = Rig("nojudge1", {"budget.prior_spend_usd": 0.5})
        dbmod.create_db(rig.settings.paths.db_path)
        self.assertFalse(judge_db_path(rig.settings).exists())
        self.assertEqual(judge_spent(rig.settings), 0.0)
        self.assertAlmostEqual(CostTracker(rig.settings, dbmod.Db(rig.settings.paths.db_path)).spent(), 0.5)

    def test_launch_guard_formula(self):
        b = JudgeBudget(self.s, self.main, self.jdb)                 # cap 80, margin 0.5, call cap 0.10
        put_ledger(judge_db_path(self.s), [(78.0, "note", 0)])     # spent = 1.25 + 78 = 79.25
        self.assertTrue(b.can_launch(0))                          # 79.25 + 0.10 <= 79.5
        self.assertTrue(b.can_launch(1))                          # 79.25 + 0.20 <= 79.5
        self.assertFalse(b.can_launch(2))                         # 79.25 + 0.30 > 79.5
        self.assertAlmostEqual(b.remaining(), 0.25)

    def test_c_bar_rules(self):
        b = JudgeBudget(self.s, self.main, self.jdb)
        self.assertIsNone(b.c_bar())
        self.assertEqual(b.c_for(ItemType.NOTE), ASSUMED_C[ItemType.NOTE])
        put_ledger(judge_db_path(self.s), [(0.01, "note", 0)] * 10 + [(0.03, "d1_answer", 0)] * 10)
        self.assertAlmostEqual(b.c_bar(), 0.02)                  # overall: fewer than 20 calls per type
        self.assertAlmostEqual(b.c_bar(ItemType.NOTE), 0.02)
        put_ledger(judge_db_path(self.s), [(0.01, "note", 0)] * 10)
        self.assertAlmostEqual(b.c_bar(ItemType.NOTE), 0.01)     # 20 note calls: the type mean
        self.assertAlmostEqual(b.c_bar(ItemType.D1_ANSWER), (0.01 * 20 + 0.03 * 10) / 30)

    def test_projection(self):
        w = World("proj_items", SynthSpec.small(4))
        items = IM.load_items(w.jdb, subtier="J2c")[:10]
        b = JudgeBudget(w.s, None, w.jdb)
        p = b.project(items, factor=1.1)
        want = sum(ASSUMED_C[i.item_type] * 1.1 for i in items)
        self.assertAlmostEqual(p.need_usd, want)
        self.assertEqual((p.n_items, p.fits, p.n_fit), (10, True, 10))
        self.assertAlmostEqual(p.remaining_usd, 80 - 0.5)
        tight = JudgeBudget(w.s, None, w.jdb, margin_usd=80 - want / 2)
        q = tight.project(items, factor=1.1)
        self.assertFalse(q.fits)
        self.assertTrue(0 < q.n_fit < 10)
        empty = b.project([], factor=1.5)
        self.assertEqual((empty.n_items, empty.need_usd, empty.fits), (0, 0.0, True))


class Tiers(unittest.TestCase):
    def build(self, name, overrides):
        patch_seeds(self, SEEDS)
        w = World(name, SynthSpec.small(4))
        w.s2 = w.rig.settings
        return w

    def make(self, name, **budget):
        patch_seeds(self, SEEDS)
        rig_over = {f"budget.{k}": v for k, v in budget.items()}
        spec = SynthSpec.small(4)
        # a World whose settings carry the budget overrides
        import tests.test_wp6_judge_items as TI
        orig = TI.Rig

        class R2(orig):
            def __post_init__(self_):
                self_.overrides = {**rig_over}
                orig.__post_init__(self_)
        TI.Rig = R2
        try:
            w = TI.World(name, spec)
        finally:
            TI.Rig = orig
        set_meta(w.jdb, "validation_passed", "1")
        set_meta(w.jdb, "validation_level", "L2")
        return w

    def n_calls(self, w):
        return w.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM calls").fetchone()[0])

    def test_validation_required(self):
        patch_seeds(self, SEEDS)
        w = World("tier_noval", SynthSpec.small(3))
        from wtth.judge.types import JudgeStop
        with self.assertRaises(JudgeStop):
            R.run_tier(w.s, "J1")

    def test_j1_reserve_rule_binds_before_any_call(self):
        w = self.make("tier_j1bind", cap_usd=1.0)             # remaining 0.5: c-bar 0.0067 x N x 1.5 > 0.5 for N > 49
        n = len(IM.load_items(w.jdb, tier="J1"))
        self.assertGreater(n, 60)
        with self.assertRaises(BudgetBinds) as cm:
            R.run_tier(w.s, "J1", k=2)
        self.assertIn("J1 reserve", str(cm.exception))
        self.assertEqual(self.n_calls(w), 0)
        ev = w.jdb.read(lambda c: c.execute("SELECT detail_json FROM jevents WHERE kind='budget_binds'").fetchall())
        self.assertEqual(len(ev), 1)
        self.assertEqual(json.loads(ev[0][0])["rule"], "J1 reserve")
        self.assertEqual(w.jdb.read(lambda c: c.execute("SELECT exit_code FROM judge_runs").fetchone()[0]), 4)

    def test_j1_runs_when_the_reserve_fits(self):
        w = self.make("tier_j1ok")
        items = IM.load_items(w.jdb, subtier="J1c") + IM.load_items(w.jdb, subtier="J1b")
        self.assertTrue(items)
        w.rig.answer_all(items)
        w.rig.write()
        res = R.run_tier(w.s, "J1", subtiers=["J1c", "J1b"], k=3)
        self.assertEqual([r.n_items for r in res], [len(IM.load_items(w.jdb, subtier=s)) for s in ("J1c", "J1b")])
        self.assertTrue(all(r.n_valid == r.n_items and r.stopped is None for r in res))
        self.assertEqual(IM.load_items(w.jdb, subtier="J1b", unsettled_only=True), [])
        again = R.run_tier(w.s, "J1", subtiers=["J1c", "J1b"], k=3)
        self.assertEqual(again, [])                            # everything is settled

    def test_j2_projection_binds_without_partial(self):
        w = self.make("tier_j2bind", cap_usd=0.8)
        its = IM.load_items(w.jdb, subtier="J2a")
        self.assertGreater(len(its), 40)
        with self.assertRaises(BudgetBinds):
            R.run_tier(w.s, "J2", subtiers=["J2a"], k=1)
        self.assertEqual(self.n_calls(w), 0)

    def test_partial_stops_cleanly_at_the_guard(self):
        w = self.make("tier_partial", cap_usd=0.8)
        its = IM.load_items(w.jdb, subtier="J2a")
        w.rig.answer_all(its)
        w.rig.write()
        res = R.run_tier(w.s, "J2", subtiers=["J2a", "J2b"], partial=True, k=1)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].stopped, "budget")
        done = w.jdb.read(lambda c: [r[0] for r in c.execute("SELECT item_id FROM judge_labels").fetchall()])
        self.assertTrue(0 < len(done) < len(its))
        # the judged items are exactly the first ones of the seeded order
        self.assertEqual(set(done), {i.item_id for i in its[:len(done)]})
        spent = w.jdb.read(lambda c: c.execute("SELECT usd FROM v_judge_spend").fetchone()[0])
        self.assertLessEqual(spent - 0.006 + 0.10, 0.8 - 0.5 + 1e-9)     # the last launch was allowed ...
        self.assertGreater(spent + 0.10, 0.8 - 0.5)                       # ... and the next one was not
        ev = w.jdb.read(lambda c: c.execute(
            "SELECT detail_json FROM jevents WHERE kind='budget_binds' AND detail_json LIKE '%fractions%'").fetchall())
        self.assertEqual(len(ev), 1)
        fr = json.loads(ev[0][0])["fractions"]
        self.assertEqual(sum(v[0] for v in fr["J2a"].values()), len(done))
        self.assertEqual(sum(v[1] for v in fr["J2a"].values()), len(its))
        self.assertEqual(w.jdb.read(lambda c: c.execute("SELECT exit_code FROM judge_runs").fetchone()[0], ), 0)
        self.assertEqual(w.jdb.read(lambda c: c.execute("SELECT COUNT(*) FROM calls WHERE ended_at IS NULL")
                                    .fetchone()[0]), 0)


if __name__ == "__main__":
    unittest.main()
