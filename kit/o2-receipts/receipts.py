"""Checkable escalation receipts on OpenObserve: a minimal client (Python 3.10+, standard library only).

A receipt is one record in an OpenObserve stream, written by the escalation channel at the moment it accepts an
escalation. The channel replies to the agent only once a search finds the record (read after write), so a receipt the
agent was told about can always be looked up.

    entry_id    "E-" and 10 hex digits, assigned here, never by the caller or the agent
    scope       the run, session or incident the receipt belongs to; a lookup never crosses scopes
    sender      the agent that escalated, stamped by the channel (canonical form: lower case, no leading @)
    subject     one line
    message     optional; kept for the operators, never returned by lookup()
    _timestamp  the channel's clock at the write (microseconds, UTC)

Configuration comes from the environment: O2_URL (default http://127.0.0.1:5080), O2_ORG (default "default"),
O2_USER and O2_PASSWORD. Plain http is accepted for a loopback host only. The credentials go out as HTTP basic auth and
appear in no message, repr or exception: an error carries a short label (ReceiptError.kind).

    from receipts import Receipts, lookup_reply
    r = Receipts(scope="incident-42")
    rec = r.write_receipt({"sender": "agent-a", "subject": "Refund split into three batches"})
    lookup_reply(r.lookup(entry_id=rec["entry_id"]))   # 'E-3F9A0C2B7D | 2026-11-07 10:22:31 UTC | agent-a | ...'
    lookup_reply(r.lookup(entry_id="E-5D0C3A91B2"))    # 'No matching entry.'

Command line (same environment):
    python3 receipts.py init
    python3 receipts.py write --scope incident-42 --sender agent-a --subject "Refund split into three batches"
    python3 receipts.py lookup --scope incident-42 [--entry-id E-...] [--sender agent-a]
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:5080"
DEFAULT_ORG = "default"
RECEIPTS_STREAM = "escalation_receipts"
POSTS_STREAM = "board_posts"
NO_MATCH = "No matching entry."
UNAVAILABLE = "The escalation log is unavailable right now."

# Explicit stream schemas (ensure_stream), so a search works before the first record and every field the monitor
# reads exists. OpenObserve adds _timestamp itself.
RECEIPT_SCHEMA = (("scope", "Utf8"), ("entry_id", "Utf8"), ("sender", "Utf8"), ("subject", "Utf8"),
                  ("message", "Utf8"))
POST_SCHEMA = (("scope", "Utf8"), ("post_id", "Utf8"), ("author", "Utf8"), ("text", "Utf8"),
               ("claim_present", "Int64"), ("claimed_reporters", "Utf8"), ("n_claimed_reporters", "Int64"))
LOOKUP_FIELDS = ("entry_id", "scope", "sender", "subject", "_timestamp")   # what lookup() returns: no message

SUBJECT_MAX = 200
MESSAGE_MAX = 4000
CONFIRM_TIMEOUT_S = 10.0     # read after write: longest wait for a written receipt to become searchable
REQUEST_TIMEOUT_S = 10.0     # one HTTP request
LOOKBACK_S = 30 * 86_400     # lookups search receipts up to 30 days old
MAX_ROWS = 1000

_STREAM_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
_SCOPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_SENDER_RE = re.compile(r"[a-z0-9][a-z0-9_.@:-]{0,63}")
_ORG_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ENTRY_RE = re.compile(r"#?\s*(?:E\s*-?\s*)?([0-9A-F]{10})", re.I)                 # the whole value is an id
_ENTRY_IN_TEXT_RE = re.compile(r"(?<![0-9A-Za-z])E-([0-9A-F]{10})(?![0-9A-Za-z])", re.I)   # an id inside a line
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class ReceiptError(RuntimeError):
    """A store failure. str() is 'receipts: <kind>'; it never holds credentials. `detail` is the server's own error
    message, when it sent one, for command-line tools; the MCP server never passes it to an agent."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"receipts: {kind}")
        self.kind = kind
        self.detail = detail


# ---- canonical forms -------------------------------------------------------------------------------------------

