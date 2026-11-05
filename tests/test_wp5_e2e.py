"""End-to-end: `wtth run --phase g0dry --fake` into a temp runs root, then audit_g0 on what it wrote.

The file-based checks (G0-5/6/7/10/11/12) must find and examine real files; live-only checks are MANUAL. This is the
test that catches a disagreement between the runner's on-disk layout and the audit's expectations.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from tests import fake_claude
from tests.util import DUMMY_KEY, PYTHON, REPO, tmp_dir
from wtth import audit
from wtth.config import load_settings
from wtth.db import Db


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tmp_dir("wp5_e2e").resolve()
        cls.over = {"fake": True, "paths.runs_dir": str(cls.base / "runs"), "paths.slots_root": str(cls.base / "slots"),
                    "paths.cwd": str(cls.base / "cwd")}
        cls.settings = load_settings(overrides=cls.over)
        # key-free contract (sha256 only), built with the same helper the CLI shim should use
        beh = cls.base / "behavior.json"
        fake_claude.write_behavior(beh, {}, contract=fake_claude.contract_for(cls.settings, DUMMY_KEY))
        cmd = [PYTHON, "-I", "-m", "wtth", "run", "--phase", "g0dry", "--fake", str(beh), "--runs-dir",
               str(cls.base / "runs"), "--set", f"paths.slots_root={cls.base / 'slots'}",
               "--set", f"paths.cwd={cls.base / 'cwd'}"]
        cls.proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        cls.db = Db(cls.base / "runs" / "wtth.sqlite")
        rows = cls.db.read(lambda c: c.execute("SELECT run_id FROM runs").fetchall())
        cls.run_id = rows[0][0] if rows else None

    def audit(self, **kw):
        kw.setdefault("startup_check", lambda s, m, p: [])
        kw.setdefault("scan_roots", [self.base / "runs", self.base / "slots"])
        kw.setdefault("scan_git", False)
        return audit.audit_g0(self.settings, self.db, self.run_id, **kw)

    def test_run_finished(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout[-1500:] + self.proc.stderr[-500:])
        self.assertEqual(self.db.read(lambda c: c.execute("SELECT COUNT(*) FROM episodes WHERE status='done'").fetchone()[0]), 11)

    def test_production_layout_on_disk(self):
        wt = next((self.base / "runs" / "turns").glob("*/witness-*"))
        self.assertTrue(any((wt / "mcp").glob("*.jsonl")), "witness turn dir must hold mcp/<ts>.jsonl")
        self.assertFalse((wt / "mcp.jsonl").exists())
        self.assertTrue((wt / "stream.jsonl").is_file() and (wt / "stderr.txt").is_file())
        self.assertTrue(any((wt / "bodies").rglob("*.request.json")))

    def test_file_based_checks_pass_and_examine_files(self):
        rep = self.audit()
        got = {c.id: c.status for c in rep.checks}
        for cid in ("G0-2", "G0-4", "G0-5", "G0-6", "G0-7", "G0-8", "G0-9", "G0-10", "G0-11", "G0-12", "G0-15"):
            self.assertEqual(got[cid], "PASS", "\n".join(rep.lines()))
        self.assertEqual(got["G0-1"], "MANUAL")
        self.assertEqual(got["G0-3"], "MANUAL")      # a dry run has no deny probe
        self.assertIn(got["G0-13"], ("INFO",))
        self.assertRegex(rep.get("G0-11").detail, r"re-scanning \d{3} stderr and \d{3} MCP log files")
        self.assertRegex(rep.get("G0-7").detail, r"^(\d+)/\1 witness turns reconcile")
        self.assertRegex(rep.get("G0-6").detail, r"^\d+ audit turns, \d+ requests")

    def test_no_key_shaped_string_anywhere_in_the_run(self):
        needle = ("sk-" + "ant").encode()
        for root in (self.base / "runs", self.base / "slots", self.base / "behavior.json"):
            files = [root] if root.is_file() else [f for f in root.rglob("*") if f.is_file()]
            for f in files:
                self.assertNotIn(needle, f.read_bytes(), f.name)
        self.assertEqual(self.audit().get("G0-10").status, "PASS")

    def test_audit_notices_a_damaged_layout(self):
        wt = next((self.base / "runs" / "turns").glob("*/witness-*"))
        hold = wt.parent / "_mcp_hold"
        shutil.move(str(wt / "mcp"), str(hold))
        try:
            rep = self.audit()
            self.assertEqual(rep.get("G0-7").status, "FAIL")
        finally:
            shutil.move(str(hold), str(wt / "mcp"))
        self.assertEqual(self.audit().get("G0-7").status, "PASS")

    def test_report_written_masked(self):
        out = self.base / "runs" / "reports" / "g0_e2e.md"
        self.audit().write_md(out)
        text = out.read_text(encoding="utf-8")
        self.assertIn("G0-7", text)
        from wtth.prereg import load_private

        for pid, val in load_private(self.settings).items():
            self.assertFalse(val in text, pid)


if __name__ == "__main__":
    unittest.main()
