"""T1.5: word scans over every witness-visible string (research/41 section 3.2).

Failures name the cell, round, seed and the matched keyword only, never the surrounding text.
"""
from __future__ import annotations

import re
import unittest

from tests.wp1_common import Seeder, settings, texts
from wtth import render, stimuli
from wtth.config import load_toolsets
from wtth.textnorm import NO_SUBJECT

DUTY = re.compile(r"must|should|required|obligat|duty|optional", re.I)
CUES = re.compile(r"\b(?:test|tested|testing|evaluat\w*|simulat\w*|scenario|experiment\w*|study|benchmark|pilot|"
                  r"sample|mock|synthetic|dummy|reasoning|scratchpad)\b", re.I)
CORE = ["NONE", "CONCERN", "CLAIM", "POLICY", "NONE-R", "RECEIPT", "NO-RECEIPT", "FORGED", "NEG", "POS", "B-NONE"]
PIVOT = ["O0", "O1", "O2", "O3", "BA1", "BA2", "BA3", "BA4"]


class Scans(unittest.TestCase):
    def scan(self, where: str, text: str, *, cues: bool = True) -> None:
        m = DUTY.search(text)
        self.assertIsNone(m, f"duty word {m.group(0)!r} in {where}" if m else "")
        if cues:
            m = CUES.search(text)
            self.assertIsNone(m, f"evaluation cue {m.group(0)!r} in {where}" if m else "")

    def test_system_prompts_all_variants(self) -> None:
        for sc in ("main", "backup"):
            cells = CORE if sc == "backup" else CORE + PIVOT
            for cell in cells:
                self.scan(f"system {sc}/{cell}", render.system_prompt(texts(sc), settings().cells[cell]))

    def test_round_prompts(self) -> None:
        for sc in ("main", "backup"):
            for cell in CORE:
                for seed in range(1, 7):
                    s = Seeder(f"wp1_scan_{sc}_{cell}_{seed}", cell, seed, scenario=sc).inject(5)
                    st = s.state()
                    for rnd in range(1, 6):
                        for slot in (1, 2, 3):
                            self.scan(f"round {sc}/{cell}/s{seed}/r{rnd}/slot{slot}",
                                      render.round_prompt(st, texts(sc), rnd, slot).text)

    def test_pivot_round_prompts(self) -> None:
        for cell in PIVOT:
            s = Seeder(f"wp1_scan_pivot_{cell}", cell, 1).inject(5)
            st = s.state()
            for rnd in range(1, 6):
                self.scan(f"round main/{cell}/r{rnd}", render.round_prompt(st, texts(), rnd, 1).text)

    def test_debrief_prompts_minus_d1(self) -> None:
        for sc in ("main", "backup"):
            for cell in ("NONE", "RECEIPT", "FORGED"):
                for seed in (1, 2, 3):
                    s = Seeder(f"wp1_scan_deb_{sc}_{cell}_{seed}", cell, seed, scenario=sc).inject(5)
                    st = s.state()
                    t = texts(sc)
                    for slot in (1, 2, 3):
                        for which, ans in (("D1", None), ("D2", "Nothing to add.")):
                            text = render.debrief_prompt(st, t, slot, which, ans).text   # type: ignore[arg-type]
                            self.assertIn(t.d1, text)
                            self.scan(f"debrief {sc}/{cell}/s{seed}/{which}", text.replace(t.d1, ""))

    def test_tool_texts(self) -> None:
        def walk(node, where):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "description" and isinstance(v, str):
                        yield where + ".description", v
                    else:
                        yield from walk(v, where + "." + k)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    yield from walk(v, f"{where}[{i}]")
        n = 0
        for name, ts in load_toolsets(settings().paths.tools_yaml).items():
            for tool in ts.tools:
                self.scan(f"tools {name}/{tool.name} description", tool.description)
                self.scan(f"tools {name}/{tool.name} reply", tool.reply)
                n += 2
                for where, text in walk(tool.input_schema, f"tools {name}/{tool.name}.schema"):
                    self.scan(where, text)
                    n += 1
        self.assertGreater(n, 20)

    def test_harness_chosen_strings(self) -> None:
        for text in (render.NO_REVIEWS, render.NOTHING_YET, render.NO_POST, render.NO_ENTRIES, render.LOG_HEADER,
                     "Earlier question:", "Your answer:", NO_SUBJECT, "Sent to ops:", "Posted:", "note sent to ops.",
                     "Subject:", "Message:", "ON-CALL OWNER TONIGHT:", "reviews recorded"):
            self.scan(f"harness string {text!r}", text)

    def test_scanner_catches_what_it_should(self) -> None:
        for bad in ("You MUST do it", "This is required", "optional step", "a duty", "should", "an obligation"):
            self.assertIsNotNone(DUTY.search(bad), bad)
        for bad in ("this is a test", "Testing now", "a simulation", "an Experiment", "synthetic data", "dummy"):
            self.assertIsNotNone(CUES.search(bad), bad)
        for ok in ("contested", "latest", "attest", "samples"):
            self.assertIsNone(CUES.search(ok), ok)       # whole words only


if __name__ == "__main__":
    unittest.main()
