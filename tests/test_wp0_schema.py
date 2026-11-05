"""T0.1: schema, triggers, views."""
from __future__ import annotations

import sqlite3
import unittest

from tests.util import (make_db, seed_attempt, seed_episode, seed_run, seed_running_episode, seed_turn,
                        tmp_dir)
from wtth import SCHEMA_VERSION
from wtth.db import Db, SchemaMismatch, WriteFailed, backup, check_schema, create_db

TABLES = ("runs", "episodes", "attempts", "turns", "exposures", "tool_calls", "posts", "notes", "escalation_log",
          "reviews", "stream_tool_uses", "debrief_answers", "cost_ledger", "server_starts", "fallback_rows",
          "events", "gate_log", "exposure_checks")
INDEXES = ("ux_attempt_done", "ux_attempt_running", "ux_scripted_post", "ux_script_note", "ix_ep_queue",
           "ix_posts_ep", "ix_notes_ep", "ix_log_ep", "ix_tc_turn", "ix_turns_ep")
VIEWS = ("v_log_numbered", "v_final", "v_witnesses_final", "v_notes_final", "v_posts_final", "v_log_final",
         "v_tool_calls_final", "v_reviews_final", "v_turns_final", "v_exposures_final", "v_debrief_final",
         "v_stream_tool_uses_final", "v_attempt_counts", "v_turn_classes", "v_cost")
NO_UPDATE = ("posts", "tool_calls", "notes", "escalation_log", "reviews", "stream_tool_uses", "debrief_answers",
             "cost_ledger", "server_starts", "fallback_rows", "events", "exposure_checks")


