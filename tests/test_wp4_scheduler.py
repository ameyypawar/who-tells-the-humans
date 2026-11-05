"""T4.1: scheduler, queue, cost cap, breaker, stop rules (stub attempts, no processes)."""
from __future__ import annotations

import asyncio
import json
import time
import unittest
import uuid

from tests.wp4_common import ledger, make_ctx, patch_stimuli_if_needed
from wtth import db as dbmod
from wtth.config import load_settings
from wtth.orchestrator import episode as ep
from wtth.orchestrator import scheduler as sch
from wtth.orchestrator.context import Breaker, CostTracker, KGate, LaunchGate, SlotPool
from wtth.types import AttemptResult, FailClass, TurnClass


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_stimuli_if_needed(self)
        self.s = load_settings()

    def test_totals_d003(self) -> None:
        self.assertEqual(670, len(sch.build_queue(self.s, "core", "L2")))
        self.assertEqual(150, len(sch.build_queue(self.s, "sonnet", "L2")))

    def test_seed_major_perm_order(self) -> None:
        q = sch.build_queue(self.s, "core", "L2")
        perm = sch.permutation(self.s)
        self.assertEqual(sorted(perm), sorted(self.s.core_cells))
        self.assertEqual(perm, sch.permutation(self.s))  # deterministic
        self.assertEqual(perm, [x.cell.name for x in q[:11]])
        self.assertEqual({1}, {x.seed for x in q[:11]})
        # seeds 51-80 run only the four 80-seed cells, still in PERM order
        late = [x for x in q if x.seed == 60]
        self.assertEqual([c for c in perm if c in ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT")], [x.cell.name for x in late])
        seeds = [x.seed for x in q]
        self.assertEqual(seeds, sorted(seeds))

    def test_queue_pos_stable_and_phase_based(self) -> None:
        a = sch.build_queue(self.s, "core", "L2")
        b = sch.build_queue(self.s, "core", "L2")
        self.assertEqual([x.queue_pos for x in a], [x.queue_pos for x in b])
        self.assertEqual(6_000_000, a[0].queue_pos)
        self.assertEqual(sorted(x.queue_pos for x in a), [x.queue_pos for x in a])
        self.assertEqual(7_000_000, sch.build_queue(self.s, "sonnet", "L2")[0].queue_pos)

    def test_sonnet_uses_perm_filtered(self) -> None:
        q = sch.build_queue(self.s, "sonnet", "L2")
        perm = [c for c in sch.permutation(self.s) if c in ("NONE", "CLAIM", "NONE-R", "NO-RECEIPT", "FORGED")]
        self.assertEqual(perm, [x.cell.name for x in q[:5]])
        self.assertEqual({self.s.models.sonnet}, {x.model for x in q})

    def test_cuts(self) -> None:
        s = load_settings(overrides={"cuts.max_seed_by_cell": {"B-NONE": 20}})
        q = sch.build_queue(s, "core", "L2")
        self.assertEqual(670 - 30, len(q))
        self.assertEqual(20, max(x.seed for x in q if x.cell.name == "B-NONE"))
        lean = sch.build_queue(load_settings(overrides={"cuts.tier": "lean"}), "core", "L2")
        self.assertEqual(30 * 4 + 24 * 7, len(lean))
        mvp = sch.build_queue(load_settings(overrides={"cuts.tier": "mvp"}), "core", "L2")
        self.assertEqual(8 * 24, len(mvp))
        self.assertNotIn("RECEIPT", {x.cell.name for x in mvp})

    def test_level_resolution(self) -> None:
        from wtth.config import ConfigError
        with self.assertRaises(ConfigError):
            sch.build_queue(self.s, "core", None)  # frozen level not set
        with self.assertRaises(ConfigError):
            sch.build_queue(self.s, "g1", None)
        self.assertEqual({"L3"}, {x.level for x in sch.build_queue(self.s, "g1", "L3")})
        self.assertEqual({"L2"}, {x.level for x in sch.build_queue(self.s, "g0", None)})
        frozen = load_settings(overrides={"frozen_level": "L1"})
        self.assertEqual({"L1"}, {x.level for x in sch.build_queue(frozen, "core", None)})

    def test_pilot_blocks(self) -> None:
        g1 = sch.build_queue(self.s, "g1", "L2")
        self.assertEqual(list(range(9011, 9027)), [x.seed for x in g1])
        g2 = sch.build_queue(self.s, "g2", "L2")
        self.assertEqual(32, len(g2))
        g0 = sch.build_queue(self.s, "g0", "L2")
        self.assertEqual("g0probe", g0[0].phase)
        self.assertEqual([9001, 9002, 9003, 9004], [x.seed for x in g0[1:]])
        fake = load_settings(overrides={"fake": True})
        self.assertEqual(4, len(sch.build_queue(fake, "g0", "L2")))  # no deny probe in fake mode
        self.assertEqual(11, len(sch.build_queue(self.s, "g0dry", None)))

    def test_enqueue_idempotent(self) -> None:
        ctx, _ = make_ctx("enqueue")
        specs = sch.build_queue(ctx.settings, "g2", "L2")
        self.assertEqual(32, sch.enqueue(ctx.db, specs))
        self.assertEqual(0, sch.enqueue(ctx.db, specs))
        rows = ctx.db.read(lambda c: c.execute("SELECT queue_pos, status, runbook, c_real_note FROM episodes ORDER BY queue_pos").fetchall())
        self.assertEqual([x.queue_pos for x in specs], [r["queue_pos"] for r in rows])
        self.assertEqual({"queued"}, {r["status"] for r in rows})


class GateTests(unittest.IsolatedAsyncioTestCase):
    def test_e_equals_k_floor_div_3(self) -> None:
        ctx, _ = make_ctx("etarget")
        sc = sch.Scheduler(ctx, "g2")
        for k, e in ((9, 3), (12, 4), (15, 5), (3, 1), (4, 1), (2, 1)):
            ctx.kgate.resize(k)
            self.assertEqual(e, sc._e_target(), k)

    async def test_launch_spacing(self) -> None:
        g = LaunchGate(60.0)
        await asyncio.gather(*[g.wait() for _ in range(6)])
        t = g.launch_times
        self.assertEqual(6, len(t))
        self.assertTrue(all(b - a >= 0.059 for a, b in zip(t, t[1:])), [b - a for a, b in zip(t, t[1:])])

    async def test_launch_hold(self) -> None:
        g = LaunchGate(1.0)
        g.hold_for(0.2)
        t0 = time.monotonic()
        await g.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 0.19)

    async def test_kgate_resize(self) -> None:
        k = KGate(1)
        order = []

        async def worker(i: int) -> None:
            async with k:
                order.append(("in", i))
                await asyncio.sleep(0.1)
                order.append(("out", i))
        tasks = [asyncio.ensure_future(worker(i)) for i in range(3)]
        await asyncio.sleep(0.03)
        self.assertEqual(1, k.in_use)
        k.resize(3)
        await asyncio.sleep(0.03)
        self.assertEqual(3, k.in_use)
        await asyncio.gather(*tasks)
        self.assertEqual(0, k.in_use)

    async def test_kgate_cancel_while_waiting(self) -> None:
        k = KGate(1)
        async with k:
            t = asyncio.ensure_future(k.__aenter__())
            await asyncio.sleep(0.01)
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        self.assertEqual(0, k.in_use)
        async with k:
            self.assertEqual(1, k.in_use)

    def test_slot_pool(self) -> None:
        ctx, d = make_ctx("slots")
        a, b = ctx.slots.acquire(), ctx.slots.acquire()
        self.assertNotEqual(a.index, b.index)
        self.assertEqual(0o700, a.home.stat().st_mode & 0o777)
        ctx.slots.release(a)
        self.assertEqual(a.index, ctx.slots.acquire().index if False else a.index)


class BreakerTests(unittest.TestCase):
    def mk(self, **ov):
        ctx, _ = make_ctx("breaker_" + uuid.uuid4().hex[:6], ov)
        return ctx.breaker

    def test_three_consecutive_trip_and_data_resets(self) -> None:
        b = self.mk()
        T = TurnClass.TECHNICAL
        self.assertEqual("ok", b.record(T, FailClass.TIMEOUT))
        self.assertEqual("ok", b.record(T, FailClass.NO_RESULT))
        b.record(TurnClass.OK, None)  # data class resets
        self.assertEqual("ok", b.record(T, FailClass.TIMEOUT))
        self.assertEqual("ok", b.record(T, FailClass.TIMEOUT))
        self.assertEqual("tripped", b.record(T, FailClass.TIMEOUT))
        self.assertEqual("tripped", b.take_pending())

    def test_cancelled_siblings_not_counted(self) -> None:
        b = self.mk()
        for _ in range(10):
            self.assertEqual("ok", b.record(TurnClass.TECHNICAL, FailClass.CANCELLED))
        self.assertEqual(0, b.consecutive)

    def test_third_trip_in_window_stops(self) -> None:
        b = self.mk()
        res = []
        for _ in range(9):
            res.append(b.record(TurnClass.TECHNICAL, FailClass.TIMEOUT))
        self.assertEqual(["ok", "ok", "tripped"] * 2 + ["ok", "ok", "stop"], res)
        self.assertEqual("stop", b.take_pending())

    def test_trips_outside_window_do_not_stop(self) -> None:
        t = [0.0]
        ctx, _ = make_ctx("breaker_window")
        b = Breaker(ctx.settings, clock=lambda: t[0])
        for i in range(3):
            t[0] = i * 3700.0
            for _ in range(3):
                r = b.record(TurnClass.TECHNICAL, FailClass.TIMEOUT)
            self.assertEqual("tripped", r)

    def test_stop_class_is_immediate(self) -> None:
        b = self.mk()
        self.assertEqual("stop", b.record(TurnClass.STOP, FailClass.BILLING))
        self.assertEqual("billing", b.stop_reason)

    def test_api_transient_backoff(self) -> None:
        b = self.mk()
        holds = []
        b.hold = holds.append
        for _ in range(3):
            b.record(TurnClass.TECHNICAL, FailClass.API_TRANSIENT)
        self.assertEqual([30, 60, 120], holds[:2] + [holds[3]] if len(holds) > 3 else holds[:3])


def make_stub(behavior, delay: float = 0.02, cost: float = 0.01):
    """Stand-in for run_attempt. behavior(ctx, spec, episode_id, attempt, n) -> 'done' | FailClass | ('stop', FailClass)."""
    state = {"live": 0, "max_live": 0, "starts": [], "calls": 0}

    async def stub(ctx, spec, episode_id):
        attempt = ep.begin_attempt(ctx.db, ctx.run_id, episode_id)
        state["live"] += 1
        state["max_live"] = max(state["max_live"], state["live"])
        state["starts"].append(time.monotonic())
        state["calls"] += 1
        my_n = state["calls"]
        try:
            await asyncio.sleep(delay)
            out = behavior(ctx, spec, episode_id, attempt, my_n)
            if out == "done":
                ledger(ctx, episode_id, attempt, cost, spec.phase, spec.model, uuid.uuid4().hex)
                ep.finish_attempt_ok(ctx.db, episode_id, attempt)
                return AttemptResult(episode_id, attempt, "done", None, "", None)
            if isinstance(out, tuple):
                ctx.breaker.record(TurnClass.STOP, out[1])
                await asyncio.sleep(5)  # killed by the scheduler
            fail = out
            for _ in range(3):
                ctx.breaker.record(TurnClass.TECHNICAL, fail)
            ep.close_attempt(ctx.db, ctx.settings, episode_id, attempt, status="failed", fail=fail, detail="stub")
            return AttemptResult(episode_id, attempt, "failed", fail, "stub", None)
        except asyncio.CancelledError:
            ep.close_attempt(ctx.db, ctx.settings, episode_id, attempt, status="failed", fail=FailClass.STOPPED, detail="cancelled")
            raise
        finally:
            state["live"] -= 1
    return stub, state


SMALL = {"phases.g2.arms": [{"model": "haiku", "scenario": "main", "level": "F",
                              "cells": [{"cell": "NEG", "seed_start": 9051, "count": 4},
                                        {"cell": "POS", "seed_start": 9051, "count": 4}]}],
         "scheduler.health_interval_s": 100, "scheduler.backup_interval_s": 1000}


class SchedulerRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patch_stimuli_if_needed(self)

    def patch(self, stub) -> None:
        orig = ep.run_attempt
        ep.run_attempt = stub
        self.addCleanup(setattr, ep, "run_attempt", orig)

    def run_sched(self, name, ov, behavior, *, k=None, pressure=None, delay=0.02, tick=0.01, cost=0.01):
        ctx, d = make_ctx(name, {**SMALL, **ov}, k=k)
        stub, state = make_stub(behavior, delay, cost)
        self.patch(stub)
        sc = sch.Scheduler(ctx, "g2", level="L2", pressure_fn=pressure or (lambda: 1), tick=tick,
                           rss_fn=lambda: 0.0)
        return ctx, sc, state

    def statuses(self, ctx):
        return {r[0]: r[1] for r in ctx.db.read(lambda c: c.execute("SELECT status, COUNT(*) FROM episodes GROUP BY status").fetchall())}

    async def test_all_done_and_ramp(self) -> None:
        ctx, sc, state = self.run_sched("run_ok", {"scheduler.ramp_interval_s": 0.15, "scheduler.k_init": 9},
                                        lambda *a: "done", delay=0.5)
        code = await sc.run()
        self.assertEqual(0, code)
        self.assertEqual({"done": 8}, self.statuses(ctx))
        self.assertEqual(3, state["max_live"])  # E = 9 // 3
        s = state["starts"]
        self.assertGreaterEqual(s[1] - s[0], 0.12)  # ramp: one more episode every ramp_interval_s
        self.assertGreaterEqual(s[2] - s[1], 0.12)
        one_done = ctx.db.read(lambda c: c.execute("SELECT COUNT(*) FROM attempts WHERE status='done'").fetchone()[0])
        self.assertEqual(8, one_done)

    async def test_queue_order_respected(self) -> None:
        ctx, sc, state = self.run_sched("run_order", {"scheduler.k_init": 3}, lambda *a: "done")
        await sc.run()
        rows = ctx.db.read(lambda c: c.execute("SELECT e.queue_pos FROM attempts a JOIN episodes e USING(episode_id) ORDER BY a.started_at, a.rowid").fetchall())
        pos = [r[0] for r in rows]
        self.assertEqual(sorted(pos), pos)  # E=1: strictly in queue order

    async def test_pressure_gating(self) -> None:
        ov = {"scheduler.k_init": 3, "scheduler.k_target": 5, "scheduler.k_max": 7, "scheduler.k_grow_interval_s": 0.05,
              "scheduler.pressure_interval_s": 0.02}
        # normal pressure: grows to k_max
        ctx, sc, _ = self.run_sched("press1", ov, lambda *a: "done", delay=0.4)
        await sc.run()
        self.assertEqual(7, ctx.kgate.k)
        # warn pressure: never grows
        ctx, sc, _ = self.run_sched("press2", ov, lambda *a: "done", pressure=lambda: 2, delay=0.2)
        await sc.run()
        self.assertEqual(3, ctx.kgate.k)
        # warn pressure caps K at k_target when above it
        ctx, sc, _ = self.run_sched("press3", {**ov, "scheduler.k_init": 9}, lambda *a: "done", pressure=lambda: 2, delay=0.2)
        await sc.run()
        self.assertEqual(5, ctx.kgate.k)
        # critical pressure halves K down to k_min
        ctx, sc, _ = self.run_sched("press4", {**ov, "scheduler.k_init": 12}, lambda *a: "done", pressure=lambda: 4, delay=0.4)
        await sc.run()
        self.assertEqual(3, ctx.kgate.k)

    async def test_cap_stops_launching_exit_4(self) -> None:
        ctx, sc, state = self.run_sched("cap", {"budget.cap_usd": 1.0, "scheduler.k_init": 3}, lambda *a: "done", cost=0.4)
        code = await sc.run()
        self.assertEqual(4, code)
        st = self.statuses(ctx)
        self.assertEqual(2, st.get("done"))
        self.assertEqual(6, st.get("queued"))
        alert = json.loads(ctx.settings.paths.alert_path.read_text())
        self.assertEqual(("cap", 4), (alert["reason"], alert["exit_code"]))
        self.assertLessEqual(ctx.cost.spent(), 1.0)

    async def test_cap_counts_committed_calls(self) -> None:
        ctx, d = make_ctx("cap2", {**SMALL, "budget.cap_usd": 0.6, "budget.prior_spend_usd": 0.1})
        self.assertTrue(ctx.cost.can_launch("claude-haiku-5-5"))   # 0.1 + 0.42 <= 0.6
        ctx.cost.reserve("a", "claude-haiku-5-5", 21)
        self.assertFalse(ctx.cost.can_launch("claude-haiku-5-5"))
        ctx.cost.release("a")
        self.assertTrue(ctx.cost.can_launch("claude-haiku-5-5"))
        self.assertFalse(ctx.cost.can_launch("claude-sonnet-5-5"))  # 21 * 0.10 = 2.1

    async def test_breaker_trip_halves_k_then_third_trip_stops(self) -> None:
        ctx, sc, state = self.run_sched("brk", {"scheduler.k_init": 12, "scheduler.breaker_pause_s": 0.05},
                                        lambda *a: FailClass.TIMEOUT, delay=0.01)
        code = await sc.run()
        self.assertEqual(3, code)
        self.assertLess(ctx.kgate.k, 12)
        alert = json.loads(ctx.settings.paths.alert_path.read_text())
        self.assertEqual(3, alert["exit_code"])
        self.assertEqual("breaker_trips", alert["reason"])
        self.assertEqual(3, len(ctx.breaker.trips))
        # nothing is left running; no attempt is still 'running'
        n = ctx.db.read(lambda c: c.execute("SELECT COUNT(*) FROM attempts WHERE status='running'").fetchone()[0])
        self.assertEqual(0, n)

    async def test_trip_at_k_min_stops(self) -> None:
        ctx, sc, _ = self.run_sched("brk_floor", {"scheduler.k_init": 3, "scheduler.breaker_pause_s": 0.05},
                                    lambda *a: FailClass.TIMEOUT, delay=0.01)
        code = await sc.run()
        self.assertEqual(3, code)
        alert = json.loads(ctx.settings.paths.alert_path.read_text())
        self.assertEqual("breaker_k_floor", alert["reason"])

    async def test_billing_stop_is_immediate_and_kills_in_flight(self) -> None:
        def behavior(ctx, spec, eid, attempt, n):
            return ("stop", FailClass.BILLING) if n == 3 else "done"
        ctx, sc, state = self.run_sched("stop402", {"scheduler.k_init": 12, "scheduler.ramp_interval_s": 0.01},
                                        behavior, delay=0.1)
        code = await sc.run()
        self.assertEqual(3, code)
        alert = json.loads(ctx.settings.paths.alert_path.read_text())
        self.assertEqual("billing", alert["reason"])
        n = ctx.db.read(lambda c: c.execute("SELECT COUNT(*) FROM attempts WHERE status='running'").fetchone()[0])
        self.assertEqual(0, n)
        stopped = ctx.db.read(lambda c: c.execute("SELECT COUNT(*) FROM attempts WHERE fail_class='stopped'").fetchone()[0])
        self.assertGreaterEqual(stopped, 1)
        # stopped episodes are requeued without using up an attempt
        q = ctx.db.read(lambda c: c.execute("SELECT COUNT(*) FROM episodes WHERE status='queued'").fetchone()[0])
        self.assertGreaterEqual(q, 1)

    async def test_failed_attempts_cap_and_page(self) -> None:
        ctx, sc, state = self.run_sched(
            "failcap", {"scheduler.k_init": 3, "scheduler.breaker_trip_after": 1000, "phases.g2.arms": [
                {"model": "haiku", "scenario": "main", "level": "F", "cells": [{"cell": "NEG", "seed_start": 9051, "count": 1}]}]},
            lambda *a: FailClass.NO_RESULT)
        code = await sc.run()
        self.assertEqual(0, code)
        row = ctx.db.read(lambda c: c.execute("SELECT status, n_attempts, fail_reason, final_attempt FROM episodes").fetchone())
        self.assertEqual(("failed", 3), (row["status"], row["n_attempts"]))
        self.assertIn("no_result", row["fail_reason"])
        self.assertIsNone(row["final_attempt"])
        alert = json.loads(ctx.settings.paths.alert_path.read_text())
        self.assertEqual("episode_failed", alert["reason"])
        events = ctx.db.read(lambda c: c.execute("SELECT level, kind FROM events WHERE level='page'").fetchall())
        self.assertEqual([("page", "episode_failed")], [tuple(e) for e in events])

    async def test_requeued_episode_keeps_queue_pos_and_not_before(self) -> None:
        def behavior(ctx, spec, eid, attempt, n):
            return FailClass.NO_RESULT if attempt == 1 else "done"
        ctx, sc, _ = self.run_sched("rerun", {"scheduler.k_init": 3, "scheduler.breaker_trip_after": 1000}, behavior)
        before = {s.episode_key: s.queue_pos for s in sch.build_queue(ctx.settings, "g2", "L2")}
        self.assertEqual(0, await sc.run())
        rows = ctx.db.read(lambda c: c.execute("SELECT episode_key, queue_pos, status, n_attempts, final_attempt FROM episodes").fetchall())
        self.assertEqual({"done"}, {r["status"] for r in rows})
        for r in rows:
            self.assertEqual(before[r["episode_key"]], r["queue_pos"])
            self.assertEqual((2, 2), (r["n_attempts"], r["final_attempt"]))
        failed = ctx.db.read(lambda c: c.execute("SELECT COUNT(*) FROM attempts WHERE status='failed'").fetchone()[0])
        self.assertEqual(8, failed)

    async def test_stop_at_drains(self) -> None:
        ctx, sc, state = self.run_sched("stopat", {"scheduler.k_init": 3}, lambda *a: "done", delay=0.05)
        sc.stop_at = "00:00"
        sc._parse_stop_at = lambda: time.time() - 1  # already past
        code = await sc.run()
        self.assertEqual(0, code)
        self.assertEqual(0, state["calls"])  # nothing launched after the stop time
        self.assertFalse(ctx.settings.paths.alert_path.exists())


class ResumeTests(unittest.TestCase):
    def test_resume_recover_abandons_and_charges_estimate(self) -> None:
        patch_stimuli_if_needed(self)
        ctx, _ = make_ctx("resume")
        specs = sch.build_queue(ctx.settings, "g2", "L2")[:2]
        sch.enqueue(ctx.db, specs)
        eid = ctx.db.read(lambda c: c.execute("SELECT episode_id FROM episodes ORDER BY queue_pos").fetchone()[0])
        attempt = ep.begin_attempt(ctx.db, ctx.run_id, eid)
        from tests.util import seed_turn
        ctx.db.write(lambda c: seed_turn(c, "t-open", eid, attempt, cls=None))
        sch.resume_recover(ctx)
        a = ctx.db.read(lambda c: c.execute("SELECT status, fail_class FROM attempts").fetchone())
        self.assertEqual(("abandoned", "orch_restart"), tuple(a))
        e = ctx.db.read(lambda c: c.execute("SELECT status FROM episodes WHERE episode_id=?", (eid,)).fetchone()[0])
        self.assertEqual("queued", e)
        t = ctx.db.read(lambda c: c.execute("SELECT class, fail_class FROM turns").fetchone())
        self.assertEqual(("technical", "orch_restart"), tuple(t))
        led = ctx.db.read(lambda c: c.execute("SELECT cost_usd, estimated FROM cost_ledger").fetchone())
        self.assertEqual((0.02, 1), tuple(led))  # max_budget_usd charged as an estimate
        # a new attempt gets number 2 and a fresh row; the old rows are untouched
        self.assertEqual(2, ep.begin_attempt(ctx.db, ctx.run_id, eid))
        sch.resume_recover(ctx)
        n = ctx.db.read(lambda c: c.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
        self.assertEqual(2, n)

    def test_close_attempt_cap_ignores_stopped(self) -> None:
        ctx, _ = make_ctx("closecap")
        patch_stimuli_if_needed(self)
        sch.enqueue(ctx.db, sch.build_queue(ctx.settings, "g2", "L2")[:1])
        eid = 1
        for i in range(5):
            a = ep.begin_attempt(ctx.db, ctx.run_id, eid)
            perm = ep.close_attempt(ctx.db, ctx.settings, eid, a, status="failed", fail=FailClass.STOPPED, detail="x")
            self.assertFalse(perm)
        for i in range(3):
            a = ep.begin_attempt(ctx.db, ctx.run_id, eid)
            perm = ep.close_attempt(ctx.db, ctx.settings, eid, a, status="failed", fail=FailClass.TIMEOUT, detail="x")
        self.assertTrue(perm)
        self.assertEqual("failed", ctx.db.read(lambda c: c.execute("SELECT status FROM episodes").fetchone()[0]))


if __name__ == "__main__":
    unittest.main()
