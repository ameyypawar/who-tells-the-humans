"""T4.2-T4.5: the real orchestrator, renderer, MCP server and runner against tests/fake_claude.py (no network)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.util import PYTHON, REPO, tmp_dir
from wtth import cli
from wtth.orchestrator import manifest
from wtth.orchestrator.lock import RunLock

ALL_CELLS = ["NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS", "B-NONE"]
R1_EMPTY = ("NONE-R", "NEG", "NO-RECEIPT", "FORGED")


def ready() -> str | None:
    """None when WP1-WP3 and WP5 are in; else a skip reason."""
    if not (REPO / "tests" / "fake_claude.py").exists():
        return "tests/fake_claude.py (WP5) not present"
    try:
        from wtth import runner  # noqa: F401
        from wtth.runner import stream
        stream.parse_stream(Path("/nonexistent"))
    except NotImplementedError:
        return "WP3 runner not landed"
    except Exception:
        pass
    try:
        from wtth import prereg
        from wtth.config import load_settings
        prereg.load_scenario_texts("main", load_settings())
    except NotImplementedError:
        return "WP1 not landed"
    except Exception:
        pass
    return None


def arms_override(cells: list[str], seed_start: int, count: int) -> str:
    return json.dumps([{"model": "haiku", "scenario": "main", "level": "L2",
                        "cells": [{"cell": c, "seed_start": seed_start, "count": count} for c in cells]}])


class Env:
    """Paths for one test run; every argument that points into tests/tmp."""

    def __init__(self, name: str, behavior: dict | None = None) -> None:
        self.d = tmp_dir(f"{name}_{os.getpid()}")  # per-process dir: other coders run the suite concurrently
        self.runs = self.d / "runs"
        self.beh = self.d / "behavior.json"
        self.beh.write_text(json.dumps(behavior or {}), encoding="utf-8")

    def args(self, phase: str, cells: list[str] | None = None, seed_start: int = 9000, count: int = 1,
             k: int = 12, extra: list[str] | None = None) -> list[str]:
        a = ["run", "--phase", phase, "--fake", str(self.beh), "--runs-dir", str(self.runs), "--k-init", str(k),
             "--set", f"paths.slots_root={self.d / 'slots'}", "--set", f"paths.cwd={self.d / 'cwd'}",
             "--set", "scheduler.ramp_interval_s=0.2", "--set", "scheduler.health_interval_s=5"]
        if cells:
            a += ["--set", f"phases.{phase}.arms={arms_override(cells, seed_start, count)}"]
        return a + (extra or [])

    @property
    def db(self) -> Path:
        return self.runs / "wtth.sqlite"

    def q(self, sql: str, *params):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        try:
            return c.execute(sql, params).fetchall()
        finally:
            c.close()


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


@unittest.skipIf(ready(), ready() or "")
class OneEpisodePerCell(unittest.TestCase):
    """T4.2."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = Env("wp4_t42")
        cls.code, cls.out = run_cli(cls.env.args("g0dry"))

    def test_all_done(self) -> None:
        self.assertEqual(0, self.code, self.out[-2000:])
        rows = self.env.q("SELECT cell, status, final_attempt, n_attempts FROM episodes")
        self.assertEqual(sorted(ALL_CELLS), sorted(r["cell"] for r in rows))
        self.assertEqual({"done"}, {r["status"] for r in rows})
        self.assertEqual({(1, 1)}, {(r["final_attempt"], r["n_attempts"]) for r in rows})

    def test_turn_counts_and_classes(self) -> None:
        rows = self.env.q("SELECT episode_id, turn_type, class, COUNT(*) n FROM turns GROUP BY 1,2,3")
        per: dict = {}
        for r in rows:
            per.setdefault(r["episode_id"], {})[(r["turn_type"], r["class"])] = r["n"]
        self.assertEqual(11, len(per))
        for eid, d in per.items():
            self.assertEqual({("witness", "ok"): 15, ("d1", "ok"): 3, ("d2", "ok"): 3}, d, eid)

    def test_mechanical_exposure_checks(self) -> None:
        n = self.env.q("SELECT COUNT(*) n, SUM(ok) s FROM exposure_checks")[0]
        self.assertEqual((165, 165), (n["n"], n["s"]))
        rows = self.env.q("SELECT e.cell, x.prompt_text, t.round FROM exposures x JOIN turns t USING(turn_id) "
                          "JOIN episodes e USING(episode_id) WHERE t.turn_type='witness' AND t.round=2")
        self.assertEqual(33, len(rows))
        for r in rows:
            text, cell = r["prompt_text"], r["cell"]
            if cell in ("RECEIPT", "POS"):
                self.assertTrue("E-0200 | 21:24 | rd-11 |" in text, cell)
            elif cell in R1_EMPTY:
                self.assertTrue("#escalation-log (as of 21:30)\nNo entries." in text, cell)
            else:
                self.assertFalse("#escalation-log" in text, cell)

    def test_scripted_note_only_in_receipt_and_pos(self) -> None:
        rows = self.env.q("SELECT e.cell, COUNT(*) n FROM notes n JOIN episodes e USING(episode_id) "
                          "WHERE n.source='script' GROUP BY 1")
        self.assertEqual({"RECEIPT": 1, "POS": 1}, {r["cell"]: r["n"] for r in rows})
        logs = self.env.q("SELECT COUNT(*) n FROM escalation_log")[0]["n"]
        self.assertEqual(2, logs)

    def test_d2_prompt_contains_d1_answer(self) -> None:
        rows = self.env.q(
            "SELECT x.prompt_text, a.answer_text FROM exposures x JOIN turns t USING(turn_id) "
            "JOIN debrief_answers a ON a.episode_id=t.episode_id AND a.attempt=t.attempt AND a.slot=t.slot AND a.question='D1' "
            "WHERE t.turn_type='d2'")
        self.assertEqual(33, len(rows))
        for r in rows:
            self.assertTrue(r["answer_text"] in r["prompt_text"])
            self.assertTrue("Earlier question: " in r["prompt_text"] and "\nYour answer: " in r["prompt_text"])

    def test_no_fault_files_and_cost_recorded(self) -> None:
        faults = list((self.env.runs / "faults").glob("*"))
        self.assertEqual([], faults)
        self.assertGreater(self.env.q("SELECT SUM(cost_usd) s FROM cost_ledger")[0]["s"], 0)
        self.assertEqual(0, self.env.q("SELECT COUNT(*) n FROM cost_ledger WHERE estimated=1")[0]["n"])
        self.assertTrue(any((self.env.runs / "backups").glob("wtth-*.sqlite")))  # backup at clean exit
        self.assertEqual(1, self.env.q("SELECT COUNT(*) n FROM runs")[0]["n"])

    def test_status_command(self) -> None:
        code, out = run_cli(["status", "--runs-dir", str(self.env.runs)])
        self.assertEqual(0, code)
        self.assertIn("done", out)
        self.assertIn("cost:", out)

    def test_never_touches_production(self) -> None:
        self.assertFalse((REPO / "runs" / "wtth.sqlite").exists())


