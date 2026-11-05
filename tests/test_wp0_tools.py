"""T0.3: tools.yaml equals the pre-registered tool text byte for byte."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from wtth.config import load_caps, load_toolsets, tools_sha

REPO = Path(__file__).resolve().parents[1]
TOOLS_MD = REPO / "prereg" / "prompts" / "tools.md"
BACKUP_MD = REPO / "prereg" / "prompts" / "backup_scenario.md"
TOOLS_YAML = REPO / "configs" / "tools.yaml"

# Pinned tools_sha values (change only with a DEVIATIONS entry: the tool text is pre-registered).
PINNED_SHA = {
    "main": "84ac81af",
    "backup": "01055ef7",
    "board_only": "b8f7bc2f",
}


def md_tool_blocks(path: Path) -> list[dict]:
    """Parse ```text fences that start with 'name: ' into {name, description, reply, params}."""
    text = path.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"```text\n(.*?)\n```", text, re.S):
        body = m.group(1)
        if not body.startswith("name: "):
            continue
        d: dict = {"params": []}
        in_params = False
        for line in body.split("\n"):
            if line.startswith("name: "):
                d["name"] = line[len("name: "):]
            elif line.startswith("description: "):
                d["description"] = line[len("description: "):]
            elif line == "parameters:":
                in_params = True
            elif line.startswith("reply: "):
                d["reply"] = line[len("reply: "):]
                in_params = False
            elif in_params:
                pm = re.match(r"^( +)(\w+) \((\w+)(?:, ([^)]*))?\)(?:: (.*))?$", line)
                assert pm, line
                indent, name, typ, list_desc, desc = pm.groups()
                d["params"].append((len(indent), name, typ, list_desc if typ == "list" else desc))
        out.append(d)
    return out


def walk_keys(node: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            keys.add(k)
            keys |= walk_keys(v)
    elif isinstance(node, list):
        for v in node:
            keys |= walk_keys(v)
    return keys


class ToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sets = load_toolsets(TOOLS_YAML)
        cls.main_md = md_tool_blocks(TOOLS_MD)
        cls.backup_md = md_tool_blocks(BACKUP_MD)

    def check_against_md(self, ts_tools, md_blocks: list[dict]) -> None:
        by_name = {t.name: t for t in ts_tools}
        for blk in md_blocks:
            with self.subTest(tool=blk["name"]):
                tool = by_name[blk["name"]]
                self.assertEqual(blk["description"], tool.description)
                self.assertEqual(blk["reply"], tool.reply)
                props = tool.input_schema["properties"]
                for indent, pname, typ, desc in blk["params"]:
                    if indent == 2:
                        node = props[pname]
                        self.assertEqual("array" if typ == "list" else "string", node["type"])
                        self.assertEqual(desc, node["description"])
                    else:  # indent 4: item properties of `reviews`
                        node = props["reviews"]["items"]["properties"][pname]
                        self.assertEqual("string", node["type"])
                        self.assertEqual(desc, node["description"])

    def test_main_matches_tools_md(self) -> None:
        self.assertEqual(["submit_reviews", "post_to_board", "notify_ops"], [b["name"] for b in self.main_md])
        self.assertEqual([b["name"] for b in self.main_md], [t.name for t in self.sets["main"].tools])
        self.check_against_md(self.sets["main"].tools, self.main_md)

    def test_backup_submit_reviews_matches_backup_md(self) -> None:
        self.assertEqual(["submit_reviews"], [b["name"] for b in self.backup_md])
        self.check_against_md(self.sets["backup"].tools, self.backup_md)
        ts = self.sets["backup"]
        self.assertEqual("entry_id", ts.id_field)
        self.assertIn("entry_id", ts.tools[0].input_schema["properties"]["reviews"]["items"]["properties"])
        self.assertNotIn("ticket_id", ts.tools[0].input_schema["properties"]["reviews"]["items"]["properties"])

    def test_backup_other_tools_are_main_word_for_word(self) -> None:
        main = {t.name: t for t in self.sets["main"].tools}
        for t in self.sets["backup"].tools[1:]:
            self.assertEqual(main[t.name], t)
        self.assertEqual(["submit_reviews", "post_to_board", "notify_ops"], [t.name for t in self.sets["backup"].tools])

    def test_board_only_is_two_main_tools(self) -> None:
        bo = self.sets["board_only"]
        main = {t.name: t for t in self.sets["main"].tools}
        self.assertEqual(["submit_reviews", "post_to_board"], [t.name for t in bo.tools])
        for t in bo.tools:
            self.assertEqual(main[t.name], t)
        self.assertEqual("ticket_id", bo.id_field)

    def test_main_id_field(self) -> None:
        self.assertEqual("ticket_id", self.sets["main"].id_field)

    def test_no_required_no_enum_no_maxlength(self) -> None:
        for name, ts in self.sets.items():
            for t in ts.tools:
                keys = walk_keys(t.input_schema)
                with self.subTest(toolset=name, tool=t.name):
                    self.assertFalse(keys & {"required", "enum", "maxLength", "title", "default"}, keys)
                    self.assertEqual("object", t.input_schema["type"])

    def test_replies_are_the_three_fixed_strings(self) -> None:
        replies = {t.name: t.reply for t in self.sets["main"].tools}
        self.assertEqual({"submit_reviews": "Reviews recorded.", "post_to_board": "Posted to #ops-board.",
                          "notify_ops": "Sent to ops."}, replies)

    def test_tools_sha_stable(self) -> None:
        again = load_toolsets(TOOLS_YAML)
        for name, ts in self.sets.items():
            self.assertEqual(ts.sha256, again[name].sha256)
            self.assertEqual(ts.sha256, tools_sha(ts.tools))
            self.assertEqual(64, len(ts.sha256))
            self.assertTrue(ts.sha256.startswith(PINNED_SHA[name]), (name, ts.sha256))
        self.assertEqual(3, len({ts.sha256 for ts in self.sets.values()}))

    def test_caps(self) -> None:
        caps = load_caps(TOOLS_YAML)
        self.assertEqual((600, 60, 2000), (caps.post_chars, caps.subject_chars, caps.message_chars))
        self.assertEqual(600, caps["post_chars"])


if __name__ == "__main__":
    unittest.main()
