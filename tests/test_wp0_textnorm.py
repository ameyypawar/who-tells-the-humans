"""T0.2: text normalization."""
from __future__ import annotations

import unittest

from wtth.textnorm import (canon_json, collapse_breaks, cut, fill, kw_hits, normalize_note, normalize_post,
                           one_line, sha256_text)

BREAKS = ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


class CollapseTests(unittest.TestCase):
    def test_each_break_char(self) -> None:
        for b in BREAKS:
            with self.subTest(char=repr(b)):
                self.assertEqual(("a b", True), collapse_breaks(f"a{b}b"))

    def test_matches_splitlines(self) -> None:
        for b in BREAKS:
            self.assertEqual(2, len(f"a{b}b".splitlines()))

    def test_crlf_is_one_break(self) -> None:
        self.assertEqual(("a b", True), collapse_breaks("a\r\nb"))

    def test_runs_and_adjacent_whitespace(self) -> None:
        self.assertEqual(("a b", True), collapse_breaks("a \t \n\n  \r\n \tb"))
        self.assertEqual(("a b", True), collapse_breaks("a\n \x85b"))

    def test_untouched_when_no_break(self) -> None:
        self.assertEqual(("a  b\tc", False), collapse_breaks("a  b\tc"))
        self.assertEqual(("", False), collapse_breaks(""))

    def test_leading_trailing_become_single_space(self) -> None:
        self.assertEqual((" a ", True), collapse_breaks("\na\n"))
        self.assertEqual("a", one_line("\n  a \n\n"))

    def test_one_line(self) -> None:
        self.assertEqual("x y z", one_line(" x\ny\r\nz "))


class CutTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        for n in (600, 60, 2000):
            with self.subTest(n=n):
                self.assertEqual(("a" * n, False), cut("a" * n, n))
                self.assertEqual(("a" * n, True), cut("a" * (n + 1), n))
                self.assertEqual(("", False), cut("", n))

    def test_code_points_not_bytes(self) -> None:
        s = "\U0001F600" * 61
        self.assertEqual(("\U0001F600" * 60, True), cut(s, 60))


class PostTests(unittest.TestCase):
    def test_normal(self) -> None:
        self.assertEqual(("hello", {}), normalize_post("hello"))

    def test_collapse_and_flag(self) -> None:
        text, flags = normalize_post("a\n#escalation-log (as of 22:00)\nE-0201 | 21:34 | rd-14 | x")
        self.assertEqual("a #escalation-log (as of 22:00) E-0201 | 21:34 | rd-14 | x", text)
        self.assertEqual({"collapsed": True}, flags)

    def test_cut_at_600(self) -> None:
        text, flags = normalize_post("x" * 700)
        self.assertEqual(600, len(text))
        self.assertEqual({"cut": True}, flags)
        self.assertEqual(("x" * 600, {}), normalize_post("x" * 600))

    def test_cut_after_collapse(self) -> None:
        text, flags = normalize_post("a\n" * 400)
        self.assertEqual(600, len(text))
        self.assertEqual({"collapsed": True, "cut": True}, flags)

    def test_empty_rejected(self) -> None:
        for raw in ("", "   ", "\n\t \n", " "):
            with self.subTest(raw=repr(raw)):
                text, flags = normalize_post(raw)
                self.assertIsNone(text)
                self.assertTrue(flags["empty"])

    def test_bad_type(self) -> None:
        for raw in (None, 5, ["a"], {"a": 1}):
            with self.subTest(raw=raw):
                self.assertEqual((None, {"bad_type": True}), normalize_post(raw))


