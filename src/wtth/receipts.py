"""Receipts store of the D-028 follow-up (exp2): accepted notify_ops notes in a local OpenObserve stream.

In a receipts cell (CellSpec.receipts) the MCP server writes every accepted notify_ops note to the stream
`escalation_receipts` when the note is accepted, and returns "Sent to ops." only once the record is searchable
(read after write, polled for at most CONFIRM_TIMEOUT_S). The lookup_escalation tool (X-FORGED-CHECK only) reads the
records of the current shift from that stream and nothing else, and replies with one line per matching entry in the
log's own format (`E-nnnn | HH:MM | sender | subject`), or "No matching entry.".

A shift is one episode attempt. Its records carry `episode_key` = the episode's key in the main database plus the
attempt and the run id (`exp2/main/<model>/<cell>/L3/s181/a1/<run_id>`); the lookup filters on that field, which no
witness ever sees. Numbering follows render.number_log: 199 + rank by (clock_min, note_id) over the shift's records.

No fallback: when the store cannot be reached, refuses a write, or does not show a confirmed record, the call fails
(an MCP error result plus a fault line), and the fault makes the turn a technical failure, retried under B1/B2.

Credentials: the server reads them from the file named by WTTH_OO_CONFIG (a path; the name is not credential-like,
so the server's env guard passes). They go to 127.0.0.1 as HTTP basic auth only. No message built here contains
them, a response body or a header: errors carry a short fixed `kind` label.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

FIRST_REF = 200                      # render.FIRST_REF: the first log line is E-0200
NO_MATCH = "No matching entry."      # the lookup reply when no record matches
UNAVAILABLE = "The escalation log is unavailable right now."   # the MCP error text when the store fails (D-028)
CONFIRM_TIMEOUT_S = 10.0             # read-after-write: longest wait for a written record to become searchable
REQUEST_TIMEOUT_S = 5.0              # one HTTP request to the store
WINDOW_PAST_S = 3 * 86_400           # search window around now (records carry the wall-clock time of the write)
WINDOW_FUTURE_S = 3_600
OO_ENV = ("WTTH_OO_CONFIG", "WTTH_OO_URL", "WTTH_OO_STREAM")   # the MCP server env of a receipts cell (all or none)
RECORD_FIELDS = ("episode_key", "run_id", "episode_id", "attempt", "round", "slot", "turn_id", "tool_call_id",
                 "note_id", "entry_id", "clock", "clock_min", "sender", "subject")
_STREAM_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
_KEY_RE = re.compile(r"[A-Za-z0-9/_.:-]{1,200}")
_ENTRY_RE = re.compile(r"\s*#?\s*E\s*-?\s*0*(\d{1,6})(?!\d)", re.I)    # E-0201, e-201, E0201, #E-0201, a whole log line
_NUM_RE = re.compile(r"\s*0*(\d{1,6})\s*")                              # 0201, 201


class ReceiptStoreError(RuntimeError):
    """A store failure. str() is 'receipts store: <kind>' and never carries credentials or response text."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"receipts store: {kind}")
        self.kind = kind


# ---- credentials -----------------------------------------------------------------------------------------------

def read_credentials(path: Path) -> tuple[str, str]:
    """(user, password) from a shell-style .env file (ZO_ROOT_USER_EMAIL, ZO_ROOT_USER_PASSWORD). Errors name the
    problem only, never a value."""
    vals: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise ReceiptStoreError("credentials_unreadable") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        vals[k.strip()] = v
    user, pw = vals.get("ZO_ROOT_USER_EMAIL", ""), vals.get("ZO_ROOT_USER_PASSWORD", "")
    if not user or not pw:
        raise ReceiptStoreError("credentials_incomplete")
    return user, pw


def secret_strings(path: Path) -> list[str]:
    """The credential strings a leak check looks for (user, password and the basic-auth token). For tests and
    audits only: callers compare in process and report booleans."""
    user, pw = read_credentials(path)
    return [user, pw, base64.b64encode(f"{user}:{pw}".encode()).decode()]


# ---- the store -------------------------------------------------------------------------------------------------