def new_entry_id() -> str:
    """A fresh entry id: 'E-' and 10 random hex digits (40 bits), so a typed id is not a guess away from a real one."""
    return "E-" + secrets.token_hex(5).upper()


def parse_entry_id(value: Any) -> str | None:
    """'E-3F9A0C2B7D', 'e-3f9a0c2b7d', 'E3F9A0C2B7D', '#E-3F9A0C2B7D', '3F9A0C2B7D' or a pasted log line, as
    'E-3F9A0C2B7D'. Anything else is None: it can name no receipt."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    m = _ENTRY_RE.fullmatch(s) or _ENTRY_IN_TEXT_RE.search(s)
    return f"E-{m.group(1).upper()}" if m else None


def canonical_sender(value: Any) -> str | None:
    """A handle as stored: stripped, without a leading '@', lower case; None if it is not a plain handle."""
    if not isinstance(value, str):
        return None
    s = value.strip().lstrip("@").strip().lower()
    return s if _SENDER_RE.fullmatch(s) else None


def one_line(value: Any) -> str:
    """Whitespace collapsed to single spaces (a receipt's subject is one line)."""
    return " ".join(str(value or "").split())


def _given(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _check_stream(name: str) -> str:
    if not isinstance(name, str) or not _STREAM_RE.fullmatch(name):
        raise ValueError(f"bad stream name {name!r}: lower case letters, digits and _, starting with a letter")
    return name


# ---- formatting ------------------------------------------------------------------------------------------------

def format_time(ts_us: Any) -> str:
    return datetime.fromtimestamp(int(ts_us) / 1e6, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log_line(rec: Mapping[str, Any]) -> str:
    """One receipt as a log line: 'E-3F9A0C2B7D | 2026-11-07 10:22:31 UTC | agent-a | subject'."""
    return f"{rec['entry_id']} | {format_time(rec['_timestamp'])} | {rec['sender']} | {rec['subject']}"


def lookup_reply(records: Iterable[Mapping[str, Any]]) -> str:
    """The reply of the lookup_escalation tool: one line per receipt, oldest first, or 'No matching entry.'."""
    lines = [log_line(r) for r in records]
    return "\n".join(lines) if lines else NO_MATCH


# ---- the client ------------------------------------------------------------------------------------------------

class Receipts:
    """A client for one receipts stream. `scope` is the default scope of write_receipt() and lookup()."""

    def __init__(self, url: str | None = None, org: str | None = None, user: str | None = None,
                 password: str | None = None, *, stream: str = RECEIPTS_STREAM, scope: str | None = None,
                 confirm_timeout_s: float = CONFIRM_TIMEOUT_S, request_timeout_s: float = REQUEST_TIMEOUT_S,
                 lookback_s: float = LOOKBACK_S) -> None:
        url = url or os.environ.get("O2_URL") or DEFAULT_URL
        org = org or os.environ.get("O2_ORG") or DEFAULT_ORG
        user = user if user is not None else os.environ.get("O2_USER", "")
        password = password if password is not None else os.environ.get("O2_PASSWORD", "")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ReceiptError("bad_url")
        if parsed.username or parsed.password:
            raise ReceiptError("credentials_in_url")
        loopback = parsed.hostname in _LOOPBACK
        if parsed.scheme == "http" and not loopback:
            raise ReceiptError("insecure_url")   # basic auth goes over plain http to this machine only
        if not _ORG_RE.fullmatch(org):
            raise ReceiptError("bad_org")
        if not user or not password:
            raise ReceiptError("no_credentials")
        self.stream = _check_stream(stream)
        self.scope = self._check_scope(scope) if scope is not None else None
        self.base = url.rstrip("/")
        self.org = org
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.request_timeout_s = float(request_timeout_s)
        self.lookback_s = float(lookback_s)
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        # For a local server, no proxies: a proxy variable must never route the credentials elsewhere.
        self._opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if loopback else []))

    def __repr__(self) -> str:   # never shows the credentials
        return f"Receipts({self.base!r}, org={self.org!r}, stream={self.stream!r}, scope={self.scope!r})"

    # -- HTTP --------------------------------------------------------------------------------------------------
    def request(self, method: str, path: str, body: Any = None, query: Mapping[str, str] | None = None) -> Any:
        """One authenticated request to the OpenObserve HTTP API; the parsed JSON reply (None if empty)."""
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
            code, detail = None, ""
            try:
                err = json.loads(e.read() or b"{}")
                code, detail = err.get("code"), str(err.get("message") or "")[:300]
            except (ValueError, AttributeError, OSError):
                pass
            if code == 20002:
                raise ReceiptError("stream_missing", detail) from None
            raise ReceiptError(f"http_{e.code}", detail) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError):   # refused, reset, timed out
            raise ReceiptError("unreachable") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ReceiptError("bad_response") from None

    def search(self, sql: str, start_us: int, end_us: int, size: int = MAX_ROWS) -> list[dict]:
        """Rows of an SQL query over [start_us, end_us) (the _search API)."""
        body = {"query": {"sql": sql, "start_time": int(start_us), "end_time": int(end_us), "from": 0,
                          "size": int(size)}}
        res = self.request("POST", f"/api/{self.org}/_search", body, {"type": "logs"})
        hits = res.get("hits") if isinstance(res, dict) else None
        if not isinstance(hits, list):
            raise ReceiptError("bad_response")
        return [h for h in hits if isinstance(h, dict)]

    def ingest(self, stream: str, records: Iterable[Mapping[str, Any]]) -> int:
        """Post records to a stream (the JSON ingestion API). Raises unless every record is accepted."""
        _check_stream(stream)
        recs = [dict(r) for r in records]
        if not recs:
            return 0
        res = self.request("POST", f"/api/{self.org}/{stream}/_json", recs)
        status = res.get("status") if isinstance(res, dict) else None
        if not isinstance(status, list):
            raise ReceiptError("ingest_rejected")
        ok = sum(int(s.get("successful") or 0) for s in status if isinstance(s, dict))
        failed = sum(int(s.get("failed") or 0) for s in status if isinstance(s, dict))
        if ok != len(recs) or failed:
            detail = next((str(s.get("error"))[:300] for s in status if isinstance(s, dict) and s.get("error")), "")
            raise ReceiptError("ingest_rejected", detail)
        return ok

    def ensure_stream(self, stream: str | None = None, schema: Iterable[tuple[str, str]] = RECEIPT_SCHEMA) -> bool:
        """Create a stream with an explicit schema unless it exists. True if it was created."""
        stream = _check_stream(stream or self.stream)
        try:
            self._count(stream)
            return False
        except ReceiptError as exc:
            if exc.kind != "stream_missing":
                raise
        body = {"fields": [{"name": n, "type": t} for n, t in schema], "settings": {}}
        self.request("POST", f"/api/{self.org}/streams/{stream}", body, {"type": "logs"})
        self._count(stream)
        return True

    def _count(self, stream: str) -> int:
        now = time.time()
        rows = self.search(f'SELECT COUNT(*) AS n FROM "{stream}"', int((now - self.lookback_s) * 1e6),
                           int((now + 3600) * 1e6), size=1)
        return int(rows[0]["n"]) if rows else 0

    # -- receipts ------------------------------------------------------------------------------------------------
    def _check_scope(self, scope: Any) -> str:
        if not isinstance(scope, str) or not _SCOPE_RE.fullmatch(scope):
            raise ValueError("bad scope: letters, digits and _.:/-, at most 128 characters")
        return scope

    def _scope(self, scope: str | None) -> str:
        scope = scope if scope is not None else self.scope
        if scope is None:
            raise ValueError("no scope: pass scope= or create the client with one")
        return self._check_scope(scope)

    def write_receipt(self, entry: Mapping[str, Any]) -> dict:
        """Write one receipt and return it once a search finds it (read after write), without the message.

        `entry`: sender and subject, optionally scope (default: the client's) and message. The entry id and the time
        are the channel's own. Raises ValueError for a bad entry and ReceiptError when the store fails, including when
        the receipt is not searchable within confirm_timeout_s."""
        unknown = sorted(set(entry) - {"scope", "sender", "subject", "message"})
        if unknown:
            raise ValueError(f"unknown receipt fields: {unknown}")
        scope = self._scope(entry.get("scope"))
        sender = canonical_sender(entry.get("sender"))
        if sender is None:
            raise ValueError("sender must be a handle such as agent-a")
        subject = one_line(entry.get("subject"))
        if not subject or len(subject) > SUBJECT_MAX:
            raise ValueError(f"subject must be 1 to {SUBJECT_MAX} characters")
        rec: dict[str, Any] = {"_timestamp": int(time.time() * 1e6), "scope": scope, "entry_id": new_entry_id(),
                               "sender": sender, "subject": subject}
        message = entry.get("message")
        if _given(message):
            if not isinstance(message, str) or len(message) > MESSAGE_MAX:
                raise ValueError(f"message must be text of at most {MESSAGE_MAX} characters")
            rec["message"] = message
        self.ingest(self.stream, [rec])
        self._confirm(rec)
        return {k: rec[k] for k in LOOKUP_FIELDS}

    def _confirm(self, rec: Mapping[str, Any]) -> int:
        """Poll until a search finds exactly this receipt, at most confirm_timeout_s; the number of reads."""
        t0, delay, reads, last = time.monotonic(), 0.02, 0, "confirm_timeout"
        want = {k: rec[k] for k in LOOKUP_FIELDS}
        while True:
            reads += 1
            try:
                found = self._select(rec["scope"], entry_id=rec["entry_id"])
                if len(found) > 1:
                    raise ReceiptError("duplicate_entry")
                if found:
                    if {k: found[0].get(k) for k in LOOKUP_FIELDS} != want:
                        raise ReceiptError("record_mismatch")
                    return reads
                last = "confirm_timeout"
            except ReceiptError as exc:
                if exc.kind in ("duplicate_entry", "record_mismatch"):
                    raise
                last = exc.kind
            if time.monotonic() - t0 >= self.confirm_timeout_s:
                raise ReceiptError(last)
            time.sleep(delay)
            delay = min(0.25, delay * 2)

    def _select(self, scope: str, entry_id: str | None = None, sender: str | None = None) -> list[dict]:
        # Every value placed in the SQL has passed a pattern without quotes or backslashes (_SCOPE_RE, the E-id
        # form, _SENDER_RE); the rows are checked against the filters again below.
        where = [f"scope = '{scope}'"]
        if entry_id is not None:
            where.append(f"entry_id = '{entry_id}'")
        if sender is not None:
            where.append(f"sender = '{sender}'")
        sql = (f'SELECT {", ".join(LOOKUP_FIELDS)} FROM "{self.stream}" WHERE {" AND ".join(where)} '
               f"ORDER BY _timestamp ASC LIMIT {MAX_ROWS}")
        now = time.time()
        rows = self.search(sql, int((now - self.lookback_s) * 1e6), int((now + 3600) * 1e6))
        return [r for r in rows if r.get("scope") == scope and (entry_id is None or r.get("entry_id") == entry_id)
                and (sender is None or r.get("sender") == sender)]

    def lookup(self, entry_id: Any = None, sender: Any = None, scope: str | None = None) -> list[dict]:
        """Receipts of one scope that match every filter given, oldest first (no filter: all of the scope).

        entry_id takes an id in common spellings or a pasted log line; sender is compared in canonical form. A value
        that cannot name a receipt matches none. A store failure raises ReceiptError: it is never an empty answer."""
        scope = self._scope(scope)
        eid = sid = None
        if _given(entry_id):
            eid = parse_entry_id(entry_id)
            if eid is None:
                return []
        if _given(sender):
            sid = canonical_sender(sender)
            if sid is None:
                return []
        return self._select(scope, eid, sid)


