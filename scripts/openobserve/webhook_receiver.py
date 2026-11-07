"""Offline alert destination for the D-027 monitor: a tiny HTTP receiver on 127.0.0.1 that writes every POST to a file.

Usage: python3 -I scripts/openobserve/webhook_receiver.py [--port 5091] [--out DIR]

Each notification OpenObserve sends is appended to DIR/received.jsonl as one JSON object (received_at, path,
content_type, body) and the latest body is also written to DIR/latest.txt. DIR defaults to
/Users/amey/Projects/wtth-openobserve/alerts. Request headers other than the content type are not stored.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_OUT = Path(os.environ.get("O2_HOME", "/Users/amey/Projects/wtth-openobserve")) / "alerts"


class Handler(BaseHTTPRequestHandler):
    out_dir: Path = DEFAULT_OUT

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", errors="replace")
        rec = {"received_at": datetime.now(timezone.utc).isoformat(), "path": self.path,
               "content_type": self.headers.get("Content-Type", ""), "body": body}
        with (self.out_dir / "received.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        (self.out_dir / "latest.txt").write_text(body, encoding="utf-8")
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        payload = b"wtth webhook receiver: POST alerts here\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{datetime.now(timezone.utc).isoformat()} {self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5091)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    Handler.out_dir = a.out
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"listening on http://127.0.0.1:{a.port}/, writing to {a.out}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