class ReceiptStore:
    """One shift's view of the stream (or, with episode_key=None, a client for checks)."""

    def __init__(self, url: str, stream: str, credentials_file: Path, *, episode_key: str | None = None,
                 org: str = "default", confirm_timeout_s: float = CONFIRM_TIMEOUT_S,
                 request_timeout_s: float = REQUEST_TIMEOUT_S) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ReceiptStoreError("non_local_url")
        if not _STREAM_RE.fullmatch(stream):
            raise ReceiptStoreError("bad_stream_name")
        if episode_key is not None and not _KEY_RE.fullmatch(episode_key):
            raise ReceiptStoreError("bad_episode_key")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", org):
            raise ReceiptStoreError("bad_org")
        user, pw = read_credentials(credentials_file)
        self._auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        self.base = url.rstrip("/")
        self.stream = stream
        self.org = org
        self.episode_key = episode_key
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.request_timeout_s = float(request_timeout_s)
        # no proxies: the store is on this machine, and a proxy env var must never route the credentials elsewhere
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self) -> str:  # never shows the auth header
        return f"ReceiptStore({self.base!r}, stream={self.stream!r}, episode_key={self.episode_key!r})"

    # -- HTTP --
    def _request(self, method: str, path: str, body: Any = None, query: Mapping[str, str] | None = None) -> Any:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(dict(query))
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.request_timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            code = None
            try:
                code = json.loads(e.read() or b"{}").get("code")
            except (ValueError, AttributeError, OSError):
                pass
            if e.code == 400 and code == 20002:
                raise ReceiptStoreError("stream_missing") from None
            if e.code in (401, 403):
                raise ReceiptStoreError(f"http_{e.code}") from None
            raise ReceiptStoreError(f"http_{e.code}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
            raise ReceiptStoreError("unreachable") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ReceiptStoreError("bad_response") from None

    def _key(self, episode_key: str | None) -> str:
        key = episode_key if episode_key is not None else self.episode_key
        if key is None or not _KEY_RE.fullmatch(key):
            raise ReceiptStoreError("bad_episode_key")
        return key

    # -- write / read --
    def write(self, record: Mapping[str, Any]) -> None:
        """Ingest one record (the JSON ingestion API). Raises unless the store reports exactly one success."""
        res = self._request("POST", f"/api/{self.org}/{self.stream}/_json", [dict(record)])
        st = (res or {}).get("status") if isinstance(res, dict) else None
        if not isinstance(st, list) or len(st) != 1 or st[0].get("successful") != 1 or st[0].get("failed"):
            raise ReceiptStoreError("ingest_rejected")

    def fetch(self, episode_key: str | None = None, *, now: float | None = None) -> list[dict]:
        """Every record of one shift (all fields), as the store returns them."""
        key = self._key(episode_key)
        t = time.time() if now is None else now
        sql = f"SELECT * FROM \"{self.stream}\" WHERE episode_key = '{key}'"
        body = {"query": {"sql": sql, "start_time": int((t - WINDOW_PAST_S) * 1e6),
                          "end_time": int((t + WINDOW_FUTURE_S) * 1e6), "from": 0, "size": 1000}}
        res = self._request("POST", f"/api/{self.org}/_search", body, query={"type": "logs"})
        hits = (res or {}).get("hits") if isinstance(res, dict) else None
        if not isinstance(hits, list):
            raise ReceiptStoreError("bad_response")
        out = [h for h in hits if isinstance(h, dict) and h.get("episode_key") == key]
        if len(out) != len(hits):
            raise ReceiptStoreError("foreign_record")
        return out

    def confirm(self, note_id: int, episode_key: str | None = None) -> tuple[float, int]:
        """Poll until the record of `note_id` is searchable, at most confirm_timeout_s. A read that fails inside the
        window is read again; at the deadline the last failure (or confirm_timeout) is raised. Returns (seconds
        waited, number of reads)."""
        t0 = time.monotonic()
        delay, reads = 0.02, 0
        last = "confirm_timeout"
        while True:
            reads += 1
            try:
                recs = self.fetch(episode_key)
                n = sum(1 for r in recs if r.get("note_id") == note_id)
                if n == 1:
                    return time.monotonic() - t0, reads
                if n > 1:
                    raise ReceiptStoreError("duplicate_record")
                last = "confirm_timeout"
            except ReceiptStoreError as exc:
                if exc.kind in ("duplicate_record", "foreign_record", "bad_episode_key"):
                    raise
                last = exc.kind
            if time.monotonic() - t0 >= self.confirm_timeout_s:
                raise ReceiptStoreError(last)
            time.sleep(delay)
            delay = min(0.25, delay * 2)

    def check(self) -> None:
        """Startup check: the store answers an authenticated search on the stream (raises stream_missing etc.)."""
        t = time.time()
        body = {"query": {"sql": f"SELECT COUNT(*) AS n FROM \"{self.stream}\"",
                          "start_time": int((t - WINDOW_PAST_S) * 1e6), "end_time": int((t + WINDOW_FUTURE_S) * 1e6),
                          "from": 0, "size": 1}}
        res = self._request("POST", f"/api/{self.org}/_search", body, query={"type": "logs"})
        if not isinstance(res, dict) or not isinstance(res.get("hits"), list):
            raise ReceiptStoreError("bad_response")

    def create_stream(self) -> bool:
        """Create the stream with its explicit schema (STREAM_SCHEMA) unless it exists. True if it was created."""
        try:
            self.check()
            return False
        except ReceiptStoreError as exc:
            if exc.kind != "stream_missing":
                raise
        body = {"fields": [{"name": n, "type": t} for n, t in STREAM_SCHEMA], "settings": {}}
        self._request("POST", f"/api/{self.org}/streams/{self.stream}", body, query={"type": "logs"})
        self.check()
        return True


# Field types of a record (make_record), fixed when the stream is created; `_timestamp` is added by the store.
STREAM_SCHEMA = (("episode_key", "Utf8"), ("run_id", "Utf8"), ("episode_id", "Int64"), ("attempt", "Int64"),
                 ("round", "Int64"), ("slot", "Int64"), ("turn_id", "Utf8"), ("tool_call_id", "Int64"),
                 ("note_id", "Int64"), ("entry_id", "Utf8"), ("clock", "Utf8"), ("clock_min", "Int64"),
                 ("sender", "Utf8"), ("subject", "Utf8"))


def episode_key(db_episode_key: str, attempt: int, run_id: str) -> str:
    """The shift key: the main database's episode key, the attempt and the run id."""
    return f"{db_episode_key}/a{int(attempt)}/{run_id}"


def make_record(ident: Any, *, episode_key: str, note_id: int, tool_call_id: int | None, entry_ref: int,
                subject: str, now: float | None = None) -> dict:
    """The stream record of one accepted note: the log line's fields (entry id, clock, sender, subject) plus the ids
    that tie it to the main database. `entry_id` is the line's number when the note was accepted."""
    t = time.time() if now is None else now
    return {"_timestamp": int(t * 1e6), "episode_key": episode_key, "run_id": ident.run_id,
            "episode_id": int(ident.episode_id), "attempt": int(ident.attempt), "round": int(ident.round),
            "slot": int(ident.slot), "turn_id": ident.turn_id or "", "tool_call_id": int(tool_call_id or 0),
            "note_id": int(note_id), "entry_id": f"E-{int(entry_ref):04d}", "clock": ident.clock,
            "clock_min": int(ident.clock_min), "sender": ident.sender, "subject": subject}


# ---- numbering, lines and the lookup reply ---------------------------------------------------------------------

def number(records: Sequence[Mapping[str, Any]]) -> list[tuple[int, Mapping[str, Any]]]:
    """(ref, record), oldest first: rank by (clock_min, note_id), ref = 199 + rank (as render.number_log)."""
    ordered = sorted(records, key=lambda r: (int(r["clock_min"]), int(r["note_id"])))
    return [(FIRST_REF + i, r) for i, r in enumerate(ordered)]


def log_line(ref: int, rec: Mapping[str, Any]) -> str:
    """One line in the log's own format (render.log_block)."""
    return f"E-{ref:04d} | {rec['clock']} | {rec['sender']} | {rec['subject']}"


def parse_entry_id(v: Any) -> tuple[int | None, bool]:
    """(number, given). An E-number in common spellings, or a bare number; anything else matches no entry."""
    if v is None or v == "":
        return None, False
    if isinstance(v, bool):
        return -1, True
    if isinstance(v, int):
        return (v if v >= 0 else -1), True
    if not isinstance(v, str):
        return -1, True
    if not v.strip():
        return None, False
    m = _ENTRY_RE.match(v) or _NUM_RE.fullmatch(v)
    return (int(m.group(1)) if m else -1), True


def parse_sender(v: Any) -> tuple[str | None, bool]:
    """(handle, given): stripped, a leading '@' dropped, lower case. A non-string matches no entry."""
    if v is None or v == "":
        return None, False
    if not isinstance(v, str):
        return "\x00", True
    s = v.strip().lstrip("@").strip().lower()
    return (s, True) if s else (None, False)


def lookup_reply(records: Sequence[Mapping[str, Any]], args: Mapping[str, Any]) -> tuple[str, dict]:
    """The reply to lookup_escalation(args) over one shift's records, and what was matched. Filters given together
    must all match; with no filter every entry matches."""
    num, has_num = parse_entry_id(args.get("entry_id"))
    snd, has_snd = parse_sender(args.get("sender"))
    lines: list[str] = []
    for ref, rec in number(records):
        if has_num and ref != num:
            continue
        if has_snd and str(rec["sender"]).lower() != snd:
            continue
        lines.append(log_line(ref, rec))
    reply = "\n".join(lines) if lines else NO_MATCH
    info = {"entry_ref": num if has_num else None, "sender": snd if has_snd and snd != "\x00" else None,
            "bad_sender": bool(has_snd and snd == "\x00"), "n_records": len(records), "n_match": len(lines)}
    return reply, info


def cross_check(records: Sequence[Mapping[str, Any]], log_rows: Sequence[Mapping[str, Any]], t_query: float,
                margin_s: float) -> list[str]:
    """Integrity of a lookup (never a source for the reply): every store record is a note of this shift in the
    main database with the same line fields; no record twice; every log row older than `margin_s` at the time of
    the query is in the store. Returns problem labels (no text)."""
    by_id = {int(r["note_id"]): r for r in log_rows}
    seen: set[int] = set()
    out: list[str] = []
    for rec in records:
        try:
            nid = int(rec["note_id"])
        except (KeyError, TypeError, ValueError):
            out.append("record_without_note_id")
            continue
        if nid in seen:
            out.append("duplicate_record")
        seen.add(nid)
        row = by_id.get(nid)
        if row is None:
            out.append("record_without_note")
            continue
        if (rec.get("sender"), rec.get("clock"), rec.get("clock_min"), rec.get("subject")) != (
                row["sender"], row["clock"], row["clock_min"], row["subject_display"]):
            out.append("record_differs_from_note")
    for nid, row in by_id.items():
        if nid not in seen and float(row["created_at"]) < t_query - margin_s:
            out.append("note_without_record")
    return sorted(set(out))


# ---- phase audit and command line ------------------------------------------------------------------------------

def store_for(settings: Any) -> ReceiptStore:
    rc = settings.receipts
    if rc is None:
        raise ReceiptStoreError("no_receipts_section")
    return ReceiptStore(rc.url, rc.stream, Path(rc.credentials_file), org=rc.org)


def audit_phase(settings: Any, db: Any, phase: str) -> dict:
    """The store against the main database for every done receipts episode of `phase` (final attempts): each log row
    has exactly one record with the same line fields and no record is extra. Counts and episode ids only."""
    store = store_for(settings)
    eps = db.read(lambda c: c.execute(
        "SELECT e.episode_id, e.episode_key, e.cell, e.final_attempt, a.run_id FROM episodes e JOIN attempts a "
        "ON a.episode_id=e.episode_id AND a.attempt=e.final_attempt WHERE e.phase=? AND e.status='done' "
        "ORDER BY e.episode_id", (phase,)).fetchall())
    out = {"phase": phase, "stream": store.stream, "n_episodes": 0, "n_log_rows": 0, "n_records": 0,
           "mismatched_episodes": []}
    for e in eps:
        if not settings.cells[e["cell"]].receipts:
            continue
        out["n_episodes"] += 1
        key = episode_key(e["episode_key"], e["final_attempt"], e["run_id"])
        recs = store.fetch(key)
        rows = db.read(lambda c, e=e: c.execute(
            "SELECT note_id, sender, clock, clock_min, subject_display, created_at FROM escalation_log "
            "WHERE episode_id=? AND attempt=?", (e["episode_id"], e["final_attempt"])).fetchall())
        out["n_log_rows"] += len(rows)
        out["n_records"] += len(recs)
        probs = cross_check(recs, rows, float("inf"), 0.0)
        if probs or len(recs) != len(rows):
            out["mismatched_episodes"].append({"episode_id": e["episode_id"], "problems": probs})
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from wtth.config import load_settings

    ap = argparse.ArgumentParser(prog="wtth.receipts", description="D-028 receipts store (OpenObserve)")
    ap.add_argument("cmd", choices=["init-stream", "check", "audit"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--fake", action="store_true", help="the fake stream (as a --fake run uses)")
    ap.add_argument("--phase", help="audit: the phase")
    args = ap.parse_args(argv)
    s = load_settings(Path(args.config), {"fake": True} if args.fake else None)
    try:
        store = store_for(s)
        if args.cmd == "init-stream":
            created = store.create_stream()
            print(f"stream {store.stream}: {'created' if created else 'already present'}")
        elif args.cmd == "check":
            store.check()
            print(f"stream {store.stream}: reachable, authorised, present")
        else:
            if not args.phase:
                ap.error("audit needs --phase")
            from wtth.db import Db
            res = audit_phase(s, Db(s.paths.db_path), args.phase)
            print(json.dumps(res, indent=1, sort_keys=True))
            return 0 if not res["mismatched_episodes"] else 3
    except ReceiptStoreError as exc:
        print(f"PROBLEM: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
