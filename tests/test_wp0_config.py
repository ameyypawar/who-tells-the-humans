"""T0.4: configuration (cell table, N per cell, hashes) and the other config files."""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from wtth.config import (ConfigError, env_allowlist, env_dynamic_names, load_expected_init, load_log_allowlists,
                         load_prereg_hashes, load_prereg_pins, load_settings)
from wtth.types import World

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"

# research/41 section 2.2: (world, runbook, c2, c_note, v_lines)
CELL_TABLE = {
    "NONE":       ("R0", "V0",          "C2.R",         False, "F"),
    "CONCERN":    ("R0", "V0",          "C2.CN",        False, "F"),
    "CLAIM":      ("R0", "V0",          "CLAIM",        False, "F"),
    "POLICY":     ("R0", "V-POLICY",    "CLAIM",        False, "F"),
    "NONE-R":     ("R1", "V-R1",        "C2.R",         False, "F"),
    "RECEIPT":    ("R1", "V-R1",        "CLAIM",        True,  "F"),
    "NO-RECEIPT": ("R1", "V-R1",        "CLAIM",        False, "F"),
    "FORGED":     ("R1", "V-R1",        "CLAIM+FORGED", False, "F"),
    "NEG":        ("R1", "V-R1-DEDUPE", "C2.R",         False, "F"),
    "POS":        ("R1", "V-R1-DEDUPE", "CLAIM",        True,  "F"),
    "B-NONE":     ("R0", "V0",          "C2.R",         False, "BN"),
}
# D-003 (and research/41 section 2.2): Haiku core N, Sonnet N.
HAIKU_N = {"NONE": 80, "CONCERN": 50, "CLAIM": 80, "POLICY": 50, "NONE-R": 80, "RECEIPT": 50, "NO-RECEIPT": 80,
           "FORGED": 50, "NEG": 50, "POS": 50, "B-NONE": 50}
SONNET_N = {"NONE": 30, "CLAIM": 30, "NONE-R": 30, "NO-RECEIPT": 30, "FORGED": 30}
MVP_CELLS = ["NONE", "CLAIM", "POLICY", "NONE-R", "NO-RECEIPT", "FORGED", "NEG", "POS"]


def sha256_bytes_of(path: Path) -> str:
    # Digest only: file content (including the withheld private files) is never printed or returned.
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_section23(prereg_md: Path) -> dict[str, str]:
    text = prereg_md.read_text(encoding="utf-8")
    sec = text.split("## 23 File and hash register", 1)[1].split("\n## 24", 1)[0]
    out = {}
    for line in sec.splitlines():
        m = re.match(r"^\| ([^|]+?) \| [^|]* \| `([0-9a-f]{64})` \|$", line)
        if m:
            out[m.group(1).strip()] = m.group(2)
    return out


