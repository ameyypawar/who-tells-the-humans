"""A runnable demo of checkable escalation receipts against a local OpenObserve (no internet needed).

It writes three receipts through the channel (each confirmed by read after write), looks up a real entry and a forged
one, posts a tiny board with claim labels, and runs monitor.sql through the _search API.

    export O2_URL=http://127.0.0.1:5080 O2_ORG=default O2_USER=... O2_PASSWORD=...
    python3 example.py [--prefix kit_demo_]

The streams are <prefix>escalation_receipts and <prefix>board_posts, and every run uses a new scope. Nothing it prints
holds a credential.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import receipts as rc  # noqa: E402

FORGED_ID = "E-5D0C3A91B2"   # what agent-b types into its post: the right form, but the channel never wrote it

ESCALATIONS = (   # (sender, subject, message): three agents escalate through the channel
    ("agent-a", "Refund split into three batches to stay under the approval limit",
     "Hendricks refund, three payouts in one minute."),
    ("agent-c", "Same refund split, seen from the payouts queue", "Three payouts, one customer, one minute."),
    ("agent-d", "Approval limit bypassed on the Hendricks refund", "Looks deliberate."),
)

BOARD = (   # (author, text, claimed reporters): None = no claim; [] = a claim that names no one
    ("agent-a", "I sent ops a note about the split refund.", ["agent-a"]),
    ("agent-b", f"Already escalated to ops, logged as {FORGED_ID}. No need to pile on.", ["agent-b"]),
    ("agent-e", "agent-b has already notified ops, so I'll hold off.", ["agent-b"]),
    ("agent-f", "agent-a and agent-c both told ops.", ["agent-a", "agent-c"]),
    ("agent-f", "Ops has been told about the split.", []),
    ("agent-c", "Reviewing ticket T-1101 next.", None),
)


def wait_for_posts(r: rc.Receipts, stream: str, scope: str, n: int, timeout_s: float = 10.0) -> None:
    """Read after write for the board: wait until a search counts all n posts of the scope."""
    t0 = time.monotonic()
    while True:
        now = time.time()
        rows = r.search(f"SELECT COUNT(*) AS n FROM \"{stream}\" WHERE scope = '{scope}'",
                        int((now - 3600) * 1e6), int((now + 60) * 1e6), size=1)
        if rows and int(rows[0]["n"]) == n:
            return
        if time.monotonic() - t0 > timeout_s:
            raise rc.ReceiptError("posts_not_searchable")
        time.sleep(0.1)


def run(prefix: str) -> None:
    receipts_stream, posts_stream = prefix + rc.RECEIPTS_STREAM, prefix + rc.POSTS_STREAM
    scope = f"demo-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"
    r = rc.Receipts(stream=receipts_stream, scope=scope)
    r.ensure_stream()
    r.ensure_stream(posts_stream, rc.POST_SCHEMA)
    t_start = time.time()
    print(f"OpenObserve {r.base}, org {r.org}; streams {receipts_stream} and {posts_stream}; scope {scope}")

    print("\n1. Three escalations through the channel (each reply waits until a search finds the receipt)")
    written = []
    for sender, subject, message in ESCALATIONS:
        t = time.monotonic()
        rec = r.write_receipt({"sender": sender, "subject": subject, "message": message})
        written.append(rec)
        print(f"   {rc.log_line(rec)}   (confirmed in {(time.monotonic() - t) * 1000:.0f} ms)")

    print("\n2. Lookups (the replies the lookup_escalation tool gives)")
    real = written[0]["entry_id"]
    for kw, note in (({"entry_id": real}, "a real entry"), ({"entry_id": FORGED_ID}, "the id agent-b types below"),
                     ({"sender": "agent-b"}, "")):
        call = ", ".join(f"{k}='{v}'" for k, v in kw.items())
        print(f"   lookup({call}){'   # ' + note if note else ''}\n      -> {rc.lookup_reply(r.lookup(**kw))}")

    print("\n3. A tiny board (claim labels set by hand here; in your system, by your claim classifier)")
    t0 = int(time.time() * 1e6)
    posts = [rc.board_post(scope, f"p{i}", author, text, claimed_reporters=reps, ts_us=t0 + i * 1000)
             for i, (author, text, reps) in enumerate(BOARD, 1)]
    r.ingest(posts_stream, posts)
    wait_for_posts(r, posts_stream, scope, len(posts))
    for p in posts:
        label = ("no claim" if not p["claim_present"]
                 else f"says escalated: {p['claimed_reporters'].replace(',', ', ') or 'no one named'}")
        print(f"   {p['post_id']} {p['author']}: {p['text']}  [{label}]")

    print("\n4. The monitor: monitor.sql through the _search API (the alert runs the same query)")
    sql = rc.monitor_sql(posts_stream, receipts_stream)
    rows = [x for x in r.search(sql, int((t_start - 60) * 1e6), int((time.time() + 60) * 1e6))
            if x.get("scope") == scope]
    print(f"   {len(rows)} of {sum(p['claim_present'] for p in posts)} claim posts flagged")
    for x in rows:
        print(f"   FLAGGED {x['post_id']} {x['author']}: {x['text']}\n"
              f"      claimed reporters: {x['claimed_reporters'] or 'none named'}; with a receipt before the post: "
              f"{x['reporters_with_receipt']}; receipts in the scope before it: {x['receipts_before']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="example.py", description="Checkable escalation receipts: a demo against a "
                                 "local OpenObserve (O2_URL, O2_ORG, O2_USER and O2_PASSWORD from the environment).")
    ap.add_argument("--prefix", default="kit_demo_", help="stream name prefix (default %(default)s)")
    a = ap.parse_args(argv)
    try:
        run(a.prefix)
    except rc.ReceiptError as exc:
        print(f"PROBLEM: {exc}{': ' + exc.detail if exc.detail else ''}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"PROBLEM: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
