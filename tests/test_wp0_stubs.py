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


class StubTests(unittest.TestCase):
    def test_names_exist(self) -> None:
        for mod, names in EXPECTED.items():
            m = importlib.import_module(mod)
            for n in names:
                with self.subTest(module=mod, name=n):
                    self.assertTrue(hasattr(m, n), f"{mod}.{n}")

    def test_unfinished_functions_raise_with_owner(self) -> None:
        for mod in ("wtth.prereg", "wtth.render", "wtth.secrets", "wtth.runner.stream", "wtth.cli", "wtth.audit"):
            m = importlib.import_module(mod)
            for name, fn in inspect.getmembers(m, inspect.isfunction):
                if fn.__module__ != mod or name.startswith("_"):
                    continue
                n_params = len([p for p in inspect.signature(fn).parameters.values()
                                if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
                kw = {p.name: None for p in inspect.signature(fn).parameters.values()
                      if p.default is p.empty and p.kind is p.KEYWORD_ONLY}
                if inspect.iscoroutinefunction(fn):
                    continue
                with self.subTest(fn=f"{mod}.{name}"):
                    try:
                        fn(*([None] * n_params), **kw)
                    except NotImplementedError as exc:
                        self.assertRegex(str(exc), r"^WP\d$")


if __name__ == "__main__":
    unittest.main()