@unittest.skipIf(ready(), ready() or "")
class DryRun33(unittest.TestCase):
    """T4.5: seeds 1-3 x 11 cells."""

    def test_dry_run(self) -> None:
        env = Env("wp4_t45")
        code, out = run_cli(env.args("g0dry", ALL_CELLS, seed_start=1, count=3, k=12))
        self.assertEqual(0, code, out[-2000:])
        self.assertEqual({"done": 33}, {r["status"]: r["n"] for r in env.q("SELECT status, COUNT(*) n FROM episodes GROUP BY 1")})
        self.assertEqual(33 * 21, env.q("SELECT COUNT(*) n FROM turns WHERE class='ok'")[0]["n"])
        self.assertEqual(33 * 15, env.q("SELECT COUNT(*) n FROM exposure_checks WHERE ok=1")[0]["n"])
        code, out = run_cli(["status", "--runs-dir", str(env.runs)])
        self.assertIn("done", out)
        self.assertIn("33", out)


@unittest.skipIf(ready(), ready() or "")
class GuardTests(unittest.TestCase):
    """T4.4."""

    def settings(self, name: str, **ov):
        from wtth.config import load_settings
        d = tmp_dir(name)
        base = {"fake": True, "paths.runs_dir": str(d / "runs"), "paths.slots_root": str(d / "slots"),
                "paths.cwd": str(d / "cwd")}
        base.update(ov)
        return load_settings(overrides=base), d

    def test_clean_fake_startup(self) -> None:
        s, _ = self.settings("wp4_t44_clean")
        self.assertEqual([], manifest.verify_startup(s, "fake", "core"))

    def test_hash_drift_in_copied_prereg_dir(self) -> None:
        import shutil
        d = tmp_dir("wp4_t44_drift")
        shutil.copytree(REPO / "prereg" / "prompts", d / "prereg" / "prompts")
        shutil.copy(REPO / "prereg" / "PREREG.md", d / "prereg" / "PREREG.md")
        shutil.copy(REPO / "prereg" / "duty_word_check.md", d / "prereg" / "duty_word_check.md")
        shutil.copy(REPO / "prereg" / "references.bib", d / "prereg" / "references.bib")
        with open(d / "prereg" / "prompts" / "tools.md", "a") as fh:
            fh.write("\ndrift\n")
        s, _ = self.settings("wp4_t44_drift_s", **{"paths.prereg_dir": str(d / "prereg")})
        problems = manifest.verify_startup(s, "fake", "core")
        self.assertIn("hash mismatch: prompts/tools.md", problems)
        # and the CLI refuses to run
        env = Env("wp4_t44_drift_run")
        code, out = run_cli(env.args("g0dry", extra=["--set", f"paths.prereg_dir={d / 'prereg'}"]))
        self.assertEqual(2, code)
        self.assertIn("hash mismatch: prompts/tools.md", out)

    def test_private_dir_mismatch_is_reported(self) -> None:
        empty = tmp_dir("wp4_t44_priv")
        s, _ = self.settings("wp4_t44_priv_s", **{"paths.private_dir": str(empty)})
        problems = manifest.verify_startup(s, "fake", "core")
        self.assertTrue(any("claim_paraphrases" in p for p in problems))

    def test_binary_sha_mismatch_live(self) -> None:
        d = tmp_dir("wp4_t44_bin")
        fake_bin = d / "claude"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        s, _ = self.settings("wp4_t44_bin_s", **{"fake": False, "paths.binary": str(fake_bin)})
        problems = manifest.verify_startup(s, "live", "g0")
        self.assertIn("binary sha256 does not match the pinned value", problems)

    def test_live_env_guards(self) -> None:
        s, _ = self.settings("wp4_t44_env", fake=False)
        saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDECODE")}
        os.environ["ANTHROPIC_API_KEY"] = "x"
        os.environ["CLAUDECODE"] = "1"
        try:
            problems = manifest.verify_startup(s, "live", "g0")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertTrue(any(p.startswith("ANTHROPIC_API_KEY is set") for p in problems))
        self.assertTrue(any(p.startswith("CLAUDECODE is set") for p in problems))

    def test_core_requires_freeze(self) -> None:
        s, _ = self.settings("wp4_t44_freeze", fake=False)
        problems = manifest.verify_startup(s, "live", "core")
        self.assertIn("HEAD does not carry tag frozen-v1", problems)
        self.assertIn("frozen_level is not set", problems)
        self.assertIn("max_turns.frozen is false", problems)

    def test_lock_held_exits_5(self) -> None:
        env = Env("wp4_t44_lock")
        env.runs.mkdir(parents=True)
        with RunLock(env.runs / "wtth.lock", "other"):
            code, out = run_cli(env.args("g0dry"))
        self.assertEqual(5, code)
        self.assertFalse(env.db.exists())

    def test_fake_refuses_production_paths(self) -> None:
        env = Env("wp4_t44_prod")
        argv = ["run", "--phase", "g0dry", "--fake", str(env.beh), "--runs-dir", str(REPO / "runs"),
                "--set", f"paths.slots_root={env.d / 'slots'}", "--set", f"paths.cwd={env.d / 'cwd'}"]
        code, out = run_cli(argv)
        self.assertEqual(2, code)
        self.assertIn("production database", out)
        self.assertFalse((REPO / "runs" / "wtth.sqlite").exists())

    def test_live_needs_flag(self) -> None:
        code = cli.main(["run", "--phase", "g0", "--runs-dir", str(tmp_dir("wp4_t44_noflag"))])
        self.assertEqual(2, code)


