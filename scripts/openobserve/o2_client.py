"""Minimal client for the local OpenObserve instance of the D-027 monitor (stdlib only).

Credentials come from the instance's .env file (default /Users/amey/Projects/wtth-openobserve/.env, override with
WTTH_O2_ENV). They are sent as HTTP basic auth to 127.0.0.1 and never printed. The base URL is fixed to localhost:
the client refuses any other host.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

O2_HOME = Path(os.environ.get("O2_HOME", "/Users/amey/Projects/wtth-openobserve"))
ENV_PATH = Path(os.environ.get("WTTH_O2_ENV", str(O2_HOME / ".env")))
BASE_URL = os.environ.get("WTTH_O2_URL", "http://127.0.0.1:5080")
ORG = "default"


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    """KEY='value' lines of a shell-style .env file (comments and blank lines skipped)."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


class O2Error(RuntimeError):
    pass


class O2:
    def __init__(self, base_url: str = BASE_URL, env_path: Path = ENV_PATH, org: str = ORG) -> None:
        host = urllib.parse.urlparse(base_url).hostname
        if host not in ("127.0.0.1", "localhost"):
            raise O2Error(f"refusing a non-local OpenObserve URL (host {host!r})")
        env = read_env(env_path)
        token = f"{env['ZO_ROOT_USER_EMAIL']}:{env['ZO_ROOT_USER_PASSWORD']}".encode()
        self._auth = "Basic " + base64.b64encode(token).decode()
        self.base, self.org = base_url.rstrip("/"), org

    def request(self, method: str, path: str, body: Any = None, *, query: dict | None = None,
                timeout: float = 120) -> Any:
        url = self.base + path.replace("{org}", self.org)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise O2Error(f"{method} {path}: HTTP {e.code}: {e.read().decode(errors='replace')[:800]}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode(errors="replace")

    def search(self, sql: str, start_us: int, end_us: int, *, size: int = 100_000) -> list[dict]:
        """Rows of an SQL query over [start_us, end_us) (microseconds)."""
        body = {"query": {"sql": sql, "start_time": start_us, "end_time": end_us, "from": 0, "size": size}}
        res = self.request("POST", "/api/{org}/_search", body, query={"type": "logs"})
        return res.get("hits", [])
