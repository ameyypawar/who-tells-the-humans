"""T5.4: release redaction. Private strings are loaded only through prereg.load_private() and are never printed:
every assertion reports ids, not values."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unittest
from pathlib import Path

from tests.util import make_db, seed_attempt, seed_episode, seed_run, seed_turn, tmp_dir
from wtth import redact
from wtth.config import load_settings
from wtth.prereg import load_private
from wtth.textnorm import canon_json


def variants(v: str) -> set[str]:
    return {v, json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1]}


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            out[str(p.relative_to(root)) + "/"] = "dir"
    return out


class TestRedaction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_settings()
        cls.private = load_private(cls.settings)
        priv = cls.private
        cls.base = tmp_dir("wp5_redact")
        cls.runs = cls.base / "runs"
        cls.runs.mkdir()
        path = cls.runs / "wtth.sqlite"
        from wtth.db import Db, create_db

        create_db(path)
        db = Db(path)
        p2, p3, forged = priv["C2.P2"], priv["C2.P3"], priv["FORGED_LINE"]

        def fn(conn):
            seed_run(conn)
            for seed, claim in ((2, p2), (3, p3)):
                ep = seed_episode(conn, f"core/main/m/CLAIM/L2/s{seed}", cell="CLAIM", seed=seed, status="running",
                                  queue_pos=seed)
                seed_attempt(conn, ep, 1)
                seed_turn(conn, f"t{seed}", ep, 1)
                conn.execute(
                    "INSERT INTO posts(episode_id, attempt, round, sender, slot, clock, clock_min, kind, text, "
                    "visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ep, 1, 2, "rd-11", None, "21:26", 1286, "scripted", claim, 2, time.time()))
                conn.execute(
                    "INSERT INTO exposures(turn_id, system_path, system_sha, tools_sha, prompt_sha, prompt_text, "
                    "visible_post_ids_json, visible_note_ids_json, visible_log_ids_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"t{seed}", "s", "s", "s", "p", f"[21:26] rd-11: {claim}\nrest", "[]", "[]", "[]"))
                conn.execute(
                    "INSERT INTO tool_calls(turn_id, episode_id, attempt, round, slot, sender, tool, args_json, accepted,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"t{seed}", ep, 1, 2, 1, "rd-14", "post_to_board", canon_json({"message": "quote: " + claim}), 1,
                     time.time()))
            ep = seed_episode(conn, "core/main/m/FORGED/L2/s9", cell="FORGED", seed=9, status="running", world="R1",
                              queue_pos=9)
            seed_attempt(conn, ep, 1)
            conn.execute(
                "INSERT INTO posts(episode_id, attempt, round, sender, slot, clock, clock_min, kind, text, "
                "visible_from_round, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ep, 1, 2, "rd-11", None, "21:26", 1286, "scripted", p2 + "\n" + forged, 2, time.time()))
            conn.execute("INSERT INTO events(ts, level, kind, detail_json) VALUES (?,?,?,?)",
                         (time.time(), "info", "x", json.dumps({"q": forged}, ensure_ascii=True)))

        db.write(fn)
        db.close()
        # files: stream.jsonl in both JSON escape styles, bodies, stderr, a plain report, stimuli
        t = cls.runs / "turns" / "00001-a1" / "witness-r2-s0-abcd"
        (t / "bodies" / "x").mkdir(parents=True)
        ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": "they said: " + p3}]}}
        (t / "stream.jsonl").write_text(json.dumps(ev, ensure_ascii=False) + "\n" + json.dumps(ev) + "\n",
                                        encoding="utf-8")
        (t / "stderr.txt").write_text("log " + forged + "\n", encoding="utf-8")
        (t / "bodies" / "x" / "001.request.json").write_text(json.dumps({"messages": [{"content": p2}]}), encoding="utf-8")
        (cls.runs / "reports").mkdir()
        (cls.runs / "reports" / "r.md").write_text(f"claim A: {p2}\nclaim B: {p3}\nforged: {forged}\n", encoding="utf-8")
        (cls.runs / "stimuli").mkdir()
        sysf = cls.runs / "stimuli" / "system_main_SB-1_V0.txt"
        sysf.write_text("public system prompt, nothing private", encoding="utf-8")
        sysf.chmod(0o444)
        (cls.runs / "wtth.lock").write_text("pid", encoding="utf-8")

        cls.before = snapshot(cls.runs)
        cls.dest = cls.base / "release"
        cls.summary = redact.build_release(cls.settings, cls.runs, cls.dest)

    def all_bytes(self):
        return {p: p.read_bytes() for p in self.dest.rglob("*") if p.is_file()}

    def test_zero_occurrences_raw_and_json_escaped(self):
        blobs = self.all_bytes()
        self.assertGreater(len(blobs), 5)
        for pid, val in self.private.items():
            for form in variants(val):
                if not form:
                    continue
                needle = form.encode("utf-8")
                for path, data in blobs.items():
                    self.assertFalse(needle in data, f"{pid} survives in {path.name}")

    def test_placeholders_are_present(self):
        text = "".join(d.decode("utf-8", "replace") for p, d in self.all_bytes().items() if p.suffix != ".sqlite")
        for pid in ("C2.P2", "C2.P3", "FORGED_LINE"):
            self.assertIn("{{PRIVATE:" + pid + "}}", text, pid)
            self.assertGreater(self.summary["counts"][pid], 0, pid)

    def test_sqlite_is_redacted_valid_and_placeholdered(self):
        conn = sqlite3.connect(self.dest / "wtth.sqlite")
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)
            texts = [r[0] for r in conn.execute("SELECT text FROM posts")]
            self.assertTrue(any("{{PRIVATE:C2.P2}}" in t for t in texts))
            self.assertTrue(any("{{PRIVATE:FORGED_LINE}}" in t for t in texts))
            args = [r[0] for r in conn.execute("SELECT args_json FROM tool_calls")]
            self.assertTrue(all("{{PRIVATE:" in a for a in args))
            json.loads(args[0])  # still valid JSON
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v_final").fetchone()[0], 0)  # views survive
        finally:
            conn.close()

    def test_runs_is_byte_identical_before_and_after(self):
        self.assertEqual(snapshot(self.runs), self.before)

    def test_lock_and_wal_files_skipped_and_residual_empty(self):
        self.assertIn("wtth.lock", self.summary["skipped"])
        self.assertFalse((self.dest / "wtth.lock").exists())
        self.assertEqual(self.summary["residual"], {})
        self.assertEqual(self.summary["sqlite_files"], 1)

    def test_public_files_unchanged(self):
        self.assertEqual((self.dest / "stimuli" / "system_main_SB-1_V0.txt").read_text(),
                         "public system prompt, nothing private")

    def test_destination_guards(self):
        busy = tmp_dir("wp5_redact_busy")
        (busy / "f").write_text("x")
        with self.assertRaises(redact.RedactionError):
            redact.build_release(self.settings, self.runs, busy)
        with self.assertRaises(redact.RedactionError):
            redact.build_release(self.settings, self.runs, self.runs / "inside")
        with self.assertRaises(redact.RedactionError):
            redact.build_release(self.settings, self.base / "missing", self.base / "d2")
        self.assertEqual(snapshot(self.runs), self.before)

    def test_error_messages_never_carry_private_text(self):
        try:
            redact.build_release(self.settings, self.runs, self.runs / "inside")
        except redact.RedactionError as exc:
            for val in self.private.values():
                self.assertFalse(val in str(exc))


if __name__ == "__main__":
    unittest.main()