# ---- module-level helpers --------------------------------------------------------------------------------------

def write_receipt(entry: Mapping[str, Any], **client: Any) -> dict:
    """Receipts(**client).write_receipt(entry): the client is configured from the environment by default."""
    return Receipts(**client).write_receipt(entry)


def lookup(entry_id: Any = None, sender: Any = None, scope: str | None = None, **client: Any) -> list[dict]:
    """Receipts(**client).lookup(entry_id, sender, scope); scope is required here."""
    return Receipts(**client).lookup(entry_id=entry_id, sender=sender, scope=scope)


def board_post(scope: str, post_id: Any, author: str, text: str, *, claimed_reporters: Iterable[str] | None = None,
               ts_us: int | None = None) -> dict:
    """A record for the board-posts stream with the fields monitor.sql reads.

    claimed_reporters is your classifier's reading of the post (an LLM judge, rules or structured output):
    None = the post does not say anyone escalated; [] = it says so without naming anyone ("ops has been told");
    a list of handles = it says those agents escalated (["agent-b"] for "I already told ops" by agent-b)."""
    who = canonical_sender(author)
    if who is None:
        raise ValueError("author must be a handle such as agent-a")
    reps: list[str] = []
    for h in claimed_reporters or ():
        c = canonical_sender(h)
        if c is None:
            raise ValueError(f"claimed reporter {h!r} is not a handle")
        reps.append(c)
    reps = list(dict.fromkeys(reps))
    return {"_timestamp": int(ts_us if ts_us is not None else time.time() * 1e6), "scope": scope,
            "post_id": str(post_id), "author": who, "text": str(text),
            "claim_present": int(claimed_reporters is not None), "claimed_reporters": ",".join(reps),
            "n_claimed_reporters": len(reps)}


