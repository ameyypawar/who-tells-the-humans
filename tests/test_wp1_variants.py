"""T1.4: runbook variants differ from their parent by exactly one appended paragraph."""
from __future__ import annotations

import unittest

from tests.wp1_common import settings, texts
from wtth import render
from wtth.prereg import RUNBOOK_VARIANTS

CELLS = lambda: settings().cells   # noqa: E731


class Variants(unittest.TestCase):
    def sysprompt(self, sc: str, cell: str) -> str:
        return render.system_prompt(texts(sc), CELLS()[cell])

    def test_registry_matches_config(self) -> None:
        self.assertEqual({k: list(v) for k, v in RUNBOOK_VARIANTS.items()}, settings().runbooks)

    def test_one_paragraph_over_the_parent(self) -> None:
        # (child cell, parent cell, extra paragraph id)
        pairs = [("POLICY", "NONE", "RB-POLICY"), ("NONE-R", "NONE", "RB-R1"), ("NEG", "NONE-R", "RB-DEDUPE"),
                 ("O1", "O0", "RB-OWNER")]
        for sc in ("main", "backup"):
            t = texts(sc)
            for child, parent, para in pairs:
                with self.subTest(scenario=sc, child=child):
                    p, c = self.sysprompt(sc, parent), self.sysprompt(sc, child)
                    self.assertEqual(c, p + "\n\n" + t.runbook_paras[para])
                    self.assertTrue(c.startswith(p))
                    self.assertEqual(c.count("\n\n"), p.count("\n\n") + 1)
            # the other cells share a variant with one of those
            for cell, same in (("CONCERN", "NONE"), ("CLAIM", "NONE"), ("B-NONE", "NONE"), ("RECEIPT", "NONE-R"),
                               ("NO-RECEIPT", "NONE-R"), ("FORGED", "NONE-R"), ("POS", "NEG")):
                self.assertEqual(self.sysprompt(sc, cell), self.sysprompt(sc, same), cell)

    def test_v_r1_dedupe_is_v0_plus_two(self) -> None:
        t = texts("main")
        self.assertEqual(self.sysprompt("main", "NEG"),
                         self.sysprompt("main", "NONE") + "\n\n" + t.runbook_paras["RB-R1"] + "\n\n" + t.runbook_paras["RB-DEDUPE"])

    def test_base_is_sb1_plus_runbook(self) -> None:
        for sc in ("main", "backup"):
            t = texts(sc)
            self.assertEqual(self.sysprompt(sc, "NONE"), t.system["SB-1"] + "\n\n" + t.runbook_base["RB-BASE"])
        self.assertFalse(self.sysprompt("main", "NONE").endswith("\n"))

    def test_scenario_texts_differ_only_in_the_setting(self) -> None:
        self.assertNotEqual(self.sysprompt("main", "NONE"), self.sysprompt("backup", "NONE"))
        for para in ("RB-R1", "RB-POLICY", "RB-DEDUPE"):
            self.assertEqual(texts("main").runbook_paras[para], texts("backup").runbook_paras[para])
        # BK-1 differs from SB-1 only in the first paragraph and "log entries" for "refund tickets"
        a, b = texts("main").system["SB-1"].split("\n\n"), texts("backup").system["SB-1"].split("\n\n")
        self.assertEqual(len(a), len(b))
        self.assertNotEqual(a[0], b[0])
        self.assertEqual(a[3:], b[3:])
        self.assertEqual(a[1].replace("refund tickets", "log entries"), b[1])
        self.assertEqual(a[2].replace("this round's tickets", "this round's log entries"), b[2])

    def test_board_only_is_the_documented_exception(self) -> None:
        t = texts("main")
        # BO-1: SB-1 with the notify_ops bullet and the third tool gone
        sb1 = t.system["SB-1"]
        want = sb1.replace("You have three tools:", "You have two tools:").replace("- notify_ops: send a private note to ops.\n", "")
        self.assertEqual(t.system["BO-1"], want)
        # BO-RB-BASE: RB-BASE with the second sentence of the last paragraph removed
        want_rb = t.runbook_base["RB-BASE"].replace(" Notes sent with notify_ops are private and go to the operator on call.", "")
        self.assertEqual(t.runbook_base["BO-RB-BASE"], want_rb)
        self.assertNotIn("notify_ops", self.sysprompt("main", "BA3"))
        self.assertEqual(self.sysprompt("main", "BA3"), t.system["BO-1"] + "\n\n" + t.runbook_base["BO-RB-BASE"])
        self.assertEqual(self.sysprompt("main", "BA1"), self.sysprompt("main", "NONE"))
        self.assertEqual(self.sysprompt("main", "BA4"), self.sysprompt("main", "BA3"))

    def test_unknown_variant(self) -> None:
        from dataclasses import replace
        with self.assertRaises(ValueError):
            render.system_prompt(texts(), replace(settings().cells["NONE"], runbook="V-NOPE"))


class SystemFile(unittest.TestCase):
    def test_write_once_mode_0444_no_trailing_newline(self) -> None:
        import os
        import stat
        from tests.util import tmp_dir
        d = tmp_dir("wp1_sysfile") / "stimuli"
        cell = settings().cells["NEG"]
        path, sha = render.write_system_prompt(d, texts(), cell)
        self.assertEqual(path.name, "system_main_SB-1_V-R1-DEDUPE.txt")
        data = path.read_bytes()
        self.assertEqual(data.decode("utf-8"), render.system_prompt(texts(), cell))
        self.assertFalse(data.endswith(b"\n"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o444)
        from wtth.textnorm import sha256_text
        self.assertEqual(sha, sha256_text(data.decode("utf-8")))
        again, sha2 = render.write_system_prompt(d, texts(), cell)     # idempotent, no rewrite of a 0444 file
        self.assertEqual((again, sha2), (path, sha))
        other = render.write_system_prompt(d, texts("backup"), settings().cells["NONE"])[0]
        self.assertEqual(other.name, "system_backup_SB-1_V0.txt")
        path.chmod(0o644)
        path.write_bytes(b"changed")
        with self.assertRaises(FileExistsError):
            render.write_system_prompt(d, texts(), cell)


if __name__ == "__main__":
    unittest.main()
