"""T3.2: argv, env, mcp-config, redaction."""
from __future__ import annotations

import dataclasses
import json
import os
import unittest
from pathlib import Path

from tests.wp3_common import (DUMMY_KEY, SETTINGS, TOOLSETS, make_identity, make_req, make_slot, tmp_dir)
from wtth.config import env_allowlist, env_dynamic_names
from wtth.runner import invocation as inv
from wtth.textnorm import sha256_text
from wtth.types import TurnType

from wtth.config import tool_prefix  # noqa: E402

BIN = str(SETTINGS.paths.binary)
PFX = tool_prefix(SETTINGS)


class ArgvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir("wp3_inv")

    def witness(self, **kw):
        req = make_req(self.d, **kw)
        mcp = inv.build_mcp_config(SETTINGS, req.identity, req.toolset)
        return req, mcp, inv.build_argv(SETTINGS, req, mcp)

    def test_witness_argv_exact(self):
        req, mcp, argv = self.witness()
        names = ",".join(PFX + t for t in ("submit_reviews", "post_to_board", "notify_ops"))
        want = [BIN, "-p", req.prompt.text, "--bare", "--model", "claude-haiku-5-5", "--effort", "low",
                "--max-turns", "8", "--max-budget-usd", "0.02", "--system-prompt-file", str(req.system_path),
                "--tools", "", "--disable-slash-commands", "--setting-sources", "",
                "--settings", str(SETTINGS.paths.cc_settings), "--strict-mcp-config", "--mcp-config", mcp,
                "--allowedTools", names, "--permission-mode", "dontAsk", "--no-session-persistence",
                "--output-format", "stream-json", "--verbose"]
        self.assertEqual(argv, want)

    def test_sonnet_budget_formatting(self):
        _, _, argv = self.witness(model="claude-sonnet-5-5")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.10")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")

    def test_empty_strings_are_own_elements(self):
        _, _, argv = self.witness()
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv.count(""), 2)

    def test_board_only_has_two_names(self):
        _, _, argv = self.witness(toolset="board_only")
        self.assertEqual(argv[argv.index("--allowedTools") + 1],
                         PFX + "submit_reviews," + PFX + "post_to_board")

    def test_debrief_argv_exact(self):
        for tt in (TurnType.D1, TurnType.D2):
            req = make_req(self.d, turn_type=tt)
            argv = inv.build_argv(SETTINGS, req, None)
            want = [BIN, "-p", req.prompt.text, "--bare", "--model", "claude-haiku-5-5", "--effort", "low",
                    "--max-turns", "2", "--max-budget-usd", "0.02", "--system-prompt-file", str(req.system_path),
                    "--tools", "", "--disable-slash-commands", "--setting-sources", "",
                    "--settings", str(SETTINGS.paths.cc_settings), "--strict-mcp-config",
                    "--permission-mode", "dontAsk", "--no-session-persistence",
                    "--output-format", "stream-json", "--verbose"]
            self.assertEqual(argv, want)
            self.assertNotIn("--mcp-config", argv)
            self.assertNotIn("--allowedTools", argv)

    def test_witness_requires_mcp_config(self):
        req = make_req(self.d)
        with self.assertRaises(ValueError):
            inv.build_argv(SETTINGS, req, None)

    def test_allowed_tools_fallback_separate_elements(self):
        s2 = dataclasses.replace(SETTINGS, allowed_tools_separate=True)
        req = make_req(self.d)
        argv = inv.build_argv(s2, req, inv.build_mcp_config(s2, req.identity, req.toolset))
        i = argv.index("--allowedTools")
        self.assertEqual(argv[i + 1:i + 4], [PFX + "submit_reviews", PFX + "post_to_board",
                                             PFX + "notify_ops"])
        self.assertEqual(argv[i + 4], "--permission-mode")

    def test_no_key_in_argv(self):
        _, _, argv = self.witness()
        self.assertFalse(any(DUMMY_KEY in a or "ANTHROPIC" in a for a in argv))


