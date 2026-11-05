"""WP2 T2.2: the real stdio server through mcp.client.stdio + ClientSession."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import unittest
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.test_wp2_common import TOOLS_YAML, make_world, server_env
from tests.util import DUMMY_KEY, PYTHON, REPO, tmp_dir

REPLIES = {"submit_reviews": "Reviews recorded.", "post_to_board": "Posted to #ops-board.",
           "notify_ops": "Sent to ops."}
YAML = yaml.safe_load(TOOLS_YAML.read_text(encoding="utf-8"))["toolsets"]


def argv(runs_root: Path) -> list[str]:
    return ["-I", "-m", "wtth.mcp_server", "--runs-root", str(runs_root)]


def run_server_raw(env: dict[str, str], runs_root: Path, extra_env: dict[str, str] | None = None):
    full = {"PATH": "/usr/bin:/bin", "HOME": str(runs_root), **env, **(extra_env or {})}
    return subprocess.run([PYTHON, *argv(runs_root)], env=full, stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=60)


async def with_session(env: dict[str, str], runs_root: Path, fn):
    params = StdioServerParameters(command=PYTHON, args=argv(runs_root), env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            return await fn(s, init)


class StdioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.db, self.ep = make_world(self._testMethodName, "R1")
        self.runs = self.path.parent
        self.faults = self.runs / "faults"
        self.faults.mkdir()

    def tearDown(self) -> None:
        self.db.close()

    def q(self, sql, *a):
        return self.db.read(lambda c: [dict(r) for r in c.execute(sql, a)])

    def test_list_tools_all_toolsets(self):
        for ts in ("main", "backup", "board_only"):
            env = server_env(self.path, self.faults, self.ep, world="R1", toolset=ts, turn_id=("%032x" % (len(ts))))
            # one server per toolset: fresh attempt row is shared, server_starts rows accumulate
            async def fn(s, init):
                self.assertEqual(init.serverInfo.name, "workspace")
                self.assertIsNone(init.instructions)
                return await s.list_tools()
            res = asyncio.run(with_session(env, self.runs, fn))
            wire = [t.model_dump(mode="json", exclude_none=True) for t in res.tools]
            want = YAML[ts]["tools"]
            self.assertEqual([w["name"] for w in wire], [t["name"] for t in want])
            for w, t in zip(wire, want):
                self.assertEqual(set(w), {"name", "description", "inputSchema"})
                self.assertEqual(w["description"], t["description"])
                self.assertEqual(json.dumps(w["inputSchema"]), json.dumps(t["input_schema"]))

    def test_call_shapes_and_db(self):
        env = server_env(self.path, self.faults, self.ep, world="R1")
        calls = [
            ("notify_ops", {"subject": "S", "message": "M"}),
            ("notify_ops", {}),
            ("notify_ops", {"subject": 123}),
            ("notify_ops", {"subject": "x", "message": "y", "sender": "rd-99", "bogus": [1]}),
            ("post_to_board", {"message": "hello"}),
            ("post_to_board", {"message": ""}),
            ("submit_reviews", {"reviews": [{"ticket_id": "T-1201", "decision": "approve", "comment": "c"}]}),
            ("submit_reviews", {"reviews": "garbage"}),
            ("submit_reviews", {}),
            ("not_a_tool", {"a": 1}),
        ]

        async def fn(s, init):
            out = []
            for name, args in calls:
                out.append(await s.call_tool(name, args))
            return out
        res = asyncio.run(with_session(env, self.runs, fn))
        for (name, _), r in zip(calls, res):
            want = REPLIES.get(name, "Reviews recorded.")  # unknown tool: first tool's reply
            self.assertEqual([c.model_dump(mode="json", exclude_none=True) for c in r.content],
                             [{"type": "text", "text": want}], name)
            self.assertIsNone(r.structuredContent)
            self.assertFalse(r.isError)
        self.assertEqual(len(self.q("SELECT * FROM tool_calls")), 10)
        self.assertEqual(len(self.q("SELECT * FROM notes")), 2)
        self.assertEqual(len(self.q("SELECT * FROM escalation_log")), 2)
        self.assertEqual(len(self.q("SELECT * FROM posts")), 1)
        self.assertEqual(len(self.q("SELECT * FROM reviews")), 1)
        self.assertEqual({q["sender"] for q in self.q("SELECT sender FROM notes")}, {"rd-14"})
        # the unknown tool is a technical fault (fault file), nothing else is
        files = list(self.faults.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual([json.loads(x)["error"] for x in files[0].read_text().splitlines()], ["unknown_tool"])

    def test_wire_bytes_are_bare(self):
        """The raw JSON-RPC result has exactly one text block and no structuredContent / isError=true."""
        env = server_env(self.path, self.faults, self.ep)

        async def fn(s, init):
            r = await s.call_tool("notify_ops", {"subject": "a"})
            return r.model_dump(mode="json", by_alias=True, exclude_none=True)
        d = asyncio.run(with_session(env, self.runs, fn))
        self.assertEqual(d["content"], [{"type": "text", "text": "Sent to ops."}])
        self.assertNotIn("structuredContent", d)
        self.assertFalse(d.get("isError", False))

    def test_server_starts_row_names_only(self):
        env = server_env(self.path, self.faults, self.ep, turn_id="ab" * 16)

        async def fn(s, init):
            return None
        asyncio.run(with_session(env, self.runs, fn))
        rows = self.q("SELECT * FROM server_starts")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_id"], "ab" * 16)
        names = json.loads(rows[0]["env_names_json"])
        self.assertEqual(names, sorted(names))
        self.assertIn("WTTH_DB", names)
        blob = rows[0]["env_names_json"]
        for v in (str(self.path), "run-1", "rd-14"):
            self.assertNotIn(v, blob)

    def test_short_db_lock_is_survived_by_retry(self):
        env = server_env(self.path, self.faults, self.ep, turn_id="cd" * 16)
        import sqlite3
        import threading
        import time

        def hold():
            b = sqlite3.connect(str(self.path), isolation_level=None)
            b.execute("BEGIN IMMEDIATE")
            time.sleep(2.0)
            b.execute("ROLLBACK")
            b.close()

        async def fn(s, init):
            t = threading.Thread(target=hold)
            t.start()
            await asyncio.sleep(0.3)
            r = await s.call_tool("notify_ops", {"subject": "survives"})
            t.join()
            return r
        r = asyncio.run(with_session(env, self.runs, fn))
        self.assertEqual(r.content[0].text, "Sent to ops.")
        self.assertEqual(len(self.q("SELECT * FROM notes")), 1)
        self.assertEqual(list(self.faults.glob("*.jsonl")), [])


class StartupRefusal(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.db, self.ep = make_world(self._testMethodName)
        self.runs = self.path.parent
        self.faults = self.runs / "faults"
        self.faults.mkdir()
        self.env = server_env(self.path, self.faults, self.ep)

    def tearDown(self) -> None:
        self.db.close()

    def expect_exit2(self, env, extra=None):
        p = run_server_raw(env, self.runs, extra)
        self.assertEqual(p.returncode, 2, p.stderr.decode()[-400:])
        self.assertEqual(p.stdout, b"")  # nothing before the handshake
        self.assertNotIn(DUMMY_KEY.encode(), p.stderr)
        return p

    def test_baseline_valid_env_would_start(self):
        # stdin is closed: the server starts, sees EOF and exits cleanly (0), proving the env is accepted
        p = run_server_raw(self.env, self.runs)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-400:])

    def test_each_missing_var_exits_2(self):
        for name in self.env:
            env = {k: v for k, v in self.env.items() if k != name}
            self.expect_exit2(env)

    def test_invalid_values_exit_2(self):
        bad = {"WTTH_ROUND": "6", "WTTH_SLOT": "0", "WTTH_SENDER": "xx-14", "WTTH_CLOCK": "25:00",
               "WTTH_VISIBLE_FROM": "2", "WTTH_WORLD": "R2", "WTTH_TURN_ID": "short", "WTTH_TOOLSET": "nope",
               "WTTH_TICKET_IDS": "T-1,T-2", "WTTH_FAULT_DIR": str(self.runs / "nodir"),
               "WTTH_SCHEMA_VERSION": "2", "WTTH_EPISODE_ID": "x", "WTTH_ATTEMPT": "2",
               "WTTH_DB": str(self.runs / "other.sqlite"), "WTTH_TOOLS_SHA256": "0" * 64,
               "WTTH_TOOLS_PATH": str(self.runs / "none.yaml")}
        for k, v in bad.items():
            self.expect_exit2({**self.env, k: v})

    def test_db_outside_runs_root_exits_2(self):
        other = tmp_dir("wp2_other_root")
        p = run_server_raw(self.env, other)
        self.assertEqual(p.returncode, 2)

    def test_closed_attempt_exits_2(self):
        self.db.write(lambda c: c.execute("UPDATE attempts SET status='failed', finished_at=1"))
        self.expect_exit2(self.env)

    def test_secret_named_env_exits_2_with_fault_line(self):
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "FOO_TOKEN", "MY_API_KEY", "X_SECRET", "DB_PASSWORD"):
            for f in self.faults.glob("*"):
                f.unlink()
            p = self.expect_exit2(self.env, {name: DUMMY_KEY})
            self.assertIn(name.encode(), p.stderr)
            self.assertEqual(len(list(self.faults.glob("*.jsonl"))), 1)

    def run_scrub(self, extra):
        code = "import wtth.mcp_server as m; m._guard_mode = lambda: 'scrub'; m.main()"
        full = {"PATH": "/usr/bin:/bin", "HOME": str(self.runs), **self.env, **extra}
        return subprocess.run([PYTHON, "-I", "-c", code, "--runs-root", str(self.runs)], env=full,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60)

    def test_scrub_mode_deletes_names_and_continues(self):
        extra = {"ANTHROPIC_API_KEY": DUMMY_KEY, "FOO_TOKEN": "tokvalue123"}
        p = self.run_scrub(extra)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-400:])
        err = p.stderr.decode()
        self.assertIn("wtth.mcp_server: scrubbed credential-like env names: ANTHROPIC_API_KEY,FOO_TOKEN\n", err)
        self.assertNotIn(DUMMY_KEY, err)
        self.assertNotIn("tokvalue123", err)
        rows = self.db.read(lambda c: [dict(r) for r in c.execute("SELECT env_names_json FROM server_starts")])
        self.assertEqual(len(rows), 1)
        names = json.loads(rows[0]["env_names_json"])
        self.assertNotIn("ANTHROPIC_API_KEY", names)
        self.assertNotIn(DUMMY_KEY, rows[0]["env_names_json"])
        self.assertNotIn("tokvalue123", rows[0]["env_names_json"])

    def test_refuse_mode_is_default(self):
        from wtth.config import load_settings
        self.assertEqual(load_settings().mcp_env_guard, "refuse")

    def test_scrub_line_matches_log_allowlist(self):
        import re
        allow = json.loads((REPO / "configs" / "log_allowlists.json").read_text())
        pats = [re.compile(x) for x in allow["mcp_log"]["message_regexes"]]
        line = "wtth.mcp_server: scrubbed credential-like env names: ANTHROPIC_API_KEY,FOO_TOKEN"
        self.assertTrue(any(p.search(line) for p in pats))
        self.assertFalse(any(p.search("wtth.mcp_server: scrubbed credential-like env names: a b=c") for p in pats))

    def test_scrub_function_in_process(self):
        from wtth.mcp_server import scrub_secret_env
        env = {"A_KEY": "1", "B_SECRET": "2", "OK": "3", "ANTHROPIC_X": "4", "PASSWORD": "5"}
        self.assertEqual(scrub_secret_env(env), ["ANTHROPIC_X", "A_KEY", "B_SECRET", "PASSWORD"])
        self.assertEqual(env, {"OK": "3"})

    def test_schema_version_mismatch_in_db_exits_2(self):
        import sqlite3
        c = sqlite3.connect(str(self.path), isolation_level=None)
        c.execute("PRAGMA user_version=7")
        c.close()
        self.expect_exit2(self.env)


if __name__ == "__main__":
    unittest.main()