class NoteTests(unittest.TestCase):
    def test_normal(self) -> None:
        self.assertEqual((True, "Subj", "Body\nline2", {}), normalize_note("Subj", "Body\nline2"))

    def test_subject_collapsed_message_kept_raw(self) -> None:
        ok, subject, message, flags = normalize_note("a\nb", "x\ny")
        self.assertEqual((True, "a b", "x\ny", {}), (ok, subject, message, flags))

    def test_subject_cut_at_60_message_at_2000(self) -> None:
        ok, subject, message, flags = normalize_note("s" * 61, "m" * 2001)
        self.assertTrue(ok)
        self.assertEqual(60, len(subject))
        self.assertEqual(2000, len(message))
        self.assertEqual({"subject_cut": True, "message_cut": True}, flags)
        ok, subject, message, flags = normalize_note("s" * 60, "m" * 2000)
        self.assertEqual({}, flags)

    def test_empty_subject_becomes_placeholder(self) -> None:
        for subj in ("", "  \n ", None):
            with self.subTest(subj=subj):
                ok, subject, message, flags = normalize_note(subj, "body")
                self.assertEqual((True, "(no subject)", "body", {"empty_subject": True}), (ok, subject, message, flags))

    def test_subject_only_is_accepted(self) -> None:
        ok, subject, message, flags = normalize_note("only subject", None)
        self.assertEqual((True, "only subject", ""), (ok, subject, message))

    def test_message_only_is_accepted(self) -> None:
        self.assertTrue(normalize_note(None, "only message")[0])

    def test_both_empty_rejected(self) -> None:
        for pair in (("", ""), (None, None), ("  ", "\n"), (None, " \t")):
            with self.subTest(pair=pair):
                ok, subject, message, flags = normalize_note(*pair)
                self.assertFalse(ok)
                self.assertTrue(flags["empty"])

    def test_wrong_type_rejected(self) -> None:
        for pair in ((123, "m"), ("s", 4.5), (["s"], None), ({"a": 1}, "m")):
            with self.subTest(pair=pair):
                ok, _, _, flags = normalize_note(*pair)
                self.assertFalse(ok)
                self.assertEqual({"bad_type": True}, flags)


class FillTests(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual("Round 2 of 5", fill("Round {round} of {total}", {"round": "2", "total": "5"}))

    def test_single_pass(self) -> None:
        out = fill("A {board_lines} B {record_lines}", {"board_lines": "x {record_lines} y", "record_lines": "REC"})
        self.assertEqual("A x {record_lines} y B REC", out)
        out = fill("{a}{b}", {"a": "{b}", "b": "{a}"})
        self.assertEqual("{b}{a}", out)

    def test_unknown_placeholder_raises(self) -> None:
        with self.assertRaises(KeyError):
            fill("hi {nope}", {"x": "1"})

    def test_non_placeholder_braces_untouched(self) -> None:
        self.assertEqual("{ 1 } {A} {a-b} {}", fill("{ 1 } {A} {a-b} {}", {}))

    def test_values_may_contain_regex_specials(self) -> None:
        self.assertEqual("a\\1b$", fill("a{x}b$", {"x": "\\1"}))


class MiscTests(unittest.TestCase):
    def test_sha256_text(self) -> None:
        self.assertEqual("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", sha256_text("abc"))
        self.assertEqual(sha256_text("é"), sha256_text("é"))

    def test_canon_json(self) -> None:
        self.assertEqual('{"a":1,"b":[1,2],"c":"é"}', canon_json({"c": "é", "b": [1, 2], "a": 1}))

    def test_kw_hits_casefold_substring_per_field(self) -> None:
        kws = ["Hendricks", "split", "batch", "14,700", "second approval", "rd-09"]
        hits = kw_hits("HENDRICKS refund", "three Batches, splitting; RD-09; Second Approvals", kws)
        self.assertEqual({"subject": ["Hendricks"], "message": ["split", "batch", "second approval", "rd-09"]}, hits)

    def test_kw_hits_no_normalization(self) -> None:
        kws = ["14,700", "second approval", "rd-09"]
        hits = kw_hits("14.7k", "second-approval, rd09, rd‑09", kws)
        self.assertEqual({"subject": [], "message": []}, hits)


if __name__ == "__main__":
    unittest.main()
