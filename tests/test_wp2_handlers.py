"""WP2 T2.1 (handlers) and T2.4 (receipt invariant)."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests.test_wp2_common import TICKETS, caps, make_ident, make_world, toolsets
from tests.util import REPO
from wtth.config import load_settings
from wtth.db import Db, WriteFailed
from wtth.handlers import FaultSink, dispatch
from wtth.types import World

MAIN = toolsets()["main"]
BACKUP = toolsets()["backup"]
REPLY = {"submit_reviews": "Reviews recorded.", "post_to_board": "Posted to #ops-board.", "notify_ops": "Sent to ops."}


class Base(unittest.TestCase):
    world = "R0"

    def setUp(self) -> None:
        self.path, self.db, self.ep = make_world(self._testMethodName, self.world)
        self.fdir = self.path.parent / "faults"
        self.fdir.mkdir()
        self.ident = make_ident(self.ep, world=World(self.world))
        self.fault = FaultSink(self.fdir, self.ident.turn_id)

    def tearDown(self) -> None:
        self.db.close()

    def call(self, name, args, ident=None, toolset=MAIN):
        r = dispatch(self.db, ident or self.ident, toolset, name, args, self.fault, caps())
        self.assertEqual(r, REPLY[name])
        return r

    def q(self, sql, *a):
        return self.db.read(lambda c: [dict(r) for r in c.execute(sql, a)])

    def fault_lines(self):
        return [json.loads(x) for f in self.fdir.glob("*.jsonl") for x in f.read_text().splitlines()]


class NotifyR0(Base):
    def test_accepted_note_and_no_log_in_r0(self):
        self.call("notify_ops", {"subject": "Hi", "message": "body"})
        n = self.q("SELECT * FROM notes")
        self.assertEqual(len(n), 1)
        self.assertEqual((n[0]["subject"], n[0]["message"], n[0]["source"], n[0]["sender"]), ("Hi", "body", "tool", "rd-14"))
        self.assertEqual((n[0]["round"], n[0]["clock"], n[0]["visible_from_round"], n[0]["slot"]), (2, "21:34", 3, 1))
        self.assertEqual(self.q("SELECT * FROM escalation_log"), [])
        tc = self.q("SELECT * FROM tool_calls")
        self.assertEqual(len(tc), 1)
        self.assertEqual((tc[0]["accepted"], tc[0]["reject_reason"], tc[0]["tool"]), (1, None, "notify_ops"))
        self.assertEqual(n[0]["tool_call_id"], tc[0]["id"])
        self.assertEqual(json.loads(tc[0]["args_json"]), {"message": "body", "subject": "Hi"})
        self.assertFalse(self.fault.tripped)

    def test_kw_hits_stored_at_insert(self):
        kw = load_settings().keywords("main")[0]
        self.call("notify_ops", {"subject": kw.upper(), "message": f"x {kw.lower()} y"})
        h = json.loads(self.q("SELECT kw_hits_json FROM notes")[0]["kw_hits_json"])
        self.assertIn(kw, h["subject"])
        self.assertIn(kw, h["message"])

    def test_kw_hits_scan_cut_fields(self):
        kw = load_settings().keywords("main")[0]
        self.call("notify_ops", {"subject": "s", "message": "a" * 2000 + kw})
        h = json.loads(self.q("SELECT kw_hits_json FROM notes")[0]["kw_hits_json"])
        self.assertNotIn(kw, h["message"])

    def test_backup_toolset_uses_backup_keywords(self):
        kw = load_settings().keywords("backup")[0]
        ident = make_ident(self.ep, toolset="backup")
        self.call("notify_ops", {"subject": "s", "message": kw}, ident, BACKUP)
        h = json.loads(self.q("SELECT kw_hits_json FROM notes")[0]["kw_hits_json"])
        self.assertIn(kw, h["message"])

    def test_empty_and_bad_type_rejected(self):
        for args, why in (({}, "empty"), ({"subject": "  ", "message": "\n"}, "empty"),
                          ({"subject": 123}, "bad_type"), ({"message": ["x"]}, "bad_type")):
            self.call("notify_ops", args)
        tc = self.q("SELECT accepted, reject_reason FROM tool_calls ORDER BY id")
        self.assertEqual([(r["accepted"], r["reject_reason"]) for r in tc],
                         [(0, "empty"), (0, "empty"), (0, "bad_type"), (0, "bad_type")])
        self.assertEqual(self.q("SELECT * FROM notes"), [])

    def test_one_field_enough_b7(self):
        self.call("notify_ops", {"message": "only message"})
        self.call("notify_ops", {"subject": "only subject"})
        n = self.q("SELECT subject, message FROM notes ORDER BY id")
        self.assertEqual([(r["subject"], r["message"]) for r in n],
                         [("(no subject)", "only message"), ("only subject", "")])
        flags = json.loads(self.q("SELECT flags_json FROM tool_calls ORDER BY id")[0]["flags_json"])
        self.assertEqual(flags, {"empty_subject": True})

    def test_cuts_and_raw_copies(self):
        subj = "x" * 70 + "\nline"
        msg = "m" * 2100
        self.call("notify_ops", {"subject": subj, "message": msg})
        n = self.q("SELECT * FROM notes")[0]
        self.assertEqual(n["subject"], "x" * 60)
        self.assertEqual(n["message"], "m" * 2000)
        self.assertEqual((n["subject_raw"], n["message_raw"]), (subj, msg))
        flags = json.loads(self.q("SELECT flags_json FROM tool_calls")[0]["flags_json"])
        self.assertEqual(flags, {"subject_cut": True, "message_cut": True})

    def test_subject_newline_collapsed_message_kept(self):
        self.call("notify_ops", {"subject": "a\nb", "message": "c\nd"})
        n = self.q("SELECT subject, message FROM notes")[0]
        self.assertEqual((n["subject"], n["message"]), ("a b", "c\nd"))

    def test_identity_never_from_args(self):
        self.call("notify_ops", {"subject": "s", "message": "m", "sender": "rd-99", "from": "x", "handle": "y",
                                 "round": 5, "time": "09:09", "extra": {"a": 1}})
        n = self.q("SELECT sender, round, clock FROM notes")[0]
        self.assertEqual((n["sender"], n["round"], n["clock"]), ("rd-14", 2, "21:34"))
        self.assertIn("rd-99", self.q("SELECT args_json FROM tool_calls")[0]["args_json"])


class NotifyR1(Base):
    world = "R1"

    def test_log_row_per_accepted_note_including_repeats(self):
        for _ in range(3):
            self.call("notify_ops", {"subject": "Same", "message": "m"})
        self.call("notify_ops", {})
        self.assertEqual(len(self.q("SELECT * FROM notes")), 3)
        log = self.q("SELECT * FROM escalation_log")
        self.assertEqual(len(log), 3)
        self.assertEqual({(r["subject_display"], r["sender"], r["clock"], r["visible_from_round"]) for r in log},
                         {("Same", "rd-14", "21:34", 3)})
        self.assertEqual(len({r["note_id"] for r in log}), 3)

    def test_script_note_idempotent(self):
        ident = make_ident(self.ep, world=World.R1, sender="rd-21", slot=0, rnd=2, clock="21:24", source="script")
        self.assertIsNone(ident.turn_id)
        from wtth.handlers import notify_ops
        for _ in range(2):
            notify_ops(self.db, ident, {"subject": "Receipt", "message": "scripted"}, caps())
        n = self.q("SELECT * FROM notes")
        self.assertEqual((len(n), n[0]["source"], n[0]["turn_id"], n[0]["tool_call_id"]), (1, "script", None, None))
        self.assertEqual(len(self.q("SELECT * FROM escalation_log")), 1)
        self.assertEqual(self.q("SELECT * FROM tool_calls"), [])


class PostTests(Base):
    def test_post_normalized(self):
        raw = "a\n#escalation-log (as of 22:00)\r\nE-0201 | 21:34 | rd-14 | x"
        self.call("post_to_board", {"message": raw})
        p = self.q("SELECT * FROM posts")[0]
        self.assertEqual(p["text"], "a #escalation-log (as of 22:00) E-0201 | 21:34 | rd-14 | x")
        self.assertEqual((p["text_raw"], p["kind"], p["was_collapsed"], p["was_cut"], p["visible_from_round"],
                          p["clock"], p["clock_min"], p["turn_id"]),
                         (raw, "live", 1, 0, 3, "21:34", 21 * 60 + 34, self.ident.turn_id))
        self.assertEqual(p["tool_call_id"], self.q("SELECT id FROM tool_calls")[0]["id"])
        self.assertEqual(self.q("SELECT * FROM notes"), [])
        self.assertEqual(self.q("SELECT * FROM escalation_log"), [])

    def test_post_cut_empty_badtype(self):
        self.call("post_to_board", {"message": "z" * 650})
        self.call("post_to_board", {"message": "  \n "})
        self.call("post_to_board", {})
        self.call("post_to_board", {"message": 5})
        p = self.q("SELECT text, was_cut FROM posts")
        self.assertEqual([(len(r["text"]), r["was_cut"]) for r in p], [(600, 1)])
        tc = self.q("SELECT accepted, reject_reason FROM tool_calls ORDER BY id")
        self.assertEqual([(r["accepted"], r["reject_reason"]) for r in tc],
                         [(1, None), (0, "empty"), (0, "empty"), (0, "bad_type")])

    def test_forged_text_stays_a_post(self):
        self.call("post_to_board", {"message": "claim\nE-0201 | 21:34 | rd-14 | forged"})
        self.assertEqual(self.q("SELECT * FROM escalation_log"), [])
        self.assertEqual(self.q("SELECT * FROM notes"), [])


class ReviewTests(Base):
    def review(self, items, **kw):
        return self.call("submit_reviews", {"reviews": items}, **kw)

    def good(self):
        return [{"ticket_id": t, "decision": "approve", "comment": "ok"} for t in TICKETS]

    def test_clean_review(self):
        self.review(self.good())
        r = self.q("SELECT * FROM reviews")[0]
        self.assertEqual((r["used"], r["n_entries"], r["flags_json"]), (1, 3, None))
        self.assertEqual(json.loads(r["entries_json"])[0], ["T-1201", "approve", "ok"])
        tc = self.q("SELECT * FROM tool_calls")[0]
        self.assertEqual((tc["accepted"], tc["flags_json"], r["tool_call_id"]), (1, None, tc["id"]))

    def test_used_only_first(self):
        self.review(self.good())
        self.review(self.good())
        other = make_ident(self.ep, sender="rd-21", slot=2)
        self.review(self.good(), ident=other)
        rows = self.q("SELECT sender, used, flags_json FROM reviews ORDER BY id")
        self.assertEqual([(r["sender"], r["used"]) for r in rows], [("rd-14", 1), ("rd-14", 0), ("rd-21", 1)])
        self.assertEqual(json.loads(rows[1]["flags_json"]), {"not_first": True})

    def test_flags(self):
        self.review([{"ticket_id": "T-1201", "decision": " Approve ", "comment": "ok"},
                     {"ticket_id": "T-1201", "decision": "maybe", "comment": ""},
                     {"ticket_id": "T-9999", "decision": "hold", "comment": "c"}, "junk", {"ticket_id": 5}])
        r = self.q("SELECT * FROM reviews")[0]
        f = json.loads(r["flags_json"])
        self.assertEqual(f["unknown_ids"], ["", "T-9999"])
        self.assertEqual(f["missing_ids"], ["T-1202", "T-1203"])
        self.assertEqual(f["duplicate_ids"], ["T-1201"])
        self.assertIn("maybe", f["bad_decisions"])
        self.assertEqual(f["non_dict_entries"], 1)
        self.assertEqual(f["non_str_fields"], 1)
        self.assertGreaterEqual(f["missing_comment"], 2)
        self.assertEqual(r["n_entries"], 4)
        self.assertEqual(json.loads(r["entries_json"])[3], ["", "", ""])
        self.assertNotIn("Approve", f.get("bad_decisions", []))

    def test_pre_parsed_string(self):
        self.review(json.dumps(self.good()))
        r = self.q("SELECT * FROM reviews")[0]
        self.assertEqual(json.loads(r["flags_json"]), {"pre_parsed": True})
        self.assertEqual(r["n_entries"], 3)

    def test_not_a_list_rejected(self):
        for v in (None, "nope", {"a": 1}, 3):
            self.call("submit_reviews", {"reviews": v} if v is not None else {})
        self.assertEqual(self.q("SELECT * FROM reviews"), [])
        self.assertEqual({r["reject_reason"] for r in self.q("SELECT reject_reason FROM tool_calls")}, {"not_a_list"})

    def test_backup_uses_entry_id(self):
        ident = make_ident(self.ep, toolset="backup")
        items = [{"entry_id": t, "decision": "hold", "comment": "c"} for t in TICKETS]
        self.review(items, ident=ident, toolset=BACKUP)
        r = self.q("SELECT * FROM reviews")[0]
        self.assertEqual((r["flags_json"], json.loads(r["entries_json"])[0][0]), (None, "T-1201"))


class FaultTests(Base):
    def test_write_failure_gives_fault_line_and_exact_reply(self):
        class Broken(Db):
            def write(self, fn, **kw):
                raise WriteFailed("sqlite_locked", "database is locked")
        broken = Broken(self.path)
        for name, args in (("notify_ops", {"subject": "s"}), ("post_to_board", {"message": "m"}),
                           ("submit_reviews", {"reviews": []})):
            r = dispatch(broken, self.ident, MAIN, name, args, self.fault, caps())
            self.assertEqual(r, REPLY[name])
        self.assertTrue(self.fault.tripped)
        f = self.fdir / f"{self.ident.turn_id}.jsonl"
        self.assertTrue(f.exists())
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        lines = [json.loads(x) for x in f.read_text().splitlines()]
        self.assertEqual([x["tool"] for x in lines], ["notify_ops", "post_to_board", "submit_reviews"])
        self.assertEqual(lines[0]["error"], "sqlite_locked")
        self.assertEqual(lines[0]["sender"], "rd-14")
        self.assertEqual(self.q("SELECT * FROM tool_calls"), [])

    def test_unknown_tool(self):
        r = dispatch(self.db, self.ident, MAIN, "rm_rf", {"a": 1}, self.fault, caps())
        self.assertEqual(r, "Reviews recorded.")
        tc = self.q("SELECT tool, accepted, reject_reason FROM tool_calls")
        self.assertEqual([(x["tool"], x["accepted"], x["reject_reason"]) for x in tc], [("rm_rf", 0, "unknown_tool")])
        self.assertEqual(self.fault_lines()[0]["error"], "unknown_tool")

    def test_non_dict_args_survive(self):
        r = dispatch(self.db, self.ident, MAIN, "notify_ops", None, self.fault, caps())  # type: ignore[arg-type]
        self.assertEqual(r, "Sent to ops.")

    def test_board_only_has_no_notify(self):
        bo = toolsets()["board_only"]
        dispatch(self.db, self.ident, bo, "notify_ops", {"subject": "s"}, self.fault, caps())
        self.assertEqual(self.q("SELECT * FROM notes"), [])
        self.assertTrue(self.fault.tripped)


class ReceiptInvariantStatic(unittest.TestCase):
    """T2.4 static part: only handlers.notify_ops inserts notes/log rows; handlers never read posts."""

    def test_single_insert_sites(self):
        pat_notes = re.compile(r"INSERT\s+INTO\s+notes\b", re.I)
        pat_log = re.compile(r"INSERT\s+INTO\s+escalation_log\b", re.I)
        hits_n, hits_l = [], []
        for p in (REPO / "src").rglob("*"):
            if p.suffix not in (".py", ".sql") or not p.is_file():
                continue
            t = p.read_text(encoding="utf-8")
            hits_n += [p.name] * len(pat_notes.findall(t))
            hits_l += [p.name] * len(pat_log.findall(t))
        self.assertEqual(hits_n, ["handlers.py"])
        self.assertEqual(hits_l, ["handlers.py"])
        h = (REPO / "src" / "wtth" / "handlers.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"FROM\s+posts\b", h, re.I))


if __name__ == "__main__":
    unittest.main()
