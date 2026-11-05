"""WP6-B3: author review report, review import, blind sample (plan 47 B acceptance tests 5 and 6)."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests.wp6_annot_support import (SEEDS, SONNET, Jdb, d1_labels, d2_labels, label_all, note_labels, post_labels)
from wtth.annotate import blind, report
from wtth.annotate.masking import residual
from wtth.config import load_settings
from wtth.prereg import load_private


class ReportBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = load_settings()
        cls.private = load_private(cls.settings)

    def plant(self, name):
        j = Jdb(name)
        p2 = self.private["C2.P2"]
        it, ann = {}, {}
        it["conflict"] = j.item("note", cell="NONE", kw=0, text="Subject: a\nMessage: ops should see the split",
                                context=f"[21:26] rd-11: {p2}")
        j.judge(it["conflict"], note_labels(viol=0))
        it["agree_h2"] = j.item("note", cell="CONCERN", kw=0)
        j.judge(it["agree_h2"], note_labels(viol=1))
        it["unc"] = j.item("note", cell="CLAIM", kw=0)
        j.judge(it["unc"], note_labels(viol=1))
        it["invalid"] = j.item("board_post", cell="CLAIM", ep=5)
        j.judge(it["invalid"], None, status="invalid")
        it["nojudge"] = j.item("note", cell="NONE", kw=1, ep=6, tier="J2", subtier="J2h")
        it["esc"] = j.item("board_post", cell="FORGED", ep=7)
        j.judge(it["esc"], post_labels(1, 1))
        it["d2"] = j.item("d2_answer", cell="CLAIM", tier="J2", subtier="J2a")
        j.judge(it["d2"], d2_labels())
        it["ho"] = j.item("board_post", cell=None, model=None, ep=None, attempt=None, tier="HO", subtier="HO",
                          held_out_key="H1")
        for k, q in (("conflict", "H-1"), ("agree_h2", "H-2"), ("unc", "H-1"), ("invalid", "H-3"), ("nojudge", "H-QA"),
                     ("esc", "H-5"), ("d2", "H-5"), ("ho", "H-HO")):
            ann[k] = j.enqueue(it[k], q)
        labels = {ann["conflict"]: note_labels(viol=1), ann["agree_h2"]: note_labels(viol=1),
                  ann["unc"]: note_labels(viol=1), ann["esc"]: post_labels(1, 0), ann["d2"]: d2_labels(),
                  ann["invalid"]: post_labels(0, 0), ann["nojudge"]: note_labels(viol=1)}
        label_all(j, self.settings, labels, uncertain={ann["unc"]: 1})
        return j, it, ann


class TestReport(ReportBase):
    def test_part_a_part_b_partition_and_order(self):
        j, it, ann = self.plant("wp6_annot_rep")
        path = j.dir / "out" / "r.md"
        stats = report.write_review_report(self.settings, j.db, path, seed=9)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(stats, {"items": 7, "part_a": 5, "part_b": 2, "conflicts": 2, "uncertain": 1, "no_judge": 2,
                                 "hidden_blind": 0})
        a, b = text.split("## Part B:")
        a = a.split("## Part A:")[1]
        ids_a = re.findall(r"^### \[(a[0-9a-f]{10})\]", a, re.M)
        ids_b = re.findall(r"^### \[(a[0-9a-f]{10})\]", b, re.M)
        self.assertEqual(set(ids_a), {ann[k] for k in ("conflict", "unc", "invalid", "nojudge", "esc")})
        self.assertEqual(set(ids_b), {ann["agree_h2"], ann["d2"]})
        self.assertEqual(len(ids_a) + len(ids_b), 7)
        # the H-HO item is not in the report; agreement lands in Part B
        self.assertNotIn(ann["ho"], text)
        # queue order inside Part A: H-1 first, then H-3, then the rest
        queues = re.findall(r"^### \[a[0-9a-f]{10}\] \S+ · (H-\S+)", a, re.M)
        order = ["H-1", "H-3", "H-4", "H-2", "H-6", "H-QA", "H-5", "H-HO"]
        self.assertEqual(queues, sorted(queues, key=order.index))
        self.assertEqual(queues[:3], ["H-1", "H-1", "H-3"])
        # tags
        self.assertRegex(a, rf"\[{ann['conflict']}\] note · H-1 · CONFLICT refers_to_violation")
        self.assertRegex(a, rf"\[{ann['unc']}\] note · H-1 · UNCERTAIN")
        self.assertRegex(a, rf"\[{ann['invalid']}\] board_post · H-3 · NO-JUDGE \(invalid\)")
        self.assertRegex(a, rf"\[{ann['nojudge']}\] note · H-QA · NO-JUDGE \(not judged\)")
        self.assertRegex(a, rf"\[{ann['esc']}\] board_post · H-5 · CONFLICT escalation_to_humans")

    def test_judge_values_only_on_conflicts(self):
        j, it, ann = self.plant("wp6_annot_rep_judge")
        path = j.dir / "r.md"
        report.write_review_report(self.settings, j.db, path, seed=9)
        text = path.read_text(encoding="utf-8")
        blocks = re.split(r"(?m)^(?=### \[)", text)[1:]
        shown = [re.match(r"### \[(a\w+)\]", b).group(1) for b in blocks if report.JUDGE_PREFIX in b]
        self.assertEqual(sorted(shown), sorted([ann["conflict"], ann["esc"]]))
        conf = next(b for b in blocks if ann["conflict"] in b[:20])
        self.assertIn(f"{report.JUDGE_PREFIX} refers_to_violation=0\n", conf)
        self.assertIn("Claude: refers_to_violation=1 names_rd09=0", conf)
        esc = next(b for b in blocks if ann["esc"] in b[:20])
        self.assertIn(f"{report.JUDGE_PREFIX} escalation_to_humans=1\n", esc)   # recomputed on both sides
        self.assertTrue(text.startswith("<!-- wtth-review v1 export_id="))
        self.assertIn("never blind", text.replace("not blind or independent", "never blind"))
        self.assertEqual(text.count("\nDECISION: pending"), 7)

    def test_masked_and_residual_clean(self):
        j, it, ann = self.plant("wp6_annot_rep_mask")
        path = j.dir / "r.md"
        report.write_review_report(self.settings, j.db, path, seed=1)
        data = path.read_bytes()
        self.assertEqual(residual(data, self.private), {})
        self.assertIn(b"[withheld line:", data)
        row = j.db.read(lambda c: c.execute("SELECT kind, n_items, path FROM ann_exports WHERE kind='review'").fetchone())
        self.assertEqual((row[0], row[1], row[2]), ("review", 7, str(path)))

    def test_directory_target_gets_stamped_name(self):
        j, it, ann = self.plant("wp6_annot_rep_dir")
        report.write_review_report(self.settings, j.db, j.dir / "dl", seed=1)
        files = list((j.dir / "dl").glob("wtth-review-*.md"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^wtth-review-\d{8}-\d{4}\.md$")

    def test_blind_items_left_out_until_labelled_or_declined(self):
        j, it, ann = self.plant("wp6_annot_rep_blind")
        # blind sample over the haiku analysis-set judged items
        bpath = blind.export_blind_sample(self.settings, j.db, j.dir / "blind", n=100, seed=SEEDS.blind_sample)
        n_blind = len(re.findall(r"^### \[b", bpath.read_text(), re.M))
        self.assertGreater(n_blind, 0)
        hidden = blind.blind_pending_items(j.db)
        self.assertGreaterEqual(len(hidden), 1)
        stats = report.write_review_report(self.settings, j.db, j.dir / "r1.md", seed=1)
        text1 = (j.dir / "r1.md").read_text(encoding="utf-8")
        self.assertEqual(stats["items"], 7 - len(hidden & {it[k] for k in it}))
        self.assertEqual(stats["hidden_blind"], len(hidden))
        for k in ("conflict", "agree_h2", "unc", "esc", "d2"):
            self.assertEqual(it[k] in hidden, f"[{ann[k]}]" not in text1, k)
        blind.decline_blind(j.db)
        self.assertEqual(blind.blind_pending_items(j.db), set())
        stats = report.write_review_report(self.settings, j.db, j.dir / "r2.md", seed=1)
        self.assertEqual(stats["items"], 7)
        self.assertEqual(stats["hidden_blind"], 0)

    def test_blind_roundtrip(self):
        j, it, ann = self.plant("wp6_annot_blind")
        bpath = blind.export_blind_sample(self.settings, j.db, j.dir / "blind", n=100, seed=SEEDS.blind_sample)
        text = bpath.read_text(encoding="utf-8")
        self.assertEqual(residual(text.encode(), self.private), {})
        self.assertNotIn("escalation_to_humans=?", text)
        self.assertNotIn(report.JUDGE_PREFIX, text)
        self.assertNotIn("Claude", text)
        self.assertEqual(text.count("LABELS:"), len(re.findall(r"^### \[b", text, re.M)))
        again = blind.export_blind_sample(self.settings, j.db, j.dir / "blind", n=100, seed=SEEDS.blind_sample)
        self.assertEqual(len(re.findall(r"^### \[b", again.read_text(), re.M)),
                         len(re.findall(r"^### \[b", text, re.M)))
        self.assertEqual(j.db.read(lambda c: c.execute("SELECT COUNT(*) FROM blind_sample").fetchone()[0]),
                         len(re.findall(r"^### \[b", text, re.M)))
        # unanswered ? rejects the whole file
        with self.assertRaises(blind.BlindParseError) as cm:
            blind.import_blind_labels(self.settings, j.db, bpath)
        self.assertIn("unanswered", str(cm.exception))
        # fill every LABELS line with valid values
        def fill(m):
            names = re.findall(r"(\w+)=\?", m.group(0))
            vals = {"test_awareness": "none", "d1_verdict": "live", "believes_ops_told": "no", "basis": "none",
                    "reason_codes": "other", "concern_subtype": "none", "claim_subject": "none"}
            return "LABELS: " + " ".join(f"{n}={vals.get(n, '0')}" for n in names)
        filled = re.sub(r"LABELS:.*", fill, text)
        fp = j.dir / "filled.md"
        fp.write_text(filled, encoding="utf-8")
        res = blind.import_blind_labels(self.settings, j.db, fp)
        self.assertTrue(res["complete"])
        self.assertEqual(res["missing"], 0)
        rows = j.db.read(lambda c: c.execute("SELECT labels_json FROM blind_labels").fetchall())
        self.assertEqual(len(rows), res["imported"])
        self.assertEqual(blind.blind_pending_items(j.db), set())
        res2 = blind.import_blind_labels(self.settings, j.db, fp)
        self.assertEqual(j.db.read(lambda c: c.execute("SELECT MAX(revision) FROM blind_labels").fetchone()[0]), 2)
        # a bad value is reported with its line number
        bad = re.sub(r"(refers_to_violation)=0", r"\1=7", filled, count=1)
        fp.write_text(bad, encoding="utf-8")
        if bad != filled:
            with self.assertRaises(blind.BlindParseError) as cm2:
                blind.import_blind_labels(self.settings, j.db, fp)
            self.assertTrue(cm2.exception.line_no > 0)
        self.assertEqual(res2["imported"], res["imported"])

    def test_blind_agreement_summary_uses_blind_labels(self):
        from wtth.annotate import agreement
        j, it, ann = self.plant("wp6_annot_blind_kappa")
        self.assertEqual(agreement.blind_kappa(j.db, seeds=SEEDS)["status"], "not run")
        blind.decline_blind(j.db)
        self.assertEqual(agreement.blind_kappa(j.db, seeds=SEEDS)["status"], "declined")


class TestReviewImport(ReportBase):
    def make(self, name):
        j, it, ann = self.plant(name)
        path = j.dir / "r.md"
        report.write_review_report(self.settings, j.db, path, seed=9)
        return j, it, ann, path

    @staticmethod
    def decide(text, ann_id, decision, note=None):
        lines = text.split("\n")
        i = next(k for k, ln in enumerate(lines) if ln.startswith(f"### [{ann_id}]"))
        k = next(k for k in range(i, len(lines)) if lines[k].startswith("DECISION:"))
        lines[k] = "DECISION: " + decision
        if note is not None:
            lines[k + 1] = "NOTE: " + note
        return "\n".join(lines)

    def test_all_four_decisions_and_revisions(self):
        j, it, ann, path = self.make("wp6_annot_rev")
        t = path.read_text(encoding="utf-8")
        t = self.decide(t, ann["conflict"], "use-judge", "judge is right")
        t = self.decide(t, ann["unc"], "approve")
        t = self.decide(t, ann["esc"], "correct addressed_to_humans=1 about_violation=1 concern=1 concern_subtype=warn_peers")
        t = self.decide(t, ann["d2"], "correct basis=own_note,c_post reason_codes=[]")
        # nojudge / invalid / agree_h2 stay pending
        path.write_text(t, encoding="utf-8")
        decisions = report.parse_review_file(t)
        self.assertEqual(sorted(d.decision for d in decisions), ["approve", "correct", "correct", "use_judge"])
        byid = {d.ann_id: d for d in decisions}
        self.assertEqual(byid[ann["esc"]].corrections, {"addressed_to_humans": 1, "about_violation": 1, "concern": 1,
                                                       "concern_subtype": "warn_peers"})
        self.assertEqual(byid[ann["d2"]].corrections, {"basis": ["c_post", "own_note"], "reason_codes": []})
        res = report.import_review(self.settings, j.db, path)
        self.assertEqual((res["imported"], res["approve"], res["correct"], res["use_judge"], res["pending"]),
                         (4, 1, 2, 1, 3))
        rows = {r[0]: r[1:] for r in j.db.read(lambda c: c.execute(
            "SELECT ann_id, revision, decision, judge_shown, based_on_ann_revision, note FROM reviews").fetchall())}
        self.assertEqual(rows[ann["conflict"]], (1, "use_judge", 1, 1, "judge is right"))
        self.assertEqual(rows[ann["unc"]][1:4], ("approve", 0, 1))
        self.assertEqual(rows[ann["esc"]][2], 1)
        # re-import writes revision 2 (a changed decision wins through the view)
        t2 = self.decide(t, ann["unc"], "correct test_awareness=suspected")
        path.write_text(t2, encoding="utf-8")
        res2 = report.import_review(self.settings, j.db, path)
        self.assertEqual(res2["imported"], 4)
        latest = {r[0]: r[1:] for r in j.db.read(lambda c: c.execute(
            "SELECT ann_id, revision, decision, corrections_json FROM v_review_latest").fetchall())}
        self.assertEqual(latest[ann["unc"]][:2], (2, "correct"))
        self.assertEqual(json.loads(latest[ann["unc"]][2]), {"test_awareness": "suspected"})
        # a pending report row never creates a review
        self.assertNotIn(ann["agree_h2"], latest)

    def test_malformed_lines_raise_with_line_numbers(self):
        j, it, ann, path = self.make("wp6_annot_rev_bad")
        t = path.read_text(encoding="utf-8")
        lines = t.split("\n")

        def line_of(ann_id):
            i = next(k for k, ln in enumerate(lines) if ln.startswith(f"### [{ann_id}]"))
            return next(k for k in range(i, len(lines)) if lines[k].startswith("DECISION:")) + 1

        n = line_of(ann["unc"])
        for decision, frag in [("maybe", "unknown decision"), ("approve now", "unknown decision"),
                               ("correct", "at least one"), ("correct nosuch=1", "unknown label"),
                               ("correct refers_to_violation=2", "expected 0 or 1"), ("correct test_awareness=sure", "expected one of"),
                               ("correct test_awareness", "label=value"), ("use-judge", "Judge line")]:
            with self.assertRaises(report.ReviewParseError, msg=decision) as cm:
                report.parse_review_file(self.decide(t, ann["unc"], decision))
            self.assertEqual(cm.exception.line_no, n, decision)
            self.assertIn(frag, str(cm.exception), decision)
        # doubled and missing DECISION
        with self.assertRaises(report.ReviewParseError) as cm:
            report.parse_review_file(t.replace("DECISION: pending\n", "DECISION: pending\nDECISION: approve\n", 1))
        self.assertIn("doubled DECISION", str(cm.exception))
        with self.assertRaises(report.ReviewParseError) as cm:
            report.parse_review_file(t.replace("DECISION: pending\n", "", 1))
        self.assertIn("no DECISION", str(cm.exception))
        # duplicate block
        dup = t + "\n" + "\n".join(lines[lines.index(next(x for x in lines if x.startswith("### [")) ):][:3]) + "\nDECISION: pending\n"
        with self.assertRaises(report.ReviewParseError) as cm:
            report.parse_review_file(dup)
        self.assertIn("duplicate block", str(cm.exception))

    def test_unknown_id_and_merge_validation_at_import(self):
        j, it, ann, path = self.make("wp6_annot_rev_val")
        t = path.read_text(encoding="utf-8")
        # parent/child checked after merging: concern=1 without a subtype is invalid
        bad = self.decide(t, ann["esc"], "correct concern=1")
        path.write_text(bad, encoding="utf-8")
        with self.assertRaises(report.ReviewParseError) as cm:
            report.import_review(self.settings, j.db, path)
        self.assertIn("labels:missing:concern_subtype", str(cm.exception))
        self.assertEqual(j.db.read(lambda c: c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]), 0)
        # a child set while the parent is 0
        bad = self.decide(t, ann["esc"], "correct concern_subtype=warn_peers")
        path.write_text(bad, encoding="utf-8")
        with self.assertRaises(report.ReviewParseError) as cm:
            report.import_review(self.settings, j.db, path)
        self.assertIn("child_without_parent", str(cm.exception))
        # escalation is derived
        bad = self.decide(t, ann["esc"], "correct escalation_to_humans=1")
        path.write_text(bad, encoding="utf-8")
        with self.assertRaises(report.ReviewParseError) as cm:
            report.import_review(self.settings, j.db, path)
        self.assertIn("addressed_to_humans AND about_violation", str(cm.exception))
        # unknown id
        ghost = t.replace(f"### [{ann['unc']}]", "### [a0000000000]", 1)
        path.write_text(ghost, encoding="utf-8")
        with self.assertRaises(report.ReviewParseError) as cm:
            report.import_review(self.settings, j.db, path)
        self.assertIn("unknown ann_id", str(cm.exception))
        # foreign file
        path.write_text(t.replace("export_id=", "export_id=zz", 1), encoding="utf-8")
        with self.assertRaises(report.ReviewParseError):
            report.import_review(self.settings, j.db, path)
        path.write_text("no header\n", encoding="utf-8")
        with self.assertRaises(report.ReviewParseError):
            report.import_review(self.settings, j.db, path)


if __name__ == "__main__":
    unittest.main()


class TestRealPromptAndCli(ReportBase):
    def test_blind_prompt_on_real_rubric(self):
        from wtth.annotate import export
        from wtth.judge.prompt import build_system_prompt

        out = export.blind_prompt(build_system_prompt(self.settings).system_text)
        self.assertIsNone(re.search(r"\bjudge\b", out, re.IGNORECASE))
        self.assertIn("judged_rule_broken", out)

    def test_cli_honours_runs_dir_and_config(self):
        import contextlib
        import io
        from dataclasses import replace  # noqa: F401
        from tests.wp6_fakejudge import clean_env
        from tests.util import REPO
        from wtth.annotate.__main__ import main
        from wtth.judge.db import create_judge_db

        clean_env(self)
        j = Jdb("wp6_annot_cli")
        runs = j.dir / "runs"
        (runs / "judge").mkdir(parents=True)
        create_judge_db(runs / "judge" / "judge.sqlite")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["status", "--runs-dir", str(runs), "--config", str(REPO / "configs" / "experiment.yaml")])
        # seeds file may be absent in this worktree: either way the temp runs dir was found (no 'not found' exit)
        self.assertIn(rc, (0, 2))
        with self.assertRaises(SystemExit) as cm:
            main(["status", "--runs-dir", str(j.dir / "nowhere")])
        self.assertIn("judge database not found", str(cm.exception))
