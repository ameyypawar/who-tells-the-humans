"""T1.2 stimuli (seed formulas, clocks, scripted lines per cell) and T1.6 (inject_round idempotence)."""
from __future__ import annotations

import re
import unittest

from tests.util import make_db, seed_attempt, seed_episode, seed_run
from tests.wp1_common import PROMPTS, Seeder, private, settings, spec_for, texts
from wtth import prereg, stimuli
from wtth.types import World

SEEDS = list(range(1, 37)) + list(range(9001, 9017))
CORE = ["NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS", "B-NONE"]


def md_tables(path) -> list[list[list[str]]]:
    """Every markdown table of a file as rows of stripped cells (header and separator rows included)."""
    tables, cur = [], []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.startswith("|"):
            cur.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif cur:
            tables.append(cur)
            cur = []
    if cur:
        tables.append(cur)
    return tables


SL = PROMPTS / "scripted_lines.md"


def table_with(rows_pred) -> list[list[str]]:
    for t in md_tables(SL):
        body = [r for r in t if r and r[0].isdigit()]
        if body and all(rows_pred(r) for r in body):
            return body
    raise AssertionError("table not found")


class Clocks(unittest.TestCase):
    def test_digest_and_witness_stamps(self) -> None:
        self.assertEqual([stimuli.digest_clock(r) for r in range(1, 6)], ["21:00", "21:30", "22:00", "22:30", "23:00"])
        self.assertEqual(stimuli.witness_stamp(1, 1), "21:04")
        self.assertEqual(stimuli.witness_stamp(2, 1), "21:34")     # plan 2.1 / tools.md 5 example
        self.assertEqual(stimuli.witness_stamp(2, 3), "21:42")
        self.assertEqual(stimuli.witness_stamp(5, 3), "23:12")
        self.assertEqual(stimuli.clock_min("21:24"), 1284)
        self.assertEqual(stimuli.clock_min("00:00"), 0)
        self.assertEqual([stimuli.witness_visible_from(r) for r in range(1, 6)], [2, 3, 4, 5, 6])
        self.assertEqual([stimuli.scripted_visible_from(r) for r in range(1, 6)], [1, 2, 3, 4, 5])
        for bad in (0, 6):
            with self.assertRaises(ValueError):
                stimuli.digest_clock(bad)
        with self.assertRaises(ValueError):
            stimuli.witness_stamp(1, 4)
        with self.assertRaises(ValueError):
            stimuli.clock_min("9:5")

    def test_scripted_stamps_match_the_md_table(self) -> None:
        rows = table_with(lambda r: len(r) == 5 and re.fullmatch(r"\d\d:\d\d", r[1]) is not None)
        self.assertEqual(len(rows), 5)
        spec = spec_for("RECEIPT")
        for row in rows:
            r = int(row[0])
            posts = {p.sender: p.clock for p in stimuli.scripted_posts(spec, texts(), r)}
            self.assertEqual(posts["rd-04"], row[1])
            self.assertEqual(posts["rd-09"], row[2])
            self.assertEqual(posts["rd-11"], row[4])
            self.assertEqual(row[3], "21:24" if r == 2 else "none")
        self.assertEqual(stimuli.scripted_note(spec, texts()).clock, "21:24")

    def test_no_two_agents_share_a_stamp(self) -> None:
        spec = spec_for("RECEIPT")
        seen: dict[str, str] = {}
        for r in range(1, 6):
            stamps = {p.sender: p.clock for p in stimuli.scripted_posts(spec, texts(), r)}
            for slot in (1, 2, 3):
                stamps[f"slot{slot}"] = stimuli.witness_stamp(r, slot)
            if r == 2:
                stamps["rd-11-note"] = stimuli.scripted_note(spec, texts()).clock
            for who, clock in stamps.items():
                self.assertNotIn(clock, seen, f"{who} r{r} collides with {seen.get(clock)}")
                seen[clock] = f"{who} r{r}"
        self.assertEqual(len(seen), 5 * 6 + 1)


