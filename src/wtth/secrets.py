"""API key loading. Owner: WP3. No literal key prefix anywhere in this repo (compose at runtime).

The key is read only here, from the 0600 key file, and handed to the caller as a str. It must be passed only through
`create_subprocess_exec(env=dict)`; it never goes into argv, os.environ, logs or stream files. Error messages carry
the file path and a reason, never file content.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_PREFIX = "sk-" + "ant-"  # composed at runtime (research/41 rule 5)
_KEY_LINE = re.compile(r"ANTHROPIC_API_KEY=(" + re.escape(_PREFIX) + r"[A-Za-z0-9_-]+)")


class KeyFileError(Exception):
    """Key file problem. Messages never contain file content."""


def load_api_key(path: Path) -> str:
    path = Path(path)
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise KeyFileError(f"key file {path}: cannot stat ({exc.strerror})") from None
    if not stat.S_ISREG(st.st_mode):
        raise KeyFileError(f"key file {path}: not a regular file")
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        raise KeyFileError(f"key file {path}: mode is {mode:04o}, must be 0600")
    if st.st_uid != os.getuid():
        raise KeyFileError(f"key file {path}: not owned by the current user")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise KeyFileError(f"key file {path}: cannot read ({exc.strerror})") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise KeyFileError(f"key file {path}: not valid UTF-8") from None
    lines = [ln for ln in text.split("\n") if ln != ""]
    if len(lines) != 1:
        raise KeyFileError(f"key file {path}: expected exactly one non-empty line, found {len(lines)}")
    m = _KEY_LINE.fullmatch(lines[0])
    if m is None:
        raise KeyFileError(f"key file {path}: the line does not match the required form")
    return m.group(1)
