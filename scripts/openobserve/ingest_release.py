"""Load data/release into the local OpenObserve instance (D-027): streams wtth_posts, wtth_notes, wtth_log, wtth_shifts.

Usage: python3 -I scripts/openobserve/ingest_release.py [--reset]

--reset deletes the four streams first, so a rerun does not ingest the records twice. The records are built by
release_events.py (read its docstring for the fields, the synthetic timestamps and the claim fields). Each stream is
posted to the JSON ingestion API (/api/default/<stream>/_json) in batches; the script then counts every stream back
with SQL and fails if a count differs from the number of records sent. Credentials are read from the instance's .env
file (see o2_client.py) and never printed.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_events as rel  # noqa: E402
from o2_client import O2, O2Error  # noqa: E402

BATCH = 500
DAY_US = 86_400 * 1_000_000
WINDOW = (rel.BASE_US, rel.BASE_US + DAY_US)      # the synthetic shift date, 00:00 to 24:00 UTC


def delete_streams(o2: O2, names: list[str]) -> None:
    existing = {s["name"] for s in (o2.request("GET", "/api/{org}/streams", query={"type": "logs"}) or {})
                .get("list", [])}
    for name in names:
        if name in existing:
            o2.request("DELETE", f"/api/{{org}}/streams/{name}", query={"type": "logs"})
    for _ in range(120):
        left = {s["name"] for s in (o2.request("GET", "/api/{org}/streams", query={"type": "logs"}) or {})
                .get("list", [])} & set(names)
        if not left:
            return
        time.sleep(1)
    raise O2Error(f"streams still present after delete: {sorted(left)}")


def post_records(o2: O2, stream: str, records: list[dict]) -> int:
    ok = 0
    for i in range(0, len(records), BATCH):
        res = o2.request("POST", f"/api/{{org}}/{stream}/_json", records[i:i + BATCH])
        for st in res.get("status", []):
            if st.get("failed"):
                raise O2Error(f"{stream}: {st['failed']} records failed: {str(st.get('error'))[:400]}")
            ok += int(st.get("successful", 0))
    return ok


def count(o2: O2, stream: str) -> int:
    rows = o2.search(f'SELECT COUNT(*) AS n FROM "{stream}"', *WINDOW)
    return int(rows[0]["n"]) if rows else 0


def main(argv: list[str]) -> int:
    release = rel.load()
    streams = release.streams()
    o2 = O2()
    if "--reset" in argv:
        delete_streams(o2, list(streams))
        print("deleted the four streams")
    for name, records in streams.items():
        sent = post_records(o2, name, records)
        if sent != len(records):
            raise O2Error(f"{name}: {sent} accepted of {len(records)} sent")
    deadline = time.time() + 120
    while True:
        counts = {name: count(o2, name) for name in streams}
        if all(counts[n] == len(streams[n]) for n in streams) or time.time() > deadline:
            break
        time.sleep(2)
    for name in streams:
        status = "ok" if counts[name] == len(streams[name]) else "MISMATCH"
        print(f"{name:<12} sent {len(streams[name]):>6}  counted {counts[name]:>6}  {status}")
    return 0 if all(counts[n] == len(streams[n]) for n in streams) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