KILL_BEHAVIOR = {
    "default": {"sleep_s": 0.15},
    "rules": [{"match": {"world": "R1", "round": 2, "slot": 1, "attempt": 1},
               "witness": {"calls": [{"tool": "submit_reviews", "args": "auto"},
                                     {"tool": "notify_ops", "args": {"subject": "First note", "message": "attempt one only"}}]}}],
}


@unittest.skipIf(ready(), ready() or "")
class KillResume(unittest.TestCase):
    """T4.3."""

    def test_kill_and_resume(self) -> None:
        env = Env("wp4_t43", KILL_BEHAVIOR)
        cells = ["NONE-R", "RECEIPT", "FORGED", "CLAIM"]
        argv = env.args("g0dry", cells, seed_start=1, count=5, k=12)
        cmd = [PYTHON, "-I", "-m", "wtth", *argv]
        log = open(env.d / "orch1.log", "wb")
        self.addCleanup(log.close)
        p = subprocess.Popen(cmd, cwd="/", stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 120
        while time.time() < deadline:
            if env.db.exists():
                try:
                    if env.q("SELECT COUNT(*) n FROM turns")[0]["n"] >= 40:
                        break
                except sqlite3.Error:
                    pass
            if p.poll() is not None:
                self.fail("orchestrator exited early: " + (env.d / "orch1.log").read_text()[-1500:])
            time.sleep(0.1)
        else:
            p.kill()
            self.fail("no progress")
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        tables = [r["name"] for r in env.q("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {t: env.q(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for t in tables}
        self.assertGreaterEqual(env.q("SELECT COUNT(*) n FROM attempts WHERE status='running'")[0]["n"], 1)  # killed mid-run
        code, out = run_cli(argv)
        self.assertEqual(0, code, out[-2000:])
        after = {t: env.q(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for t in tables}
        for t in tables:  # nothing is ever deleted
            self.assertGreaterEqual(after[t], before[t], t)
        eps = env.q("SELECT episode_id, status, final_attempt FROM episodes")
        self.assertEqual(20, len(eps))
        self.assertEqual({"done"}, {e["status"] for e in eps})
        done = env.q("SELECT episode_id, COUNT(*) n FROM attempts WHERE status='done' GROUP BY 1")
        self.assertEqual(20, len(done))
        self.assertEqual({1}, {d["n"] for d in done})
        self.assertGreaterEqual(env.q("SELECT COUNT(*) n FROM attempts WHERE status='abandoned'")[0]["n"], 1)
        self.assertEqual(0, env.q("SELECT COUNT(*) n FROM attempts WHERE status='running'")[0]["n"])
        # no exposure of attempt N references a row of another attempt
        bad = env.q(
            "SELECT COUNT(*) n FROM exposures x JOIN turns t USING(turn_id), json_each(x.visible_post_ids_json) j "
            "WHERE NOT EXISTS (SELECT 1 FROM posts p WHERE p.id=j.value AND p.episode_id=t.episode_id AND p.attempt=t.attempt)")[0]["n"]
        self.assertEqual(0, bad)
        bad = env.q(
            "SELECT COUNT(*) n FROM exposures x JOIN turns t USING(turn_id), json_each(x.visible_log_ids_json) j "
            "WHERE NOT EXISTS (SELECT 1 FROM escalation_log l WHERE l.id=j.value AND l.episode_id=t.episode_id AND l.attempt=t.attempt)")[0]["n"]
        self.assertEqual(0, bad)
        # no stale receipt: attempt >= 2 renders at round 3+ for NONE-R / FORGED show an empty log
        rows = env.q("SELECT e.cell, t.attempt, t.round, x.prompt_text FROM exposures x JOIN turns t USING(turn_id) "
                     "JOIN episodes e USING(episode_id) WHERE t.turn_type='witness' AND t.attempt>=2 AND t.round>=3")
        self.assertTrue(rows, "no attempt-2 renders: the kill landed too late; raise the sleep")
        for r in rows:
            if r["cell"] in ("NONE-R", "FORGED"):
                self.assertTrue("No entries." in r["prompt_text"], r["cell"])
            if r["cell"] == "RECEIPT":
                self.assertTrue("E-0200 | 21:24 | rd-11 |" in r["prompt_text"])
                self.assertFalse("E-0201" in r["prompt_text"])
        # a third run adds nothing
        mid = {t: env.q(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for t in tables}
        code, out = run_cli(argv)
        self.assertEqual(0, code)
        end = {t: env.q(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for t in tables}
        self.assertEqual(mid, end)
        # no leftover fake or server processes for this runs root
        ps = subprocess.run(["/bin/ps", "-axo", "command"], capture_output=True, text=True).stdout
        mine = [ln for ln in ps.splitlines() if str(env.runs) in ln and ("fake_claude" in ln or "wtth.mcp_server" in ln)]
        self.assertEqual([], mine)
        self.assertEqual([], list((env.runs / "faults").glob("*")))


if __name__ == "__main__":
    unittest.main()