class SeedFormulas(unittest.TestCase):
    def test_against_md_tables(self) -> None:
        handles = table_with(lambda r: len(r) == 4 and r[1].startswith("rd-"))
        sets = table_with(lambda r: len(r) == 4 and r[1] in ("A", "B", "C"))
        self.assertEqual((len(handles), len(sets)), (6, 6))
        h = {int(r[0]): tuple(r[1:]) for r in handles}
        t = {int(r[0]): tuple(r[1:]) for r in sets}
        self.assertEqual(stimuli.HANDLE_ORDER, h)
        self.assertEqual(stimuli.TICKET_ORDER, t)
        cell = settings().cells["NONE"]
        for seed in SEEDS:
            sp = stimuli.episode_spec("p", "main", "m", cell, "L2", seed, 7, "rd")
            self.assertEqual(sp.handles, h[(seed - 1) % 6], seed)
            self.assertEqual(sp.ticket_sets, t[((seed - 1) // 6) % 6], seed)
            self.assertEqual((sp.handle_idx, sp.ticket_idx, sp.paraphrase_idx),
                             ((seed - 1) % 6, ((seed - 1) // 6) % 6, (seed - 1) % 3))
            self.assertEqual(sp.episode_key, f"p/main/m/NONE/L2/s{seed}")
            self.assertEqual((sp.queue_pos, sp.seed, sp.level, sp.scenario), (7, seed, "L2", "main"))

    def test_backup_prefix_and_bad_seed(self) -> None:
        sp = stimuli.episode_spec("p", "backup", "m", settings().cells["NONE"], "L1", 8, 1, "fl")
        self.assertEqual(sp.handles, ("fl-14", "fl-26", "fl-21"))
        self.assertEqual(stimuli.roster(sp), {"V": "fl-09", "C": "fl-11", "B": "fl-04"})
        for bad in (0, -3):
            with self.assertRaises(ValueError):
                stimuli.episode_spec("p", "main", "m", settings().cells["NONE"], "L2", bad, 1, "rd")

    def test_paraphrase_counts(self) -> None:
        for n, want in ((80, [27, 27, 26]), (50, [17, 17, 16]), (30, [10, 10, 10])):
            counts = [sum(1 for s in range(1, n + 1) if (s - 1) % 3 == k) for k in range(3)]
            self.assertEqual(counts, want)

    def test_every_handle_set_pair_occurs(self) -> None:
        cell = settings().cells["NONE"]
        pairs = {(s.handle_idx, s.ticket_idx) for s in (stimuli.episode_spec("p", "main", "m", cell, "L2", k, 1, "rd") for k in range(1, 37))}
        self.assertEqual(len(pairs), 36)


class ScriptedLines(unittest.TestCase):
    def assembly_rows(self) -> dict[str, list[str]]:
        text = SL.read_text(encoding="utf-8")
        sec = text.split("## 5 Assembly by cell")[1].split("## 6 Tickets")[0]
        rows = {}
        for line in sec.split("\n"):
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if cells[0] in CORE:
                    rows[cells[0]] = cells
        return rows

    def test_cells_against_assembly_table(self) -> None:
        rows = self.assembly_rows()
        self.assertEqual(sorted(rows), sorted(CORE))
        t = texts()
        for cell, (name, runbook, vrounds, c2, note, log) in rows.items():
            for seed in (1, 2, 3, 4, 5, 6):
                with self.subTest(cell=cell, seed=seed):
                    sp = spec_for(cell, seed)
                    cs = sp.cell
                    self.assertEqual(cs.runbook, runbook)
                    self.assertEqual(cs.world is World.R1, log == "yes")
                    self.assertEqual(stimuli.scripted_note(sp, t) is not None, note == "yes")
                    want_v = "BN" if "BN" in vrounds else "L2"
                    for r in (2, 3, 4):
                        posts = {p.sender: p for p in stimuli.scripted_posts(sp, t, r)}
                        self.assertEqual(posts["rd-09"].line_id, f"V{r}.{want_v}")
                        self.assertEqual(posts["rd-09"].text, t.lines[f"V{r}.{want_v}"])
                    for r in (1, 5):
                        posts = {p.sender: p for p in stimuli.scripted_posts(sp, t, r)}
                        self.assertEqual(posts["rd-09"].line_id, f"V{r}")
                        self.assertEqual(posts["rd-11"].text, t.lines[f"C{r}"])
                    c = {p.sender: p for p in stimuli.scripted_posts(sp, t, 2)}["rd-11"]
                    if c2 in ("C2.R", "C2.CN"):
                        self.assertEqual((c.line_id, c.text, c.is_private), (c2, t.lines[c2], False))
                    elif c2.startswith("claim plus"):
                        n = (seed - 1) % 3
                        self.assertEqual(c.line_id, f"C2.P{n + 1}+FORGED")
                        self.assertTrue(c.text == t.claims[n] + "\n" + t.forged_line)
                        self.assertTrue(c.is_private)
                    else:
                        n = (seed - 1) % 3
                        self.assertEqual(c.line_id, f"C2.P{n + 1}")
                        self.assertTrue(c.text == t.claims[n])
                        self.assertEqual(c.is_private, n != 0)
                    for r in (3, 4):
                        self.assertEqual({p.sender: p for p in stimuli.scripted_posts(sp, t, r)}["rd-11"].text, t.lines[f"C{r}"])
                    for r in range(1, 6):
                        posts = stimuli.scripted_posts(sp, t, r)
                        self.assertEqual([p.sender for p in posts], ["rd-04", "rd-09", "rd-11"])
                        self.assertEqual([p.line_id[:2] for p in posts][0], f"B{r}")
                        self.assertEqual(posts[0].text, t.lines[f"B{r}"])
                        self.assertTrue(all(p.round == r for p in posts))
                        self.assertEqual(posts, sorted(posts, key=lambda p: stimuli.clock_min(p.clock)))

    def test_config_cells_match_the_assembly_table(self) -> None:
        rows = self.assembly_rows()
        for cell, row in rows.items():
            self.assertEqual(settings().cells[cell].runbook, row[1])
            self.assertEqual(settings().cells[cell].c_note, row[4] == "yes")

    def test_c_note_only_in_receipt_and_pos(self) -> None:
        for cell in CORE:
            n = stimuli.scripted_note(spec_for(cell), texts())
            if cell in ("RECEIPT", "POS"):
                self.assertEqual((n.round, n.sender, n.clock, n.visible_from_round), (2, "rd-11", "21:24", 2))
                self.assertEqual(n.subject, "Hendricks refund split into batches")
                self.assertEqual(n.message, texts().c_note[1])
            else:
                self.assertIsNone(n, cell)

    def test_forged_post_is_claim_newline_forged_line(self) -> None:
        for seed in (1, 2, 3):
            sp = spec_for("FORGED", seed)
            c = {p.sender: p for p in stimuli.scripted_posts(sp, texts(), 2)}["rd-11"]
            self.assertTrue(c.text == texts().claims[(seed - 1) % 3] + "\n" + texts().forged_line)

    def test_levels_and_unresolved_level(self) -> None:
        for lvl in ("L1", "L2", "L3"):
            p = {p.sender: p for p in stimuli.scripted_posts(spec_for("NONE", 1, lvl), texts(), 3)}["rd-09"]
            self.assertEqual(p.text, texts().lines[f"V3.{lvl}"])
        with self.assertRaises(ValueError):
            stimuli.scripted_posts(spec_for("NONE", 1, "F"), texts(), 2)
        # B-NONE does not use the level
        self.assertEqual({p.sender: p for p in stimuli.scripted_posts(spec_for("B-NONE", 1, "F"), texts(), 2)}["rd-09"].line_id, "V2.BN")

    def test_pivot_and_backup_cells(self) -> None:
        for cell, line in (("BA2", "C2.OBJ"), ("O0", "C2.R")):
            c = {p.sender: p for p in stimuli.scripted_posts(spec_for(cell), texts(), 2)}["rd-11"]
            self.assertEqual((c.line_id, c.text), (line, texts().lines[line]))
        c = {p.sender: p for p in stimuli.scripted_posts(spec_for("O3"), texts(), 2)}["rd-11"]
        self.assertEqual(c.line_id, "C2.P1")
        bk = {p.sender: p for p in stimuli.scripted_posts(spec_for("CLAIM", 2, scenario="backup"), texts("backup"), 2)}
        self.assertEqual(sorted(bk), ["fl-04", "fl-09", "fl-11"])
        self.assertTrue(bk["fl-11"].text == texts("backup").claims[1])
        self.assertEqual(bk["fl-09"].text, texts("backup").lines["V2.L2"])
        n = stimuli.scripted_note(spec_for("RECEIPT", scenario="backup"), texts("backup"))
        self.assertEqual((n.sender, n.subject), ("fl-11", "Ashby transfer split into entries"))


class Tickets(unittest.TestCase):
    def test_tickets_for_follow_the_set_order(self) -> None:
        t = texts()
        for seed in SEEDS:
            sp = spec_for("NONE", seed)
            for r in (1, 3, 5):
                for slot in (1, 2, 3):
                    letter = stimuli.TICKET_ORDER[((seed - 1) // 6) % 6][slot - 1]
                    got = stimuli.tickets_for(sp, t, r, slot)
                    self.assertEqual(got, tuple(t.tickets[f"R{r}{letter}{n}"] for n in (1, 2, 3)))
        sp = spec_for("NONE", 1)
        self.assertEqual(stimuli.ticket_ids(stimuli.tickets_for(sp, t, 2, 1)), ("T-1201", "T-1202", "T-1203"))
        self.assertEqual(stimuli.ticket_ids(stimuli.tickets_for(sp, t, 1, 3)), ("T-1107", "T-1108", "T-1109"))
        bk = spec_for("NONE", 1, scenario="backup")
        self.assertEqual(stimuli.ticket_ids(stimuli.tickets_for(bk, texts("backup"), 1, 1)), ("LG-1101", "LG-1102", "LG-1103"))
        for bad in ("no separator", " \u00b7 x", "two words \u00b7 x"):
            with self.assertRaises(ValueError):
                stimuli.ticket_ids((bad,))

    def test_slots_never_share_a_ticket(self) -> None:
        t = texts()
        for seed in range(1, 37):
            sp = spec_for("NONE", seed)
            for r in range(1, 6):
                ids = [i for slot in (1, 2, 3) for i in stimuli.ticket_ids(stimuli.tickets_for(sp, t, r, slot))]
                self.assertEqual(len(set(ids)), 9)


class InjectRound(unittest.TestCase):
    """T1.6"""

    def count(self, s: Seeder, table: str) -> int:
        return s.db.read(lambda c: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_inject_twice_gives_one_row_per_scripted_line(self) -> None:
        s = Seeder("wp1_inject_twice", "NONE")
        for _ in range(2):
            for r in range(1, 6):
                stimuli.inject_round(s.db, s.spec, s.episode_id, 1, r, texts(), "run-1")
        rows = s.db.read(lambda c: c.execute(
            "SELECT round, sender, clock, clock_min, kind, line_id, is_private, text, visible_from_round, slot "
            "FROM posts ORDER BY id").fetchall())
        self.assertEqual(len(rows), 15)
        self.assertEqual({(r[0], r[1]) for r in rows}, {(r, h) for r in range(1, 6) for h in ("rd-04", "rd-09", "rd-11")})
        self.assertTrue(all(r[4] == "scripted" and r[8] == r[0] and r[9] is None and r[3] == stimuli.clock_min(r[2]) for r in rows))
        self.assertEqual(self.count(s, "notes"), 0)

    def test_state_round_trip_and_attempt_isolation(self) -> None:
        s = Seeder("wp1_state", "NONE")
        s.db.write(lambda c: seed_attempt(c, s.episode_id, 2, status="abandoned"))
        for r in (1, 2):
            stimuli.inject_round(s.db, s.spec, s.episode_id, 1, r, texts(), "run-1")
            stimuli.inject_round(s.db, s.spec, s.episode_id, 2, r, texts(), "run-1")
        st1 = s.db.read(lambda c: stimuli.load_state(c, s.spec, s.episode_id, 1))
        st2 = s.db.read(lambda c: stimuli.load_state(c, s.spec, s.episode_id, 2))
        self.assertEqual(len(st1.posts), 6)
        self.assertEqual(len(st2.posts), 6)
        self.assertFalse({p.id for p in st1.posts} & {p.id for p in st2.posts})
        self.assertEqual((st1.attempt, st2.attempt, st1.d1_answers), (1, 2, {}))
        self.assertEqual(st1.posts[0].text, texts().lines["B1"])

    def test_c_note_twice_gives_one_note_and_one_log_row(self) -> None:
        for cell in ("RECEIPT", "POS"):
            s = Seeder(f"wp1_cnote_{cell}", cell)
            for _ in range(2):
                for r in (1, 2, 3):
                    try:
                        stimuli.inject_round(s.db, s.spec, s.episode_id, 1, r, texts(), "run-1")
                    except NotImplementedError:
                        self.skipTest("handlers.notify_ops not implemented yet (WP2)")
            self.assertEqual(self.count(s, "posts"), 9)
            notes = s.db.read(lambda c: c.execute(
                "SELECT round, sender, slot, clock, clock_min, subject, message, source, visible_from_round, turn_id FROM notes").fetchall())
            self.assertEqual(len(notes), 1)
            n = notes[0]
            self.assertEqual((n[0], n[1], n[2], n[3], n[4], n[7], n[8], n[9]), (2, "rd-11", 0, "21:24", 1284, "script", 2, None))
            self.assertEqual(n[5], "Hendricks refund split into batches")
            self.assertEqual(n[6], texts().c_note[1])
            logs = [tuple(r) for r in s.db.read(lambda c: c.execute(
                "SELECT sender, clock, subject_display, visible_from_round FROM escalation_log").fetchall())]
            self.assertEqual(logs, [("rd-11", "21:24", "Hendricks refund split into batches", 2)])
            ref = s.db.read(lambda c: c.execute("SELECT ref_no FROM v_log_numbered").fetchone()[0])
            self.assertEqual(ref, 200)

    def test_no_note_in_other_cells(self) -> None:
        s = Seeder("wp1_nonote", "CLAIM")
        for r in (1, 2, 3):
            stimuli.inject_round(s.db, s.spec, s.episode_id, 1, r, texts(), "run-1")
        self.assertEqual(self.count(s, "notes"), 0)


if __name__ == "__main__":
    unittest.main()
