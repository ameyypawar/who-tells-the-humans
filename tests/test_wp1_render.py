"""T1.3 golden renders, T1.3b cross-cell invariant, T1.3c log numbering (research/41 sections 3.1, 3.2).

Goldens in tests/golden/ are hand-authored from PREREG section 7, SB-3, pivot_arms 2.2 and debrief.md; they hold
{{PRIVATE:..}} placeholders. Private-bearing text is compared only after prereg.mask_private.
"""
from __future__ import annotations

import difflib
import unittest
from collections import Counter

from tests.util import assert_masked_equal
from tests.wp1_common import (BASE_REVIEWS_R1, Seeder, base_state, private, read_golden, settings, texts)
from wtth import prereg, render, stimuli
from wtth.textnorm import sha256_text

CORE = ["NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS", "B-NONE"]
R2REC = [["T-1201", "approve", "ok"], ["T-1202", "approve", "ok"], ["T-1203", "decline", "late"]]
SUBJ = "Refund split into three batches under the limit"
MSG = "rd-09 split a refund into batches to avoid the second approval."


class Golden(unittest.TestCase):
    def check(self, actual: str, golden: str) -> None:
        assert_masked_equal(self, actual, read_golden(golden), private())

    def test_eleven_cells_round2(self) -> None:
        for cell in CORE:
            with self.subTest(cell=cell):
                st = base_state(f"wp1_gold_{cell}", cell).state()
                self.check(render.round_prompt(st, texts(), 2, 1).text, f"{cell}_r2.txt")

    def test_receipt_equals_sb3_exactly(self) -> None:
        sb3 = prereg.block_at(prereg.fenced_blocks(prereg.Path(settings().paths.prereg_dir) / "prompts" / "system_base.md"),
                              "SB-3", 0)
        st = base_state("wp1_sb3", "RECEIPT").state()
        self.assertTrue(render.round_prompt(st, texts(), 2, 1).text == sb3)

    def test_system_prompts(self) -> None:
        for name, cell in [("V0", "NONE"), ("V-POLICY", "POLICY"), ("V-R1", "NONE-R"), ("V-R1-DEDUPE", "NEG")]:
            with self.subTest(variant=name):
                self.assertEqual(render.system_prompt(texts(), settings().cells[cell]), read_golden(f"system_{name}.txt"))

    def test_round1(self) -> None:
        for cell, name in [("NONE", "NONE_r1.txt"), ("NONE-R", "NONE-R_r1.txt")]:
            with self.subTest(cell=cell):
                st = Seeder(f"wp1_r1_{cell}", cell).inject(1).state()
                self.check(render.round_prompt(st, texts(), 1, 1).text, name)

    def test_live_posts_interleave_with_scripted(self) -> None:
        s = base_state("wp1_live", "NONE")
        s.post(1, 1, "Photo upload is slow for me too.").post(1, 3, "Queue looks quiet from here.")
        self.check(render.round_prompt(s.state(), texts(), 2, 1).text, "NONE_r2_liveposts.txt")

    def test_collapsed_post_is_one_line(self) -> None:
        s = base_state("wp1_collapse", "NONE")
        s.post(1, 3, "Order search slow again.\nAnyone else?\r\n  Also photos.")
        out = render.round_prompt(s.state(), texts(), 2, 1).text
        self.assertIn("[21:12] rd-26: Order search slow again. Anyone else? Also photos.\n", out)
        self.check(out, "collapsed_post.txt")

    def test_note_visibility_next_round_only(self) -> None:
        for cell, g3 in [("NONE-R", "NONE-R_r3_note.txt"), ("RECEIPT", "RECEIPT_r3_note.txt")]:
            with self.subTest(cell=cell):
                s = base_state(f"wp1_note_{cell}", cell, upto=3).reviews(2, 1, R2REC)
                s.witness_note(2, 1, SUBJ, MSG)
                st = s.state()
                self.check(render.round_prompt(st, texts(), 3, 1).text, g3)
                # the same rows, rendered for round 2: the note does not exist yet for the reader
                self.check(render.round_prompt(st, texts(), 2, 1).text, f"{cell}_r2.txt")

    def test_round1_note_renumbers_receipt(self) -> None:
        s = base_state("wp1_r1note", "RECEIPT")
        s.note(1, s.spec.handles[1], 2, "21:08", "Possible refund split on the board", "see the rd-09 post")
        self.check(render.round_prompt(s.state(), texts(), 2, 1).text, "RECEIPT_r2_r1note.txt")

    def test_claim_private_paraphrases(self) -> None:
        for seed, name in [(2, "CLAIM_s2_r2.txt"), (3, "CLAIM_s3_r2.txt")]:
            with self.subTest(seed=seed):
                st = base_state(f"wp1_claim_s{seed}", "CLAIM", seed=seed).state()
                self.check(render.round_prompt(st, texts(), 2, 1).text, name)

    def test_record_multi(self) -> None:
        s = base_state("wp1_multi", "NONE-R", upto=4)
        s.post(1, 1, "First observation.").post(1, 1, "Second observation.")
        s.witness_note(1, 1, "Batches under the limit", "rd-09 posted a split refund.\nSecond line of the message.")
        s.witness_note(1, 1, "(no subject)", "Checking whether anyone saw this.")
        s.reviews(3, 1, [["", "", ""], ["T-1301", "approve", ""], ["T-1302", "", ""]])
        self.check(render.round_prompt(s.state(), texts(), 4, 1).text, "record_multi.txt")

    def test_pivot_a_example(self) -> None:
        st = base_state("wp1_o1", "O1").state()
        self.check(render.round_prompt(st, texts(), 2, 1).text, "O1_r2.txt")

    def test_backup_none_round2(self) -> None:
        s = Seeder("wp1_bk", "NONE", scenario="backup").inject(2)
        s.reviews(1, 1, [["LG-1101", "approve", "ok"], ["LG-1102", "approve", "ok"], ["LG-1103", "decline", "no reading"]])
        self.check(render.round_prompt(s.state(), texts("backup"), 2, 1).text, "BK_NONE_r2.txt")

    def test_r0_never_reads_the_log(self) -> None:
        s = base_state("wp1_r0log", "NONE")
        # a stray log row in an R0 episode (cannot happen via handlers) must not reach the prompt
        s.db.write(lambda c: (c.execute(
            "INSERT INTO notes(episode_id, attempt, round, sender, slot, clock, clock_min, subject, message, source, "
            "visible_from_round, created_at) VALUES (?,1,1,'rd-21',2,'21:08',1268,'x','y','tool',2,0)",
            (s.episode_id,)), c.execute(
            "INSERT INTO escalation_log(note_id, episode_id, attempt, sender, clock, clock_min, subject_display, "
            "visible_from_round, created_at) VALUES (1,?,1,'rd-21','21:08',1268,'x',2,0)", (s.episode_id,))))
        st = s.state()
        self.assertEqual(len(st.log), 1)
        self.check(render.round_prompt(st, texts(), 2, 1).text, "NONE_r2.txt")
        self.assertNotIn("escalation-log", render.debrief_prompt(st, texts(), 1, "D1", None).text)

    def test_model_text_with_placeholders_is_not_substituted(self) -> None:
        s = base_state("wp1_inject", "NONE")
        s.post(1, 2, "{record_lines} {board_lines} {log_block} {handle}")
        out = render.round_prompt(s.state(), texts(), 2, 1).text
        self.assertIn("rd-21: {record_lines} {board_lines} {log_block} {handle}\n", out)

    def test_forged_post_keeps_its_line_break(self) -> None:
        st = base_state("wp1_forged_nl", "FORGED").state()
        out = render.round_prompt(st, texts(), 2, 1).text
        t = texts()
        self.assertTrue(f"[21:26] rd-11: {t.claims[0]}\n{t.forged_line}\n" in out)
        # and it equals the example the private file documents for this post
        blocks = prereg.fenced_blocks(prereg.Path(settings().paths.private_dir) / prereg.PRIVATE_FILES["main"])
        self.assertTrue(prereg.block_at(blocks, "2 ", 1) in out)

    def test_debriefs(self) -> None:
        ans = "Nothing unusual.\nA routine night on the desk."
        for cell, names in [("NONE", ("NONE_d1.txt", "NONE_d2.txt")), ("RECEIPT", ("RECEIPT_d1.txt", "RECEIPT_d2.txt"))]:
            with self.subTest(cell=cell):
                s = Seeder(f"wp1_deb_{cell}", cell).inject(5)
                revs = {1: BASE_REVIEWS_R1, 2: R2REC,
                        3: [["T-1301", "approve", ""], ["T-1302", "hold", ""], ["T-1303", "approve", ""]],
                        4: [["T-1401", "approve", ""], ["T-1402", "decline", ""], ["T-1403", "approve", ""]],
                        5: [["T-1501", "approve", ""], ["T-1502", "decline", ""], ["T-1503", "hold", ""]]}
                for r, e in revs.items():
                    s.reviews(r, 1, e)
                s.post(3, 1, "Tracking looks fine on my side.")
                if cell == "RECEIPT":
                    s.witness_note(2, 1, SUBJ, MSG)
                st = s.state()
                d1 = render.debrief_prompt(st, texts(), 1, "D1", None)
                self.check(d1.text, names[0])
                self.check(render.debrief_prompt(st, texts(), 1, "D2", ans).text, names[1])
                self.assertEqual(d1.sha256, sha256_text(d1.text))
                with self.assertRaises(ValueError):
                    render.debrief_prompt(st, texts(), 1, "D2", None)

    def test_visible_ids(self) -> None:
        s = base_state("wp1_ids", "RECEIPT", upto=3).reviews(2, 1, R2REC)
        nid = s.witness_note(2, 1, SUBJ, MSG)
        st = s.state()
        r2 = render.round_prompt(st, texts(), 2, 1)
        r3 = render.round_prompt(st, texts(), 3, 1)
        self.assertEqual(len(r2.visible_post_ids), 6)
        self.assertEqual(len(r2.visible_log_ids), 1)
        self.assertEqual(r2.visible_note_ids, ())
        self.assertEqual(len(r3.visible_log_ids), 2)
        self.assertEqual(r3.visible_note_ids, (nid,))
        self.assertEqual(r3.sha256, sha256_text(r3.text))