class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.s = load_settings()

    def test_cell_table(self) -> None:
        for name, (world, runbook, c2, c_note, v_lines) in CELL_TABLE.items():
            with self.subTest(cell=name):
                c = self.s.cells[name]
                self.assertEqual(World(world), c.world)
                self.assertEqual(runbook, c.runbook)
                self.assertEqual(c2, c.c2)
                self.assertEqual(c_note, c.c_note)
                self.assertEqual(v_lines, c.v_lines)
                self.assertEqual("SB-1", c.system)
                self.assertEqual("main", c.toolset)
                self.assertFalse(c.header_owner)
        self.assertEqual(tuple(CELL_TABLE), self.s.core_cells)

    def test_pivot_cells(self) -> None:
        for n in ("O1", "O2", "O3"):
            self.assertTrue(self.s.cells[n].header_owner)
        self.assertEqual(["slot1", "rd-04", "rd-11"], [self.s.cells[n].owner for n in ("O1", "O2", "O3")])
        self.assertEqual("CLAIM", self.s.cells["O3"].c2)
        for n in ("BA2", "BA4"):
            self.assertEqual("C2.OBJ", self.s.cells[n].c2)
        for n in ("BA3", "BA4"):
            self.assertEqual(("BO-1", "board_only", "BO-RB-BASE"),
                             (self.s.cells[n].system, self.s.cells[n].toolset, self.s.cells[n].runbook))

    def test_n_per_cell_d003(self) -> None:
        core = self.s.phases["core"].arms
        self.assertEqual(1, len(core))
        self.assertEqual(self.s.models.haiku, core[0].model)
        self.assertEqual(HAIKU_N, {c.cell: c.count for c in core[0].cells})
        for c in core[0].cells:
            self.assertEqual(1, c.seed_start)
        self.assertEqual(670, sum(c.count for c in core[0].cells))
        son = self.s.phases["sonnet"].arms
        self.assertEqual(self.s.models.sonnet, son[0].model)
        self.assertEqual(SONNET_N, {c.cell: c.count for c in son[0].cells})
        self.assertEqual(150, sum(c.count for c in son[0].cells))
        self.assertEqual(MVP_CELLS, [c.cell for c in self.s.phases["mvp"].arms[0].cells])
        self.assertTrue(all(c.count == 24 for c in self.s.phases["mvp"].arms[0].cells))

    def test_pilot_seed_blocks(self) -> None:
        g0 = self.s.phases["g0"]
        gate = [(c.cell, c.seed_start) for c in g0.arms[0].cells if not c.probe]
        self.assertEqual([("NONE", 9001), ("RECEIPT", 9002)], gate)  # B9
        probes = [(a.model_key, c.cell, c.seed_start) for a in g0.arms for c in a.cells if c.probe]
        self.assertEqual([("haiku", "FORGED", 9003), ("sonnet", "NONE", 9004)], probes)
        self.assertEqual((9000, "NONE"), (g0.deny_probe.seed, g0.deny_probe.cell))
        self.assertEqual(9011, self.s.phases["g1"].arms[0].cells[0].seed_start)
        self.assertEqual(16, self.s.phases["g1"].arms[0].cells[0].count)
        self.assertEqual([9051, 9051], [c.seed_start for c in self.s.phases["g2"].arms[0].cells])
        self.assertEqual([9071, 9071], [c.seed_start for c in self.s.phases["g2fix"].arms[0].cells])
        for name in ("g0", "g1", "g1b", "g2", "g2fix", "g0dry"):
            for arm in self.s.phases[name].arms:
                for c in arm.cells:
                    self.assertGreaterEqual(c.seed_start, 9000)  # pilot seeds are never confirmatory

    def test_phase_ordinals_unique(self) -> None:
        ords = [p.ordinal for p in self.s.phases.values()]
        self.assertEqual(len(ords), len(set(ords)))
        self.assertEqual({"g0dry", "g0", "g0b", "g1", "g1b", "g2", "g2fix", "core", "sonnet", "mvp", "pivotA", "pivotB",
                          "backup"}, set(self.s.phases))

    def test_binding_decisions(self) -> None:
        self.assertEqual(3, self.s.max_attempts)  # B2
        sc = self.s.scheduler
        self.assertEqual((9, 12, 15, 3, 3), (sc.k_init, sc.k_target, sc.k_max, sc.k_min, sc.episodes_per_k))  # B4
        self.assertEqual((3, 120, 3, 60), (sc.breaker_trip_after, sc.breaker_pause_s, sc.breaker_stop_trips,
                                          sc.breaker_window_min))  # B3
        self.assertEqual((3, (5.0, 20.0)), (sc.debrief_tries, sc.debrief_backoff_s))  # B1
        self.assertEqual((80.0, 60.0), (self.s.budget.cap_usd, self.s.budget.warn_usd))  # B5
        self.assertEqual(8, self.s.max_turns.witness)
        self.assertEqual(2, self.s.max_turns.debrief)
        self.assertFalse(self.s.max_turns.frozen)
        self.assertIsNone(self.s.frozen_level)
        self.assertEqual(20261105, self.s.permutation_seed)
        self.assertEqual(120, self.s.timeouts.turn_s)
        self.assertEqual({"claude-haiku-5-5": 0.02, "claude-sonnet-5-5": 0.10}, self.s.models.max_budget_usd)
        self.assertEqual(("claude-haiku-5-5", "claude-sonnet-5-5"), (self.s.models.haiku, self.s.models.sonnet))
        self.assertEqual(21, self.s.budget.reserve_calls)

    def test_scenarios_and_keywords(self) -> None:
        self.assertEqual("rd", self.s.scenarios["main"].handle_prefix)
        self.assertEqual("fl", self.s.scenarios["backup"].handle_prefix)
        self.assertEqual({"main": "backup"}, self.s.scenarios["backup"].toolset_map)
        text = (REPO / "prereg" / "PREREG.md").read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if "Keyword rule (mechanical" in ln)
        quoted = re.findall(r'"([^"]+)"', line.split("a note counts if", 1)[1].split("The backup", 1)[0])
        self.assertEqual(quoted, list(self.s.keywords("main")))
        btext = (REPO / "prereg" / "prompts" / "backup_scenario.md").read_text(encoding="utf-8")
        bline = btext.split("## 8 Keyword rule", 1)[1].split("\n", 2)[1]
        bq = re.findall(r'"([^"]+)"', bline.split("contains any of", 1)[1].split("Notes that match", 1)[0])
        self.assertEqual(bq, list(self.s.keywords("backup")))

    def test_runbook_variants(self) -> None:
        rb = self.s.runbooks
        self.assertEqual(["RB-BASE"], rb["V0"])
        self.assertEqual(rb["V0"] + ["RB-POLICY"], rb["V-POLICY"])
        self.assertEqual(rb["V0"] + ["RB-R1"], rb["V-R1"])
        self.assertEqual(rb["V-R1"] + ["RB-DEDUPE"], rb["V-R1-DEDUPE"])
        self.assertEqual(rb["V0"] + ["RB-OWNER"], rb["V-OWNER"])

    def test_paths(self) -> None:
        p = self.s.paths
        self.assertEqual(REPO, p.repo_root)
        self.assertEqual(REPO / "runs" / "wtth.sqlite", p.db_path)
        self.assertEqual(REPO / "runs" / "faults", p.faults_dir)
        self.assertEqual("/Users/amey/.local/wtth/bin/claude-2.1.317", str(p.binary))
        self.assertEqual("/private/tmp/ws/run", str(p.cwd))
        self.assertEqual(REPO / "configs" / "tools.yaml", p.tools_yaml)

    def test_overrides(self) -> None:
        s = load_settings(overrides={"paths.runs_dir": "/x/runs", "scheduler": {"k_init": 3}, "fake": True,
                                     "phases.core.arms": []})
        self.assertEqual(Path("/x/runs/wtth.sqlite"), s.paths.db_path)
        self.assertEqual(3, s.scheduler.k_init)
        self.assertEqual(12, s.scheduler.k_target)
        self.assertTrue(s.fake)
        self.assertEqual((), s.phases["core"].arms)

    def test_validation_errors(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(overrides={"cells.NONE.c2": "bogus"})
        with self.assertRaises(ConfigError):
            load_settings(overrides={"cuts.tier": "huge"})
        with self.assertRaises(ConfigError):
            load_settings(overrides={"cells.NONE.runbook": "V-NOPE"})


class HashTests(unittest.TestCase):
    def test_hashes_match_section23_and_disk(self) -> None:
        s = load_settings()
        recorded = load_prereg_hashes(CONFIGS / "prereg_hashes.json")
        parsed = parse_section23(s.paths.prereg_dir / "PREREG.md")
        self.assertEqual(12, len(parsed))
        without_prereg = {k: v for k, v in recorded.items() if k != "PREREG.md"}
        self.assertEqual(parsed, without_prereg)
        for rel, expected in recorded.items():
            if rel.startswith("private/"):
                path = s.paths.private_dir / rel.split("/", 1)[1]
            else:
                path = s.paths.prereg_dir / rel
            with self.subTest(file=rel):
                self.assertEqual(expected, sha256_bytes_of(path))  # digest compare only; content never shown

    def test_binary_pin(self) -> None:
        s = load_settings()
        pins = load_prereg_pins(CONFIGS / "prereg_hashes.json")
        on_disk = s.paths.binary_sha256_file.read_text().split()[0]
        self.assertEqual(on_disk, pins["binary_sha256"])


class SideFileTests(unittest.TestCase):
    def test_env_allowlist(self) -> None:
        base, audit = env_allowlist(CONFIGS / "env_allowlist.json")
        self.assertEqual("UTC", base["TZ"])
        self.assertEqual("/usr/bin:/bin", base["PATH"])
        self.assertEqual("false", base["ENABLE_TOOL_SEARCH"])
        self.assertEqual(16, len(base))
        self.assertEqual("1", base["CLAUDE_CODE_DISABLE_REFUSAL_RETRY"])
        self.assertEqual({"CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_LOGS_EXPORTER", "OTEL_METRICS_EXPORTER",
                          "OTEL_LOG_RAW_API_BODIES", "OTEL_LOGS_EXPORT_INTERVAL"}, set(audit))
        dynamic = env_dynamic_names(CONFIGS / "env_allowlist.json")
        self.assertEqual({"HOME", "TMPDIR", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"}, set(dynamic))
        self.assertFalse(set(dynamic) & set(base))
        for name in list(base) + list(audit) + list(dynamic):
            if name != "ANTHROPIC_API_KEY":
                self.assertFalse(re.search(r"_KEY$|_TOKEN$|_SECRET$|PASSWORD", name), name)

    def test_expected_init(self) -> None:
        exp = load_expected_init(CONFIGS / "expected_init.json")
        self.assertEqual("2.1.317", exp["claude_code_version"])
        self.assertEqual("default", exp["permissionMode"])
        self.assertEqual("ANTHROPIC_API_KEY", exp["apiKeySource"])
        self.assertEqual("/private/tmp/ws/run", exp["cwd"])
        self.assertEqual("workspace", exp["mcp_server_name"])  # default; settings.mcp_server_name overrides at run time
        self.assertEqual("mcp__workspace__", exp["tool_prefix"])
        self.assertIn("error_max_budget_usd", exp["result"]["known_subtypes"])

    def test_log_allowlists_compile(self) -> None:
        allow = load_log_allowlists(CONFIGS / "log_allowlists.json")
        for rx in allow["stderr"]:
            re.compile(rx)
        self.assertTrue(re.search(allow["stderr"][1], "⚠ Permission mode forced to default — "
                                  "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set (allowed_non_write_users hardening)."))
        m = re.search(allow["mcp_connect_ms"], "Successfully connected (transport: stdio) in 123ms")
        self.assertEqual("123", m.group(1))

    def test_json_files_parse(self) -> None:
        for name in ("expected_init", "env_allowlist", "log_allowlists", "prereg_hashes"):
            json.loads((CONFIGS / f"{name}.json").read_text(encoding="utf-8"))

    def test_no_literal_key_prefix_in_repo_files(self) -> None:
        prefix = "sk-" + "ant"
        for p in list((REPO / "src").rglob("*.py")) + list((REPO / "tests").glob("*.py")) + list(CONFIGS.glob("*")) \
                + list((REPO / "scripts").glob("*")):
            self.assertNotIn(prefix, p.read_text(encoding="utf-8"), str(p))


if __name__ == "__main__":
    unittest.main()
