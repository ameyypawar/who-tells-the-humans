"""Shared WP1 test helpers (no tests in this module).

States are seeded with plain SQL for everything a witness or the claimant would have written, so the renderer
tests do not depend on WP2's handlers. Only T1.6 goes through handlers.notify_ops.

Private text: failures here are always reported through prereg.mask_private; never print an unmasked render
of a CLAIM, POLICY, RECEIPT, NO-RECEIPT, FORGED or POS prompt for seeds where (seed - 1) mod 3 != 0.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path

from tests.util import REPO, make_db, seed_attempt, seed_episode, seed_run, seed_turn
from wtth import prereg, stimuli
from wtth.config import load_settings
from wtth.db import Db
from wtth.textnorm import normalize_post
from wtth.types import EpisodeSpec, EpisodeState, World

GOLDEN = REPO / "tests" / "golden"
PROMPTS = REPO / "prereg" / "prompts"
MODEL = "claude-haiku-5-5"


@lru_cache(maxsize=1)
def settings():
    return load_settings()


@lru_cache(maxsize=2)
def texts(scenario: str = "main") -> prereg.ScenarioTexts:
    return prereg.load_scenario_texts(scenario, settings())


@lru_cache(maxsize=1)
def private() -> dict[str, str]:
    return prereg.load_private(settings())


def read_golden(name: str) -> str:
    """Golden file content without the single trailing newline an editor adds (prompts have none)."""
    s = (GOLDEN / name).read_text(encoding="utf-8")
    return s[:-1] if s.endswith("\n") else s


def spec_for(cell: str, seed: int = 1, level: str = "L2", scenario: str = "main") -> EpisodeSpec:
    s = settings()
    prefix = s.scenarios[scenario].handle_prefix
    return stimuli.episode_spec("test", scenario, MODEL, s.cells[cell], level, seed, 1, prefix)


class Seeder:
    """One temp DB, one episode, attempt 1; helper methods write the rows a real run would."""

    def __init__(self, name: str, cell: str, seed: int = 1, level: str = "L2", scenario: str = "main") -> None:
        self.spec = spec_for(cell, seed, level, scenario)
        self.path, self.db = make_db(name)
        self.attempt = 1

        def fn(conn):
            seed_run(conn)
            ep = seed_episode(conn, key=self.spec.episode_key, scenario=scenario, cell=cell, level=level, seed=seed,
                              world=self.spec.cell.world.value, status="running")
            seed_attempt(conn, ep, 1)
            return ep
        self.episode_id = self.db.write(fn)
        self._turn = 0

    # -- scripted --
    def inject(self, upto: int, *, with_note: bool = False) -> "Seeder":
        """Scripted posts of rounds 1..upto (+ C's note by SQL when the cell has one and round 2 is reached)."""
        for r in range(1, upto + 1):
            self._posts_only(r)
            if r == 2 and self.spec.cell.c_note:
                n = stimuli.scripted_note(self.spec, texts(self.spec.scenario))
                self.note(n.round, n.sender, 0, n.clock, n.subject, n.message, source="script",
                          visible_from=n.visible_from_round)
        return self

    def _posts_only(self, r: int) -> None:
        # inject_round files C's note through handlers for note cells; emulate its post half only.
        t = texts(self.spec.scenario)
        posts = stimuli.scripted_posts(self.spec, t, r)

        def fn(conn):
            for p in posts:
                conn.execute(
                    "INSERT OR IGNORE INTO posts(episode_id, attempt, round, sender, slot, clock, clock_min, kind, "
                    "line_id, is_private, text, visible_from_round, created_at) "
                    "VALUES (?,?,?,?,NULL,?,?,'scripted',?,?,?,?,?)",
                    (self.episode_id, self.attempt, p.round, p.sender, p.clock, stimuli.clock_min(p.clock),
                     p.line_id, int(p.is_private), p.text, stimuli.scripted_visible_from(r), time.time()))
        self.db.write(fn)

    # -- witness rows --
    def _tool_call(self, conn, rnd: int, slot: int, sender: str, tool: str) -> int:
        self._turn += 1
        tid = f"t{self._turn}"
        seed_turn(conn, tid, self.episode_id, self.attempt, round=rnd, slot=slot, handle=sender)
        cur = conn.execute(
            "INSERT INTO tool_calls(turn_id, episode_id, attempt, round, slot, sender, tool, args_json, accepted, "
            "created_at) VALUES (?,?,?,?,?,?,?,'{}',1,?)",
            (tid, self.episode_id, self.attempt, rnd, slot, sender, tool, time.time()))
        return cur.lastrowid

    def reviews(self, rnd: int, slot: int, entries: list[list[str]], used: bool = True) -> "Seeder":
        sender = self.spec.handles[slot - 1]

        def fn(conn):
            tc = self._tool_call(conn, rnd, slot, sender, "submit_reviews")
            conn.execute(
                "INSERT INTO reviews(tool_call_id, episode_id, attempt, round, slot, sender, entries_json, n_entries, "
                "used, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tc, self.episode_id, self.attempt, rnd, slot, sender, json.dumps(entries), len(entries),
                 int(used), time.time()))
        self.db.write(fn)
        return self

    def post(self, rnd: int, slot: int, raw: str) -> "Seeder":
        """A live post as the handler stores it (normalize_post), stamped T_r + 4*slot, visible next round."""
        sender = self.spec.handles[slot - 1]
        text, _ = normalize_post(raw)
        clock = stimuli.witness_stamp(rnd, slot)

        def fn(conn):
            tc = self._tool_call(conn, rnd, slot, sender, "post_to_board")
            conn.execute(
                "INSERT INTO posts(episode_id, attempt, round, sender, slot, clock, clock_min, kind, line_id, "
                "is_private, text, text_raw, visible_from_round, turn_id, tool_call_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,'live',NULL,0,?,?,?,?,?,?)",
                (self.episode_id, self.attempt, rnd, sender, slot, clock, stimuli.clock_min(clock), text, raw,
                 stimuli.witness_visible_from(rnd), f"t{self._turn}", tc, time.time()))
        self.db.write(fn)
        return self

    def note(self, rnd: int, sender: str, slot: int, clock: str, subject: str, message: str, *,
             source: str = "tool", visible_from: int | None = None) -> int:
        """A note and, in world R1, its log row (exactly what handlers.notify_ops writes)."""
        vis = stimuli.witness_visible_from(rnd) if visible_from is None else visible_from
        r1 = self.spec.cell.world is World.R1

        def fn(conn):
            tc = None if source == "script" else self._tool_call(conn, rnd, slot, sender, "notify_ops")
            cur = conn.execute(
                "INSERT INTO notes(episode_id, attempt, round, sender, slot, clock, clock_min, subject, message, "
                "source, tool_call_id, visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.episode_id, self.attempt, rnd, sender, slot, clock, stimuli.clock_min(clock), subject, message,
                 source, tc, vis, time.time()))
            if r1:
                conn.execute(
                    "INSERT INTO escalation_log(note_id, episode_id, attempt, sender, clock, clock_min, "
                    "subject_display, visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (cur.lastrowid, self.episode_id, self.attempt, sender, clock, stimuli.clock_min(clock), subject,
                     vis, time.time()))
            return cur.lastrowid
        return self.db.write(fn)

    def witness_note(self, rnd: int, slot: int, subject: str, message: str) -> int:
        return self.note(rnd, self.spec.handles[slot - 1], slot, stimuli.witness_stamp(rnd, slot), subject, message)

    def d1(self, slot: int, answer: str) -> "Seeder":
        def fn(conn):
            self._turn += 1
            tid = f"d{self._turn}"
            seed_turn(conn, tid, self.episode_id, self.attempt, turn_type="d1", round=None, slot=slot,
                      handle=self.spec.handles[slot - 1])
            conn.execute(
                "INSERT INTO debrief_answers(episode_id, attempt, slot, handle, question, answer_text, turn_id, "
                "created_at) VALUES (?,?,?,?,'D1',?,?,?)",
                (self.episode_id, self.attempt, slot, self.spec.handles[slot - 1], answer, tid, time.time()))
        self.db.write(fn)
        return self

    def state(self) -> EpisodeState:
        return self.db.read(lambda c: stimuli.load_state(c, self.spec, self.episode_id, self.attempt))


BASE_REVIEWS_R1 = [["T-1101", "approve", "ok"], ["T-1102", "approve", "ok"], ["T-1103", "decline", "late"]]


def base_state(name: str, cell: str, seed: int = 1, level: str = "L2", upto: int = 2) -> Seeder:
    """The golden base state: scripted rounds 1..upto, slot 1's round-1 reviews, no round-1 posts."""
    return Seeder(name, cell, seed, level).inject(upto).reviews(1, 1, BASE_REVIEWS_R1)