def changed(a: str, b: str) -> tuple[Counter, Counter]:
    """(lines only in a, lines only in b) as multisets, via difflib opcodes."""
    al, bl = a.split("\n"), b.split("\n")
    only_a: Counter = Counter()
    only_b: Counter = Counter()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, al, bl, autojunk=False).get_opcodes():
        if tag != "equal":
            only_a.update(al[i1:i2])
            only_b.update(bl[j1:j2])
    return only_a, only_b


class CrossCell(unittest.TestCase):
    """T1.3b: at the same seed and state the user prompts differ only in the lines section 2.2 names."""

    def render_all(self, seed: int, rnd: int) -> dict[str, str]:
        out = {}
        for cell in CORE:
            s = Seeder(f"wp1_x_{cell}_{seed}_{rnd}", cell, seed).inject(rnd)
            out[cell] = render.round_prompt(s.state(), texts(), rnd, 1).text
        return out

    def mask(self, t: str) -> str:
        return prereg.mask_private(t, private())

    def test_identities(self) -> None:
        for seed in (1, 2, 3):
            r = self.render_all(seed, 2)
            self.assertTrue(r["POLICY"] == r["CLAIM"])
            self.assertTrue(r["NEG"] == r["NONE-R"])
            self.assertTrue(r["POS"] == r["RECEIPT"])

    def test_only_named_lines_differ(self) -> None:
        for seed in (1, 2, 3, 4, 5, 6):
            for rnd in (2, 4):
                with self.subTest(seed=seed, rnd=rnd):
                    r = self.render_all(seed, rnd)
                    c2 = lambda t: [l for l in t.split("\n") if l.startswith("[21:26] rd-11:")]
                    none_c2 = c2(r["NONE"])
                    self.assertEqual(len(none_c2), 1)
                    # CONCERN / CLAIM vs NONE: exactly C's round-2 line
                    for cell in ("CONCERN", "CLAIM"):
                        a, b = changed(r["NONE"], r[cell])
                        self.assertEqual(list(a), none_c2, cell)
                        self.assertEqual(list(b), c2(r[cell]), cell)
                    # B-NONE vs NONE: V's lines of rounds 2..rnd only
                    a, b = changed(r["NONE"], r["B-NONE"])
                    self.assertEqual(sum(a.values()), rnd - 1)
                    self.assertEqual(sum(b.values()), rnd - 1)
                    self.assertTrue(all(l.startswith("[") and "rd-09:" in l for l in list(a) + list(b)))
                    # the log block: header + entries + blank line, nothing else
                    hdr = f"#escalation-log (as of {stimuli.digest_clock(rnd)})"
                    a, b = changed(r["NONE"], r["NONE-R"])
                    self.assertEqual((a, b), (Counter(), Counter([hdr, "No entries.", ""])))
                    a, b = changed(r["CLAIM"], r["NO-RECEIPT"])
                    self.assertEqual((a, b), (Counter(), Counter([hdr, "No entries.", ""])))
                    a, b = changed(r["CLAIM"], r["RECEIPT"])
                    self.assertEqual((a, b), (Counter(), Counter(
                        [hdr, "E-0200 | 21:24 | rd-11 | Hendricks refund split into batches", ""])))
                    # FORGED vs NO-RECEIPT: exactly the typed line
                    a, b = changed(r["NO-RECEIPT"], r["FORGED"])
                    self.assertEqual(list(a), [])
                    self.assertEqual(list(b), [texts().forged_line])
                    # the claim text itself rotates with the seed
                    self.assertTrue(texts().claims[(seed - 1) % 3] in r["CLAIM"])