class McpConfigTest(unittest.TestCase):
    def test_roundtrip_and_shape(self):
        ident = make_identity()
        s = inv.build_mcp_config(SETTINGS, ident, TOOLSETS["main"])
        self.assertEqual(s, json.dumps(json.loads(s), separators=(",", ":"), sort_keys=True))
        srv = json.loads(s)["mcpServers"][SETTINGS.mcp_server_name]
        self.assertEqual(srv["type"], "stdio")
        self.assertEqual(srv["command"], str(SETTINGS.paths.python))
        self.assertEqual(srv["args"], ["-I", "-m", "wtth.mcp_server", "--runs-root", str(SETTINGS.paths.runs_dir)])
        self.assertIs(srv["alwaysLoad"], True)
        env = srv["env"]
        want = {"WTTH_DB", "WTTH_RUN_ID", "WTTH_EPISODE_ID", "WTTH_ATTEMPT", "WTTH_ROUND", "WTTH_SLOT",
                "WTTH_SENDER", "WTTH_CLOCK", "WTTH_VISIBLE_FROM", "WTTH_WORLD", "WTTH_TURN_ID", "WTTH_TOOLSET",
                "WTTH_TOOLS_PATH", "WTTH_TOOLS_SHA256", "WTTH_TICKET_IDS", "WTTH_FAULT_DIR", "WTTH_SCHEMA_VERSION"}
        self.assertEqual(set(env), want)
        for k, v in env.items():
            self.assertIsInstance(v, str)
            self.assertFalse(k.endswith(("_KEY", "_TOKEN", "_SECRET")), k)
            self.assertNotIn("ANTHROPIC", k)
        self.assertEqual(env["WTTH_DB"], str(SETTINGS.paths.db_path))
        self.assertEqual(os.path.dirname(env["WTTH_DB"]), srv["args"][-1])
        self.assertEqual(env["WTTH_TURN_ID"], "a" * 32)
        self.assertEqual(env["WTTH_TOOLS_SHA256"], TOOLSETS["main"].sha256)
        self.assertEqual(env["WTTH_TICKET_IDS"], "T-1201,T-1202,T-1203")
        self.assertEqual(env["WTTH_WORLD"], "R1")
        self.assertEqual(env["WTTH_VISIBLE_FROM"], "3")
        self.assertEqual(env["WTTH_SCHEMA_VERSION"], "1")
        self.assertEqual(env["WTTH_FAULT_DIR"], str(SETTINGS.paths.faults_dir))

    def test_requires_turn_id(self):
        ident = dataclasses.replace(make_identity(), turn_id=None)
        with self.assertRaises(ValueError):
            inv.build_mcp_config(SETTINGS, ident, TOOLSETS["main"])


class EnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir("wp3_env")
        self.slot = make_slot(self.d)
        self.path = SETTINGS.paths.repo_root / "configs" / "env_allowlist.json"
        self.base, self.audit = env_allowlist(self.path)

    def test_keys_equal_allowlist_plus_dynamic(self):
        before = dict(os.environ)
        env = inv.build_env(SETTINGS, self.slot, DUMMY_KEY, None)
        self.assertEqual(before, dict(os.environ))
        self.assertEqual(set(env), set(self.base) | set(env_dynamic_names(self.path)))
        self.assertEqual(env["ANTHROPIC_API_KEY"], DUMMY_KEY)
        self.assertEqual(env["HOME"], str(self.slot.home))
        self.assertEqual(env["TMPDIR"], str(self.slot.tmp))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.slot.config))
        for k, v in self.base.items():
            self.assertEqual(env[k], v)
        self.assertTrue(all(isinstance(v, str) for v in env.values()))

    def test_audit_set(self):
        bodies = self.d / "bodies"
        env = inv.build_env(SETTINGS, self.slot, DUMMY_KEY, bodies)
        self.assertEqual(set(env), set(self.base) | set(self.audit) | set(env_dynamic_names(self.path)))
        self.assertEqual(env["OTEL_LOG_RAW_API_BODIES"], f"file:{bodies}")
        self.assertEqual(env["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")

    def test_ignores_process_environ(self):
        os.environ["WTTH_TEST_LEAK"] = "1"
        try:
            env = inv.build_env(SETTINGS, self.slot, DUMMY_KEY, None)
        finally:
            del os.environ["WTTH_TEST_LEAK"]
        self.assertNotIn("WTTH_TEST_LEAK", env)

    def test_key_only_under_its_name(self):
        env = inv.build_env(SETTINGS, self.slot, DUMMY_KEY, None)
        self.assertEqual([k for k, v in env.items() if DUMMY_KEY in v], ["ANTHROPIC_API_KEY"])


class RedactTest(unittest.TestCase):
    def test_prompt_replaced(self):
        d = tmp_dir("wp3_red")
        req = make_req(d, prompt="SECRET-ISH PROMPT")
        argv = inv.build_argv(SETTINGS, req, inv.build_mcp_config(SETTINGS, req.identity, req.toolset))
        red = inv.redact_argv(argv)
        self.assertEqual(len(red), len(argv))
        self.assertNotIn("SECRET-ISH PROMPT", red)
        self.assertEqual(red[argv.index("-p") + 1], f"<prompt sha256={sha256_text('SECRET-ISH PROMPT')}>")
        diff = [i for i, (a, b) in enumerate(zip(argv, red)) if a != b]
        self.assertEqual(diff, [argv.index("-p") + 1])
        self.assertIn("--mcp-config", red)
        self.assertIsNot(red, argv)


if __name__ == "__main__":
    unittest.main()