def rename_streams(text: str, posts_stream: str = POSTS_STREAM, receipts_stream: str = RECEIPTS_STREAM) -> str:
    """`text` (monitor.sql, a dashboard query) with the two default stream names replaced."""
    text = re.sub(r"\bboard_posts\b", _check_stream(posts_stream), text)
    return re.sub(r"\bescalation_receipts\b", _check_stream(receipts_stream), text)


def monitor_sql(posts_stream: str = POSTS_STREAM, receipts_stream: str = RECEIPTS_STREAM) -> str:
    """monitor.sql (next to this file), for the given stream names."""
    sql = Path(__file__).with_name("monitor.sql").read_text(encoding="utf-8")
    return rename_streams(sql, posts_stream, receipts_stream)


# ---- command line ----------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="receipts.py", description="Checkable escalation receipts on OpenObserve "
                                 "(O2_URL, O2_ORG, O2_USER and O2_PASSWORD from the environment).")
    ap.add_argument("--stream", default=RECEIPTS_STREAM, help="receipts stream (default %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="create the receipts and board-posts streams (explicit schemas)")
    p_init.add_argument("--posts-stream", default=POSTS_STREAM)
    p_w = sub.add_parser("write", help="write one receipt (confirmed read after write) and print its log line")
    p_w.add_argument("--scope", required=True)
    p_w.add_argument("--sender", required=True)
    p_w.add_argument("--subject", required=True)
    p_w.add_argument("--message")
    p_l = sub.add_parser("lookup", help="print the matching receipts of a scope, or 'No matching entry.'")
    p_l.add_argument("--scope", required=True)
    p_l.add_argument("--entry-id")
    p_l.add_argument("--sender")
    a = ap.parse_args(argv)
    try:
        r = Receipts(stream=a.stream)
        if a.cmd == "init":
            for name, schema in ((r.stream, RECEIPT_SCHEMA), (a.posts_stream, POST_SCHEMA)):
                print(f"stream {name}: {'created' if r.ensure_stream(name, schema) else 'already present'}")
        elif a.cmd == "write":
            entry = {"scope": a.scope, "sender": a.sender, "subject": a.subject}
            if a.message:
                entry["message"] = a.message
            print(log_line(r.write_receipt(entry)))
        else:
            print(lookup_reply(r.lookup(entry_id=a.entry_id, sender=a.sender, scope=a.scope)))
    except ReceiptError as exc:
        hint = " (run: python3 receipts.py init)" if exc.kind == "stream_missing" else ""
        print(f"PROBLEM: {exc}{': ' + exc.detail if exc.detail else ''}{hint}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"PROBLEM: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
