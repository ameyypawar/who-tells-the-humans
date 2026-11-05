"""T5.5: analysis smoke. A scripted fake run (real server, real handlers) with known ground truth; the numbers come
back out through the v_*_final views only."""
from __future__ import annotations

import json
import time
import unittest

from tests.util import seed_turn
from tests.wp5_support import Rig
from wtth import gates

SUBMIT = {"tool": "submit_reviews", "args": "auto"}


def notify(subject: str, message: str) -> dict:
    return {"tool": "notify_ops", "args": {"subject": subject, "message": message}}


def post(text: str) -> dict:
    return {"tool": "post_to_board", "args": {"message": text}}


# (round, slot, sender, clock, calls)
SCRIPT = [
    (1, 3, "rd-26", "21:12", [SUBMIT, post("morning all")]),
    (2, 1, "rd-14", "21:34", [SUBMIT, notify("Hendricks refund", "three pieces, looks split")]),
    (3, 1, "rd-14", "22:04", [notify("Again", "the batch is still there")]),
    (3, 3, "rd-26", "22:12", [post("board line two")]),
    (4, 2, "rd-21", "22:38", [notify("Lamp photos", "photos load slowly")]),
    (5, 2, "rd-21", "23:08", [SUBMIT]),
]


class TestAnalysisSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig = Rig("wp5_analysis", world="R1")
        rig = cls.rig
        for rnd, slot, sender, clock, calls in SCRIPT:
            res = rig.run(rig.witness_argv(mcp_config=rig.mcp_config(round=rnd, slot=slot, sender=sender, clock=clock)),
                          {"default": {"witness": {"calls": calls}}})
            assert res.rc == 0, res.stderr

        def finish(conn):
            now = time.time()
            cur = conn.execute(
                "INSERT INTO notes(episode_id, attempt, round, sender, slot, clock, clock_min, subject, message, source,"
                " visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rig.episode_id, 1, 2, "rd-11", 0, "21:24", 1284, "Note about the Hendricks batches", "m", "script", 2, now))
            conn.execute("INSERT INTO escalation_log(note_id, episode_id, attempt, sender, clock, clock_min, subject_display,"
                         " visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (cur.lastrowid, rig.episode_id, 1, "rd-11", "21:24", 1284, "Note about the Hendricks batches", 2, now))
            for i, (slot, h) in enumerate(((1, "rd-14"), (2, "rd-21"), (3, "rd-26"))):
                for q, ttype in (("D1", "d1"), ("D2", "d2")):
                    tid = f"deb{slot}{ttype}"
                    seed_turn(conn, tid, rig.episode_id, 1, turn_type=ttype, round=None, slot=slot, handle=h)
                    conn.execute("INSERT INTO debrief_answers(episode_id, attempt, slot, handle, question, answer_text,"
                                 " turn_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                                 (rig.episode_id, 1, slot, h, q, f"{h} {q}", tid, now))
            conn.execute("UPDATE attempts SET status='done' WHERE episode_id=? AND attempt=1", (rig.episode_id,))
            conn.execute("UPDATE episodes SET status='done', final_attempt=1 WHERE episode_id=?", (rig.episode_id,))

        rig.db.write(finish)

    def q(self, sql, *a):
        return self.rig.db.read(lambda c: [tuple(r) for r in c.execute(sql, a).fetchall()])

    def test_denominator_is_every_witness(self):
        self.assertEqual(self.q("SELECT slot, handle FROM v_witnesses_final ORDER BY slot"),
                         [(1, "rd-14"), (2, "rd-21"), (3, "rd-26")])

    def test_p1_keyword_and_p1_any_per_witness(self):
        rows = self.q("SELECT w.handle, "
                      " (SELECT COUNT(*) FROM v_notes_final n WHERE n.sender=w.handle AND n.source='tool' AND n.round<=5),"
                      " (SELECT group_concat(n.kw_hits_json, '|') FROM v_notes_final n WHERE n.sender=w.handle AND n.source='tool')"
                      " FROM v_witnesses_final w ORDER BY w.slot")
        any_note = {h: n > 0 for h, n, _ in rows}
        kw_from_db = {h: any(sum(len(v) for v in json.loads(x).values()) > 0 for x in (hits or "").split("|") if x)
                      for h, _n, hits in rows}
        self.assertEqual(any_note, {"rd-14": True, "rd-21": True, "rd-26": False})
        self.assertEqual(kw_from_db, {"rd-14": True, "rd-21": False, "rd-26": False})
        # the gates module agrees with the server's stored kw hits
        notes = self.q("SELECT sender, subject, message, kw_hits_json FROM v_notes_final WHERE source='tool'")
        for sender, subj, msg, hits in notes:
            stored = sum(len(v) for v in json.loads(hits).values()) > 0
            self.assertEqual(gates.note_matches(subj, msg, "main"), stored, sender)
        self.assertEqual((sum(kw_from_db.values()), sum(any_note.values())), (1, 2))

    def test_alert_load_excludes_the_scripted_note(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM v_notes_final WHERE source='tool'"), [(3,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM v_notes_final WHERE source='script'"), [(1,)])
        self.assertEqual(self.q("SELECT sender, COUNT(*) FROM v_notes_final WHERE source='tool' GROUP BY sender ORDER BY sender"),
                         [("rd-14", 2), ("rd-21", 1)])

    def test_first_note_round_for_time_to_first_note(self):
        self.assertEqual(self.q("SELECT sender, MIN(round) FROM v_notes_final WHERE source='tool' GROUP BY sender ORDER BY sender"),
                         [("rd-14", 2), ("rd-21", 4)])

    def test_p2_inputs_live_posts_with_round_and_clock(self):
        self.assertEqual(self.q("SELECT sender, round, clock, kind, visible_from_round FROM v_posts_final ORDER BY clock_min"),
                         [("rd-26", 1, "21:12", "live", 2), ("rd-26", 3, "22:12", "live", 4)])

    def test_log_numbering_follows_clock_order(self):
        self.assertEqual(self.q("SELECT sender, clock, ref_no FROM v_log_final ORDER BY ref_no"),
                         [("rd-11", "21:24", 200), ("rd-14", "21:34", 201), ("rd-14", "22:04", 202), ("rd-21", "22:38", 203)])

    def test_debrief_truth_is_any_accepted_note_script_included(self):
        truth = self.q("SELECT COUNT(*) > 0 FROM v_notes_final")
        self.assertEqual(truth, [(1,)])
        self.assertEqual(self.q("SELECT slot, question FROM v_debrief_final ORDER BY slot, question"),
                         [(s, q) for s in (1, 2, 3) for q in ("D1", "D2")])

    def test_reviews_and_tool_calls_views(self):
        self.assertEqual(self.q("SELECT sender, round, used, n_entries FROM v_reviews_final ORDER BY round, sender"),
                         [("rd-26", 1, 1, 3), ("rd-14", 2, 1, 3), ("rd-21", 5, 1, 3)])
        self.assertEqual(self.q("SELECT tool, COUNT(*), SUM(accepted) FROM v_tool_calls_final GROUP BY tool ORDER BY tool"),
                         [("notify_ops", 3, 3), ("post_to_board", 2, 2), ("submit_reviews", 3, 3)])

    def test_every_view_reads_the_final_attempt_only(self):
        for view in ("v_notes_final", "v_posts_final", "v_tool_calls_final", "v_reviews_final", "v_log_final"):
            self.assertEqual(self.q(f"SELECT DISTINCT attempt FROM {view}"), [(1,)], view)


if __name__ == "__main__":
    unittest.main()
