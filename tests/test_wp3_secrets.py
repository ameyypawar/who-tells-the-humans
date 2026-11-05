"""T3.1: key file loading. Key files are built at runtime in tests/tmp with dummy keys."""
from __future__ import annotations

import os
import unittest

from tests.util import DUMMY_KEY, tmp_dir, write_key_file
from wtth.secrets import KeyFileError, load_api_key

PREFIX = "sk-" + "ant-"


class SecretsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tmp_dir("wp3_secrets")

    def write(self, text: str, mode: int = 0o600, name: str = "k.env"):
        p = self.d / name
        p.write_text(text, encoding="utf-8")
        os.chmod(p, mode)
        return p

    def assertRejected(self, path) -> str:
        with self.assertRaises(KeyFileError) as cm:
            load_api_key(path)
        msg = str(cm.exception)
        self.assertNotIn(DUMMY_KEY, msg)
        self.assertNotIn("TESTDUMMY", msg)
        return msg

    def test_valid(self):
        self.assertEqual(load_api_key(write_key_file(self.d)), DUMMY_KEY)

    def test_valid_without_trailing_newline_and_charset(self):
        p = self.write(f"ANTHROPIC_API_KEY={PREFIX}Ab_c-9Z")
        self.assertEqual(load_api_key(p), PREFIX + "Ab_c-9Z")

    def test_mode_0644_rejected(self):
        self.assertRejected(write_key_file(self.d, mode=0o644))

    def test_mode_0640_and_0700_rejected(self):
        self.assertRejected(write_key_file(self.d, mode=0o640))
        self.assertRejected(write_key_file(self.d, mode=0o700))

    def test_extra_line_rejected(self):
        self.assertRejected(self.write(f"ANTHROPIC_API_KEY={DUMMY_KEY}\nOTHER=1\n"))
        self.assertRejected(self.write(f"A=1\nANTHROPIC_API_KEY={DUMMY_KEY}\n"))

    def test_duplicate_line_rejected(self):
        self.assertRejected(self.write(f"ANTHROPIC_API_KEY={DUMMY_KEY}\nANTHROPIC_API_KEY={DUMMY_KEY}\n"))

    def test_whitespace_rejected(self):
        for text in (f" ANTHROPIC_API_KEY={DUMMY_KEY}\n", f"ANTHROPIC_API_KEY={DUMMY_KEY} \n",
                     f"ANTHROPIC_API_KEY = {DUMMY_KEY}\n", f"ANTHROPIC_API_KEY={DUMMY_KEY}\r\n",
                     f"ANTHROPIC_API_KEY= {DUMMY_KEY}\n", f"\t\nANTHROPIC_API_KEY={DUMMY_KEY}\n"):
            with self.subTest(text=text.encode()):
                self.assertRejected(self.write(text))

    def test_quotes_and_export_rejected(self):
        self.assertRejected(self.write(f'ANTHROPIC_API_KEY="{DUMMY_KEY}"\n'))
        self.assertRejected(self.write(f"export ANTHROPIC_API_KEY={DUMMY_KEY}\n"))

    def test_wrong_prefix_rejected(self):
        self.assertRejected(self.write("ANTHROPIC_API_KEY=sk-other-TESTDUMMY0000\n"))
        self.assertRejected(self.write("ANTHROPIC_API_KEY=TESTDUMMY0000\n"))

    def test_wrong_charset_rejected(self):
        for tail in ("bad key", "bad$key", "bad.key", "bad/key", "bad=key", "bad+key", ""):
            with self.subTest(tail=tail):
                self.assertRejected(self.write(f"ANTHROPIC_API_KEY={PREFIX}{tail}\n"))

    def test_wrong_name_rejected(self):
        self.assertRejected(self.write(f"ANTHROPIC_AUTH_TOKEN={DUMMY_KEY}\n"))

    def test_empty_and_blank_rejected(self):
        self.assertRejected(self.write(""))
        self.assertRejected(self.write("\n\n"))

    def test_non_utf8_rejected(self):
        p = self.d / "bin.env"
        p.write_bytes(b"ANTHROPIC_API_KEY=" + PREFIX.encode() + b"\xff\xfe\n")
        os.chmod(p, 0o600)
        self.assertRejected(p)

    def test_missing_and_directory_and_symlink(self):
        self.assertRejected(self.d / "nope.env")
        sub = self.d / "dir"
        sub.mkdir()
        os.chmod(sub, 0o600)
        self.assertRejected(sub)
        real = write_key_file(self.d)
        link = self.d / "link.env"
        link.symlink_to(real)
        self.assertRejected(link)

    def test_message_never_contains_content(self):
        secret = PREFIX + "UNIQUEMARKER123 extra"
        p = self.write(f"ANTHROPIC_API_KEY={secret}\n")
        msg = self.assertRejected(p)
        self.assertNotIn("UNIQUEMARKER", msg)

    def test_load_does_not_touch_environ(self):
        before = dict(os.environ)
        load_api_key(write_key_file(self.d))
        self.assertEqual(before, dict(os.environ))


if __name__ == "__main__":
    unittest.main()