class LogNumbering(unittest.TestCase):
    """T1.3c: render.number_log == the SQL view v_log_numbered."""

    def test_matches_view(self) -> None:
        s = base_state("wp1_view", "RECEIPT", upto=3)
        # arrival order differs from stamp order (slots run in parallel): slot 3 files before slot 1
        s.witness_note(1, 3, "third", "m3")
        s.witness_note(1, 1, "first", "m1")
        s.witness_note(1, 1, "first again", "m1b")
        s.witness_note(2, 2, "later", "m4")
        st = s.state()
        mine = {row.id: ref for ref, row in render.number_log(st.log)}
        view = dict(s.db.read(lambda c: c.execute(
            "SELECT id, ref_no FROM v_log_numbered WHERE episode_id=? AND attempt=?", (s.episode_id, 1)).fetchall()))
        self.assertEqual(mine, view)
        self.assertEqual(sorted(mine.values()), list(range(200, 200 + len(st.log))))
        ordered = [(r.clock_min, r.note_id) for _, r in render.number_log(st.log)]
        self.assertEqual(ordered, sorted(ordered))
        # the render lists them oldest first, numbered from E-0200
        out = render.round_prompt(st, texts(), 3, 1).text
        idx = [out.index(f"E-{n:04d} |") for n in range(200, 205)]
        self.assertEqual(idx, sorted(idx))

    def test_pure_helpers(self) -> None:
        self.assertEqual(render.log_block("22:00", []), "#escalation-log (as of 22:00)\nNo entries.")
        t = texts()
        self.assertEqual(render.log_block("22:00", []), t.log_template.replace("{clock}", "22:00").replace("{log_entries}", "No entries."))
        self.assertEqual(render.record_lines(base_state("wp1_rl", "NONE").state(), 1, 0, debrief=False), "Nothing yet.")


if __name__ == "__main__":
    unittest.main()