def names(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def insert_note(conn: sqlite3.Connection, ep: int, sender: str = "rd-14", clock_min: int = 1284,
                round: int = 2, source: str = "tool", kw: str | None = None) -> int:
    return conn.execute(
        "INSERT INTO notes(episode_id, attempt, round, sender, slot, clock, clock_min, subject, message, source, "
        "visible_from_round, created_at, kw_hits_json) VALUES (?,1,?,?,1,'21:24',?,'s','m',?,3,0,?)",
        (ep, round, sender, clock_min, source, kw)).lastrowid


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.db = make_db(self._testMethodName)
        self.addCleanup(self.db.close)

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None)
        c.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(c.close)
        return c

    def test_objects_exist(self) -> None:
        c = self.conn()
        self.assertEqual(set(TABLES), names(c, "table"))
        self.assertTrue(set(INDEXES) <= names(c, "index"))
        self.assertEqual(set(VIEWS), names(c, "view"))
        triggers = names(c, "trigger")
        for t in TABLES:
            self.assertIn(f"trg_{t}_no_delete", triggers)
        for t in NO_UPDATE:
            self.assertIn(f"trg_{t}_no_update", triggers)

    def test_pragmas(self) -> None:
        c = self.conn()
        self.assertEqual("wal", c.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(SCHEMA_VERSION, c.execute("PRAGMA user_version").fetchone()[0])
        check_schema(c)
        c.execute("PRAGMA user_version=7")
        with self.assertRaises(SchemaMismatch):
            check_schema(c)

    def test_create_db_idempotent(self) -> None:
        ep, _ = seed_running_episode(self.db)
        create_db(self.path)  # second call must not touch existing data
        n = self.db.read(lambda c: c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
        self.assertEqual(1, n)
        self.assertEqual(1, ep)

    def test_db_connection_settings(self) -> None:
        self.db.read(lambda c: self.assertEqual(1, c.execute("PRAGMA foreign_keys").fetchone()[0]))
        self.db.write(lambda c: self.assertEqual(1, c.execute("PRAGMA synchronous").fetchone()[0]))  # NORMAL=1

    def test_delete_aborts_everywhere(self) -> None:
        ep, _ = seed_running_episode(self.db)

        def populate(c: sqlite3.Connection) -> None:
            seed_turn(c, "t1", ep)
            c.execute("INSERT INTO posts(episode_id, attempt, round, sender, slot, clock, clock_min, kind, text, "
                      "visible_from_round, created_at) VALUES (?,1,1,'rd-04',NULL,'20:35',1235,'scripted','x',1,0)", (ep,))
            nid = insert_note(c, ep)
            c.execute("INSERT INTO escalation_log(note_id, episode_id, attempt, sender, clock, clock_min, "
                      "subject_display, visible_from_round, created_at) VALUES (?,?,1,'rd-11','21:24',1284,'s',2,0)", (nid, ep))
            c.execute("INSERT INTO events(ts, level, kind) VALUES (0,'info','x')")
        self.db.write(populate)
        for table in ("runs", "episodes", "attempts", "turns", "posts", "notes", "escalation_log", "events"):
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.DatabaseError) as cm:
                    self.db.write(lambda c, t=table: c.execute(f"DELETE FROM {t}"))
                self.assertIn("wtth:", str(cm.exception))

    def test_update_aborts_on_immutable_tables(self) -> None:
        ep, _ = seed_running_episode(self.db)
        self.db.write(lambda c: insert_note(c, ep))
        with self.assertRaises(sqlite3.DatabaseError) as cm:
            self.db.write(lambda c: c.execute("UPDATE notes SET subject='changed'"))
        self.assertIn("wtth:", str(cm.exception))

    def test_closed_attempt_cannot_reopen(self) -> None:
        ep, _ = seed_running_episode(self.db)
        self.db.write(lambda c: c.execute("UPDATE attempts SET status='failed', finished_at=1"))
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.write(lambda c: c.execute("UPDATE attempts SET status='running' WHERE episode_id=?", (ep,)))
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.write(lambda c: c.execute("UPDATE attempts SET status='done' WHERE episode_id=?", (ep,)))

    def test_second_done_attempt_rejected(self) -> None:
        ep, _ = seed_running_episode(self.db)
        self.db.write(lambda c: c.execute("UPDATE attempts SET status='done', finished_at=1"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.write(lambda c: seed_attempt(c, ep, 2, status="done"))

    def test_second_running_attempt_rejected(self) -> None:
        ep, _ = seed_running_episode(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.write(lambda c: seed_attempt(c, ep, 2, status="running"))

    def test_episode_state_machine(self) -> None:
        ep, _ = seed_running_episode(self.db)
        self.db.write(lambda c: c.execute("UPDATE episodes SET status='queued' WHERE episode_id=?", (ep,)))
        with self.assertRaises(sqlite3.DatabaseError):  # queued -> done is illegal
            self.db.write(lambda c: c.execute("UPDATE episodes SET status='done' WHERE episode_id=?", (ep,)))
        self.db.write(lambda c: c.execute("UPDATE episodes SET status='running' WHERE episode_id=?", (ep,)))
        self.db.write(lambda c: c.execute("UPDATE episodes SET status='failed', updated_at=100 WHERE episode_id=?", (ep,)))
        with self.assertRaises(sqlite3.DatabaseError):  # failed -> queued needs a manual_requeue event
            self.db.write(lambda c: c.execute("UPDATE episodes SET status='queued' WHERE episode_id=?", (ep,)))

        def requeue(c: sqlite3.Connection) -> None:
            c.execute("INSERT INTO events(ts, level, kind, episode_id) VALUES (200,'info','manual_requeue',?)", (ep,))
            c.execute("UPDATE episodes SET status='queued', updated_at=200 WHERE episode_id=?", (ep,))
        self.db.write(requeue)
        self.db.write(lambda c: c.execute("UPDATE episodes SET status='running' WHERE episode_id=?", (ep,)))
        self.db.write(lambda c: c.execute("UPDATE episodes SET status='done', final_attempt=1 WHERE episode_id=?", (ep,)))
        with self.assertRaises(sqlite3.DatabaseError):  # done is terminal
            self.db.write(lambda c: c.execute("UPDATE episodes SET fail_reason='x' WHERE episode_id=?", (ep,)))

    def test_exposures_update_guard(self) -> None:
        ep, _ = seed_running_episode(self.db)

        def populate(c: sqlite3.Connection) -> None:
            seed_turn(c, "t1", ep)
            c.execute("INSERT INTO exposures(turn_id, system_path, system_sha, tools_sha, prompt_sha, prompt_text, "
                      "visible_post_ids_json, visible_note_ids_json, visible_log_ids_json) "
                      "VALUES ('t1','p','s','t','h','text','[]','[]','[]')")
        self.db.write(populate)
        self.db.write(lambda c: c.execute("UPDATE exposures SET request_ids_json='[]', session_id='s1', init_uuid='u'"))
        with self.assertRaises(sqlite3.DatabaseError):  # prompt_text is immutable
            self.db.write(lambda c: c.execute("UPDATE exposures SET prompt_text='other'"))
        with self.assertRaises(sqlite3.DatabaseError):  # already filled, cannot change
            self.db.write(lambda c: c.execute("UPDATE exposures SET session_id='s2'"))

    def test_turn_try_no_and_class_check(self) -> None:
        ep, _ = seed_running_episode(self.db)
        self.db.write(lambda c: seed_turn(c, "t1", ep))
        self.assertEqual(1, self.db.read(lambda c: c.execute("SELECT try_no FROM turns").fetchone()[0]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.write(lambda c: seed_turn(c, "t2", ep, cls="bogus"))

    def test_v_log_numbered_orders_by_clock_then_note(self) -> None:
        ep, _ = seed_running_episode(self.db)

        def populate(c: sqlite3.Connection) -> None:
            for sender, cm in (("rd-14", 1294), ("rd-11", 1284), ("rd-21", 1288)):  # inserted out of clock order
                nid = insert_note(c, ep, sender=sender, clock_min=cm)
                c.execute("INSERT INTO escalation_log(note_id, episode_id, attempt, sender, clock, clock_min, "
                          "subject_display, visible_from_round, created_at) VALUES (?,?,1,?,'x',?,'s',3,0)",
                          (nid, ep, sender, cm))
        self.db.write(populate)
        rows = self.db.read(lambda c: c.execute("SELECT sender, ref_no FROM v_log_numbered ORDER BY ref_no").fetchall())
        self.assertEqual([("rd-11", 200), ("rd-21", 201), ("rd-14", 202)], [tuple(r) for r in rows])

    def test_final_views_only_show_done_episodes(self) -> None:
        def populate(c: sqlite3.Connection) -> None:
            seed_run(c)
            a = seed_episode(c, "k/a", status="running", seed=1, queue_pos=1)
            b = seed_episode(c, "k/b", status="running", seed=2, queue_pos=2)
            seed_attempt(c, a)
            seed_attempt(c, b)
            insert_note(c, a)
            insert_note(c, b)
            c.execute("UPDATE attempts SET status='done', finished_at=1 WHERE episode_id=?", (a,))
            c.execute("UPDATE episodes SET status='done', final_attempt=1 WHERE episode_id=?", (a,))
        self.db.write(populate)
        n = self.db.read(lambda c: c.execute("SELECT COUNT(*) FROM v_notes_final").fetchone()[0])
        self.assertEqual(1, n)
        w = self.db.read(lambda c: c.execute("SELECT slot, handle FROM v_witnesses_final ORDER BY slot").fetchall())
        self.assertEqual([(1, "rd-14"), (2, "rd-21"), (3, "rd-26")], [tuple(r) for r in w])

    def test_v_turns_final_picks_last_successful_try(self) -> None:
        ep, _ = seed_running_episode(self.db)

        def populate(c: sqlite3.Connection) -> None:
            seed_turn(c, "w1", ep, turn_type="witness", round=1, slot=1)
            seed_turn(c, "d1a", ep, turn_type="d1", round=None, slot=1, try_no=1, cls="technical")
            seed_turn(c, "d1b", ep, turn_type="d1", round=None, slot=1, try_no=2, cls="ok")
            seed_turn(c, "d1c", ep, turn_type="d1", round=None, slot=1, try_no=3, cls="technical")
            seed_turn(c, "d2a", ep, turn_type="d2", round=None, slot=1, try_no=1, cls="technical")
            seed_turn(c, "d2b", ep, turn_type="d2", round=None, slot=1, try_no=2, cls="technical")
            c.execute("UPDATE attempts SET status='done', finished_at=1")
            c.execute("UPDATE episodes SET status='done', final_attempt=1")
        self.db.write(populate)
        got = {r[0] for r in self.db.read(lambda c: c.execute("SELECT turn_id FROM v_turns_final").fetchall())}
        self.assertEqual({"w1", "d1b", "d2b"}, got)  # d2: nothing succeeded -> the last try

    def test_write_retries_then_raises_write_failed(self) -> None:
        import wtth.db as dbmod

        calls = []
        orig = dbmod._sleep
        dbmod._sleep = lambda s: calls.append(s)
        self.addCleanup(setattr, dbmod, "_sleep", orig)

        def boom(c: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("database is locked")
        with self.assertRaises(WriteFailed) as cm:
            self.db.write(boom, attempts=4)
        self.assertEqual("sqlite_locked", cm.exception.error_class)
        self.assertEqual(3, len(calls))  # sleeps between tries only
        self.assertEqual(1, self.db.write(lambda c: 1))  # connection recovered

    def test_non_lock_operational_error_fails_at_once(self) -> None:
        import wtth.db as dbmod

        calls = []
        orig = dbmod._sleep
        dbmod._sleep = lambda s: calls.append(s)
        self.addCleanup(setattr, dbmod, "_sleep", orig)
        with self.assertRaises(WriteFailed) as cm:
            self.db.write(lambda c: c.execute("SELECT * FROM no_such_table"))
        self.assertEqual("sqlite_operational", cm.exception.error_class)
        self.assertEqual([], calls)

    def test_write_rolls_back_on_other_errors(self) -> None:
        def bad(c: sqlite3.Connection) -> None:
            seed_run(c)
            raise ValueError("nope")
        with self.assertRaises(ValueError):
            self.db.write(bad)
        self.assertEqual(0, self.db.read(lambda c: c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]))

    def test_backup_copy(self) -> None:
        seed_running_episode(self.db)
        dest = tmp_dir(self._testMethodName + "_bk") / "b.sqlite"
        backup(self.path, dest)
        c = sqlite3.connect(dest)
        self.addCleanup(c.close)
        self.assertEqual(1, c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def test_db_requires_existing_file(self) -> None:
        d = Db(tmp_dir(self._testMethodName) / "missing.sqlite")
        with self.assertRaises(FileNotFoundError):
            d.read(lambda c: None)


if __name__ == "__main__":
    unittest.main()
