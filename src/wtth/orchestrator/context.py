"""RunContext and the shared run-time gates (research/41 sections 2.7, research/43 B1-B5). Owner: WP4."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from wtth.types import FailClass, SlotDirs, TurnClass

# Technical fail classes that count toward the breaker (41 section 2.6 rows 2-5, 8-11, 13, 14, 16).
ROOT_CAUSE = frozenset({
    FailClass.TIMEOUT, FailClass.SERVER_FAULT, FailClass.NO_RESULT, FailClass.EXIT, FailClass.CORRUPT_STREAM,
    FailClass.API_TRANSIENT, FailClass.INIT_MISMATCH, FailClass.MODEL_MISMATCH, FailClass.PERMISSION_DENIED,
    FailClass.MCP_TRANSPORT, FailClass.RECONCILE, FailClass.API_OTHER})
# Technical outcomes that say nothing about the environment (neither count nor reset).
NEUTRAL = frozenset({FailClass.CANCELLED, FailClass.ORCH_RESTART, FailClass.STOPPED})


@dataclass
class RunContext:
    """The API key is held as `_api_key` (excluded from repr). WP4 may add behaviour, not change these fields."""

    settings: Any
    db: Any
    run_id: str
    mode: str
    toolsets: dict
    texts: dict
    private: dict
    slots: "SlotPool"
    kgate: "KGate"
    launch: "LaunchGate"
    cost: "CostTracker"
    breaker: "Breaker"
    expected_init: dict
    allow: dict
    _api_key: str = field(default="", repr=False)


class SlotPool:
    """Slot directories <root>/wNN/{home,config,tmp} (mode 0700); one process at a time per slot."""

    def __init__(self, root: Path, n: int = 15) -> None:
        self.root = Path(root)
        self.n = n
        self._free = deque(range(n))
        self._busy: set[int] = set()

    def ensure(self) -> None:
        for i in range(self.n):
            self._dirs(i, create=True)

    def _dirs(self, i: int, create: bool = False) -> SlotDirs:
        root = self.root / f"w{i:02d}"
        d = SlotDirs(i, root, root / "home", root / "config", root / "tmp")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            for p in (d.root, d.home, d.config, d.tmp):
                p.mkdir(parents=True, exist_ok=True)
                os.chmod(p, 0o700)
        return d

    def acquire(self) -> SlotDirs:
        if not self._free:
            raise RuntimeError("no free slot (K exceeds the slot count)")
        i = self._free.popleft()
        self._busy.add(i)
        return self._dirs(i, create=True)

    def release(self, slot: SlotDirs) -> None:
        if slot.index in self._busy:
            self._busy.discard(slot.index)
            self._free.append(slot.index)

    @property
    def in_use(self) -> int:
        return len(self._busy)


class KGate:
    """Resizable semaphore over CLI processes. Use as `async with kgate:`."""

    def __init__(self, k: int) -> None:
        self.k = k
        self.in_use = 0
        self._waiters: deque[asyncio.Future] = deque()

    def resize(self, k: int) -> None:
        self.k = max(1, int(k))
        self._wake()

    def _wake(self) -> None:
        while self._waiters and self.in_use < self.k:
            fut = self._waiters.popleft()
            if not fut.done():
                self.in_use += 1
                fut.set_result(None)

    async def __aenter__(self) -> None:
        if self.in_use < self.k and not self._waiters:
            self.in_use += 1
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self.in_use -= 1  # permit was handed over; give it back
                self._wake()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.in_use -= 1
        self._wake()


class LaunchGate:
    """Every process launch waits until >= gap_ms after the previous one (global), and past hold_until."""

    def __init__(self, gap_ms: float = 150.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.gap_s = gap_ms / 1000.0
        self.clock = clock
        self.last = -1e18
        self.hold_until = 0.0
        self.launch_times: list[float] = []

    def hold_for(self, seconds: float) -> None:
        self.hold_until = max(self.hold_until, self.clock() + seconds)

    async def wait(self) -> None:
        while True:
            now = self.clock()
            t = max(now, self.last + self.gap_s, self.hold_until)
            if t <= now:
                self.last = now
                self.launch_times.append(now)
                return
            await asyncio.sleep(t - now)


class CostTracker:
    """Global ledger (B5): spent = prior + SUM(cost_ledger) over all phases and attempts (failed ones included)."""

    def __init__(self, settings: Any, db: Any) -> None:
        self.settings = settings
        self.db = db
        self._res: dict[str, list] = {}  # key -> [model, calls_left]
        self.warned = False

    def spent(self) -> float:
        row = self.db.read(lambda c: c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM cost_ledger").fetchone())
        return self.settings.budget.prior_spend_usd + float(row[0])

    def phase_spent(self, phase: str) -> float:
        row = self.db.read(lambda c: c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM cost_ledger WHERE phase=?", (phase,)).fetchone())
        return float(row[0])

    def committed(self) -> float:
        return sum(self.settings.models.budget_for(m) * n for m, n in self._res.values())

    def reserve(self, key: str, model: str, calls: int = 21) -> None:
        self._res[key] = [model, calls]

    def turn_done(self, key: str) -> None:
        if key in self._res and self._res[key][1] > 0:
            self._res[key][1] -= 1

    def release(self, key: str) -> None:
        self._res.pop(key, None)

    def can_launch(self, model: str) -> bool:
        b = self.settings.budget
        need = b.reserve_calls * self.settings.models.budget_for(model)
        return self.spent() + self.committed() + need <= b.cap_usd

    def over_warn(self) -> bool:
        return self.spent() >= self.settings.budget.warn_usd

    def charge(self, turn_id: str, phase: str, model: str, cost: float, estimated: bool) -> None:
        """Idempotent insert into cost_ledger (run_turn normally writes the row itself)."""
        run_id = getattr(self, "run_id", "") or ""

        def fn(c: sqlite3.Connection) -> None:
            c.execute("INSERT OR IGNORE INTO cost_ledger(turn_id, run_id, phase, model, cost_usd, estimated, ts) "
                      "VALUES (?,?,?,?,?,?,?)", (turn_id, run_id, phase, model, float(cost), int(estimated), time.time()))
        self.db.write(fn)


class Breaker:
    """Consecutive root-cause technical failures (B3). Pending signals are polled by the scheduler."""

    def __init__(self, settings: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self.s = settings.scheduler
        self.clock = clock
        self.consecutive = 0
        self.trips: list[float] = []
        self.pending: str | None = None      # 'tripped' | 'stop'
        self.stop_reason: str | None = None
        self.api_n = 0
        self.reconcile_n = 0
        self.notices: list[str] = []         # page-only conditions (no STOP), drained by the scheduler
        self.hold: Callable[[float], None] | None = None  # wired to LaunchGate.hold_for

    def record(self, cls: TurnClass, fail: FailClass | None) -> Literal["ok", "tripped", "stop"]:
        if cls == TurnClass.STOP:
            self.pending, self.stop_reason = "stop", fail.value if fail else "stop"
            return "stop"
        if cls == TurnClass.TECHNICAL:
            if fail == FailClass.RECONCILE:
                self.reconcile_n += 1
                if self.reconcile_n == self.s.reconcile_page_after:
                    self.notices.append("reconcile_repeated")
            elif fail not in NEUTRAL:
                self.reconcile_n = 0
            if fail in NEUTRAL:
                return "ok"
            if fail == FailClass.API_TRANSIENT:
                self.api_n += 1
                if self.hold:
                    self.hold(min(self.s.api_backoff_cap_s, self.s.api_backoff_base_s * 2 ** (self.api_n - 1)))
            if fail in ROOT_CAUSE or fail is None:
                self.consecutive += 1
                if self.consecutive >= self.s.breaker_trip_after:
                    self.consecutive = 0
                    now = self.clock()
                    self.trips.append(now)
                    recent = [t for t in self.trips if now - t <= self.s.breaker_window_min * 60]
                    if len(recent) >= self.s.breaker_stop_trips:
                        self.pending, self.stop_reason = "stop", "breaker_trips"
                        return "stop"
                    if self.pending != "stop":
                        self.pending = "tripped"
                    if self.hold:
                        self.hold(self.s.breaker_pause_s)
                    return "tripped"
            return "ok"
        # data class (ok, refusal, max_turns, max_budget, probe) resets
        self.consecutive = 0
        self.reconcile_n = 0
        self.api_n = 0
        return "ok"

    def request_stop(self, reason: str) -> None:
        self.pending, self.stop_reason = "stop", reason

    def take_pending(self) -> str | None:
        p, self.pending = self.pending, None
        return p

    def seconds_since_trip(self) -> float:
        return (self.clock() - self.trips[-1]) if self.trips else 1e18
