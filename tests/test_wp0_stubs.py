"""WP0 stubs: every research/41 section 2.9 name exists and unfinished bodies say which WP owns them."""
from __future__ import annotations

import importlib
import inspect
import unittest

EXPECTED = {
    "wtth.prereg": ["Block", "ScenarioTexts", "fenced_blocks", "line_block", "parse_section23", "sha256_file",
                    "verify_hashes", "load_scenario_texts", "load_private", "mask_private"],
    "wtth.stimuli": ["HANDLE_ORDER", "TICKET_ORDER", "digest_clock", "witness_stamp", "clock_min",
                     "witness_visible_from", "scripted_visible_from", "episode_spec", "scripted_posts",
                     "scripted_note", "tickets_for", "ticket_ids", "inject_round", "load_state"],
    "wtth.render": ["system_prompt", "round_prompt", "debrief_prompt", "number_log", "board_lines", "log_block",
                    "record_lines"],
    "wtth.handlers": ["FaultSink", "dispatch", "submit_reviews", "post_to_board", "notify_ops"],
    "wtth.mcp_server": ["assert_no_secret_env", "identity_from_env", "WorkspaceServer", "main"],
    "wtth.secrets": ["KeyFileError", "load_api_key"],
    "wtth.runner.invocation": ["build_argv", "build_env", "build_mcp_config", "redact_argv"],
    "wtth.runner.proc": ["run_process", "kill_group", "orphan_sweep", "harvest_mcp_log", "sample_rss_kb",
                         "memory_pressure_level"],
    "wtth.runner.stream": ["parse_stream"],
    "wtth.runner.validate": ["validate_init", "validate_result", "check_replies", "detect_refusal", "scan_logs",
                             "reconcile"],
    "wtth.runner.classify": ["classify"],
    "wtth.runner.turn": ["run_turn"],
    "wtth.orchestrator.context": ["RunContext", "SlotPool", "KGate", "LaunchGate", "CostTracker", "Breaker"],
    "wtth.orchestrator.episode": ["run_attempt"],
    "wtth.orchestrator.scheduler": ["build_queue", "enqueue", "resume_recover", "Scheduler"],
    "wtth.orchestrator.manifest": ["verify_startup", "build_manifest"],
    "wtth.orchestrator.lock": ["RunLock", "LockHeld"],
    "wtth.orchestrator.alerts": ["page"],
    "wtth.cli": ["main"],
    "wtth.audit": ["audit_g0", "AuditReport"],
    "wtth.gates": ["keyword_match", "g1_report", "g2_report"],
    "wtth.redact": ["build_release"],
    "wtth.config": ["load_settings", "load_toolsets", "load_expected_init", "load_prereg_hashes", "env_allowlist"],
    "wtth.db": ["WriteFailed", "Db", "create_db", "check_schema", "backup"],
    "wtth.textnorm": ["collapse_breaks", "cut", "one_line", "normalize_post", "normalize_note", "fill",
                      "sha256_text", "canon_json"],
}


PARAMS = {
    "wtth.config": {"load_settings": ("path", "overrides"), "load_toolsets": ("path",), "load_expected_init": ("path",),
                    "load_prereg_hashes": ("path",), "env_allowlist": ("path",)},
    "wtth.textnorm": {"collapse_breaks": ("s",), "cut": ("s", "n"), "one_line": ("s",), "fill": ("template", "values"),
                      "sha256_text": ("s",), "canon_json": ("obj",)},
    "wtth.stimuli": {"episode_spec": ("phase", "scenario", "model", "cell", "level", "seed", "queue_pos", "prefix"),
                     "inject_round": ("db", "spec", "episode_id", "attempt", "round", "texts", "run_id"),
                     "load_state": ("conn", "spec", "episode_id", "attempt")},
    "wtth.render": {"round_prompt": ("state", "texts", "round", "slot"),
                    "debrief_prompt": ("state", "texts", "slot", "which", "d1_answer")},
    "wtth.runner.turn": {"run_turn": ("ctx", "req", "slot", "cancel")},
    "wtth.orchestrator.episode": {"run_attempt": ("ctx", "spec", "episode_id")},
    "wtth.orchestrator.scheduler": {"build_queue": ("settings", "phase", "level"), "enqueue": ("db", "specs"),
                                    "resume_recover": ("ctx",)},
    "wtth.orchestrator.manifest": {"verify_startup": ("settings", "mode", "phase"),
                                   "build_manifest": ("settings", "run_id", "phase", "mode")},
    "wtth.orchestrator.alerts": {"page": ("settings", "db", "run_id", "phase", "reason", "detail", "exit_code")},
    "wtth.cli": {"main": ("argv",)},
}


class StubTests(unittest.TestCase):
    def test_names_exist(self) -> None:
        for mod, names in EXPECTED.items():
            m = importlib.import_module(mod)
            for n in names:
                with self.subTest(module=mod, name=n):
                    self.assertTrue(hasattr(m, n), f"{mod}.{n}")

    def test_frozen_signatures(self) -> None:
        for mod, funcs in PARAMS.items():
            m = importlib.import_module(mod)
            for name, params in funcs.items():
                with self.subTest(fn=f"{mod}.{name}"):
                    got = tuple(inspect.signature(getattr(m, name)).parameters)
                    self.assertEqual(params, got)

    def test_stub_or_implemented(self) -> None:
        """Each function is implemented, or its body is a NotImplementedError naming the owning WP."""
        for mod in EXPECTED:
            m = importlib.import_module(mod)
            for name, fn in inspect.getmembers(m, inspect.isfunction):
                if fn.__module__ != mod or name.startswith("_"):
                    continue
                src = inspect.getsource(fn)
                if "raise NotImplementedError" in src:
                    with self.subTest(fn=f"{mod}.{name}"):
                        self.assertRegex(src, r"raise NotImplementedError\(\"WP\d\"\)")


if __name__ == "__main__":
    unittest.main()
