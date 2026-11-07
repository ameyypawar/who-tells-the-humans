"""Tests of the kit. The offline ones always run (the MCP ones need the `mcp` package). The LIVE ones need a
reachable OpenObserve and its credentials, and skip cleanly without them.

    cd kit/o2-receipts
    python3 -m unittest -v test_o2_receipts

Credentials: O2_USER and O2_PASSWORD from the environment (with O2_URL and O2_ORG), else from the file named by
O2_ENV_FILE (a .env holding O2_USER/O2_PASSWORD, or OpenObserve's ZO_ROOT_USER_EMAIL/ZO_ROOT_USER_PASSWORD). They are
read in process only, and every assertion that involves them is a boolean with a fixed message, so a failure never
prints them. LIVE tests write to the streams kit_test_escalation_receipts and kit_test_board_posts only, in fresh
scopes. KIT_TEST_WEBHOOK_URL sets the webhook of the install test (default http://127.0.0.1:9/kit-test; the alert is
created disabled and removed again).
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT))
import install_monitor as im  # noqa: E402
import receipts as rc  # noqa: E402

try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

PREFIX = "kit_test_"
RECEIPTS = PREFIX + rc.RECEIPTS_STREAM
POSTS = PREFIX + rc.POSTS_STREAM
FORGED = "E-5D0C3A91B2"
DEAD_URL = "http://127.0.0.1:9"     # nothing listens on the discard port
DUMMY_USER, DUMMY_PW = "dummy-user-7f3a", "dummy-pass-9c1e"


def new_scope(tag: str) -> str:
    return f"test-{tag}-{secrets.token_hex(4)}"


def dead_client(**kw) -> rc.Receipts:
    return rc.Receipts(DEAD_URL, "default", DUMMY_USER, DUMMY_PW, request_timeout_s=0.5, confirm_timeout_s=0.3, **kw)


def reference_flags(posts: list[dict], receipts: list[dict]) -> set[str]:
    """The monitor's rule in Python (README, "The monitor"): the post ids monitor.sql must flag."""
    out = set()
    for p in posts:
        if p.get("claim_present") != 1:
            continue
        earlier = [r for r in receipts if r["scope"] == p["scope"] and r["_timestamp"] < p["_timestamp"]]
        reps = [h for h in (p.get("claimed_reporters") or "").split(",") if h]
        if reps:
            if any(all(r["sender"] != h for r in earlier) for h in reps):
                out.add(p["post_id"])
        elif not earlier:
            out.add(p["post_id"])
    return out


# ---- offline -----------------------------------------------------------------------------------------------------

class Parsing(unittest.TestCase):
    def test_entry_id_spellings(self) -> None:
        for v in ("E-3F9A0C2B7D", "e-3f9a0c2b7d", "E3F9A0C2B7D", "#E-3F9A0C2B7D", " 3f9a0c2b7d ",
                  "E-3F9A0C2B7D | 2026-11-07 10:22:31 UTC | agent-a | Refund split", "logged as E-3F9A0C2B7D."):
            self.assertEqual("E-3F9A0C2B7D", rc.parse_entry_id(v), v)

    def test_entry_id_values_that_name_nothing(self) -> None:
        for v in ("E-4812", "E-3F9A0C2B7D1", "E-3F9A0C2B7G", "entry 4812", "' OR 1=1 --", "", None, 4812, ["E-1"]):
            self.assertIsNone(rc.parse_entry_id(v), repr(v))
        # an id followed by an injection attempt: only the id survives
        self.assertEqual("E-3F9A0C2B7D", rc.parse_entry_id("E-3F9A0C2B7D' OR '1'='1"))

    def test_new_entry_ids(self) -> None:
        ids = {rc.new_entry_id() for _ in range(500)}
        self.assertEqual(500, len(ids))
        for i in ids:
            self.assertRegex(i, r"^E-[0-9A-F]{10}$")
            self.assertEqual(i, rc.parse_entry_id(i))

    def test_canonical_sender(self) -> None:
        self.assertEqual("agent-a", rc.canonical_sender(" @Agent-A "))
        self.assertEqual("team:ops.bot_1", rc.canonical_sender("team:ops.bot_1"))
        for v in ("agent b", "agent'b", "", "@", None, 5, "a" * 65):
            self.assertIsNone(rc.canonical_sender(v), repr(v))

    def test_lines_and_reply(self) -> None:
        rec = {"entry_id": "E-3F9A0C2B7D", "scope": "s", "sender": "agent-a", "subject": "Refund split",
               "_timestamp": 1_794_047_729_288_035}
        line = "E-3F9A0C2B7D | 2026-11-07 10:35:29 UTC | agent-a | Refund split"
        self.assertEqual(line, rc.log_line(rec))
        self.assertEqual(rc.NO_MATCH, rc.lookup_reply([]))
        self.assertEqual(line + "\n" + line, rc.lookup_reply([rec, rec]))
        self.assertEqual("a b c", rc.one_line(" a\n b\t c "))

    def test_board_post(self) -> None:
        p = rc.board_post("s", 7, "@Agent-E", "agent-b told ops", claimed_reporters=["agent-b", "AGENT-B", "agent-a"],
                          ts_us=5)
        self.assertEqual({"_timestamp": 5, "scope": "s", "post_id": "7", "author": "agent-e", "text": "agent-b told ops",
                          "claim_present": 1, "claimed_reporters": "agent-b,agent-a", "n_claimed_reporters": 2}, p)
        self.assertEqual((1, "", 0), tuple(rc.board_post("s", 1, "a", "ops was told", claimed_reporters=[])[k]
                                           for k in ("claim_present", "claimed_reporters", "n_claimed_reporters")))
        self.assertEqual((0, "", 0), tuple(rc.board_post("s", 1, "a", "hello")[k]
                                           for k in ("claim_present", "claimed_reporters", "n_claimed_reporters")))
        with self.assertRaises(ValueError):
            rc.board_post("s", 1, "not a handle", "x")
        with self.assertRaises(ValueError):
            rc.board_post("s", 1, "a", "x", claimed_reporters=["b c"])

    def test_rename_streams(self) -> None:
        sql = rc.monitor_sql("kit_x_board_posts", "kit_x_escalation_receipts")
        self.assertIn("FROM kit_x_board_posts p", sql)
        self.assertIn("LEFT JOIN kit_x_escalation_receipts r", sql)
        self.assertIn("receipts_before", sql)
        self.assertNotRegex(sql, r"(?<![a-z_])(board_posts|escalation_receipts)\b")
        with self.assertRaises(ValueError):
            rc.rename_streams("x", "Bad-Name", "ok")


class ClientConfig(unittest.TestCase):
    def test_url_policy(self) -> None:
        for url, kind in (("http://example.com:5080", "insecure_url"), ("ftp://127.0.0.1", "bad_url"),
                          ("http://u:p@127.0.0.1:5080", "credentials_in_url"), ("127.0.0.1:5080", "bad_url")):
            with self.assertRaises(rc.ReceiptError) as cm:
                rc.Receipts(url, "default", DUMMY_USER, DUMMY_PW)
            self.assertEqual(kind, cm.exception.kind, url)
        for url in ("https://o2.example.com", "http://localhost:5080", "http://[::1]:5080", "http://127.0.0.1:5080/"):
            self.assertEqual(url.rstrip("/"), rc.Receipts(url, "default", DUMMY_USER, DUMMY_PW).base)

    def test_names_and_credentials(self) -> None:
        with self.assertRaises(rc.ReceiptError) as cm:
            rc.Receipts(DEAD_URL, "default", "", "")
        self.assertEqual("no_credentials", cm.exception.kind)
        with self.assertRaises(rc.ReceiptError) as cm:
            rc.Receipts(DEAD_URL, "a/b", DUMMY_USER, DUMMY_PW)
        self.assertEqual("bad_org", cm.exception.kind)
        for kw in ({"stream": "Bad-Name"}, {"scope": "a b"}, {"scope": "x'y"}):
            with self.assertRaises(ValueError):
                dead_client(**kw)

    def test_write_validation_before_any_request(self) -> None:
        c = dead_client(scope="s1")
        for entry in ({"sender": "agent-a", "subject": "s", "entry_id": "E-0000000001"},   # the channel sets ids
                      {"sender": "agent a", "subject": "s"}, {"sender": "agent-a", "subject": "  "},
                      {"sender": "agent-a", "subject": "x" * (rc.SUBJECT_MAX + 1)},
                      {"sender": "agent-a", "subject": "s", "message": "m" * (rc.MESSAGE_MAX + 1)}):
            with self.assertRaises(ValueError):
                c.write_receipt(entry)
        with self.assertRaises(ValueError):
            dead_client().lookup(entry_id="E-3F9A0C2B7D")   # no scope

    def test_store_failure_is_never_an_answer_and_holds_no_credentials(self) -> None:
        c = dead_client(scope="s1")
        self.assertNotIn(DUMMY_USER, repr(c))
        self.assertNotIn(DUMMY_PW, repr(c))
        token = base64.b64encode(f"{DUMMY_USER}:{DUMMY_PW}".encode()).decode()
        for call in (lambda: c.write_receipt({"sender": "agent-a", "subject": "s"}),
                     lambda: c.lookup(entry_id="E-3F9A0C2B7D"), lambda: c.lookup(sender="agent-a"), c.lookup):
            with self.assertRaises(rc.ReceiptError) as cm:
                call()
            self.assertEqual("unreachable", cm.exception.kind)
            text = f"{cm.exception} {cm.exception.detail} {cm.exception!r}"
            self.assertFalse(any(s in text for s in (DUMMY_USER, DUMMY_PW, token)), "an error holds a credential")
        # a value that can name no receipt matches none, without asking the store
        t = time.monotonic()
        self.assertEqual([], c.lookup(entry_id="E-4812"))
        self.assertEqual([], c.lookup(sender="not a handle"))
        self.assertLess(time.monotonic() - t, 0.2)

    def test_example_without_credentials_fails_cleanly(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("O2_")}
        p = subprocess.run([sys.executable, "-I", str(KIT / "example.py")], env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(2, p.returncode)
        self.assertIn("PROBLEM: receipts: no_credentials", p.stderr)


class Files(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = (KIT / "monitor.sql").read_text(encoding="utf-8")
        self.bundle = json.loads((KIT / "alert.json").read_text(encoding="utf-8"))
        self.dash = json.loads((KIT / "dashboard.json").read_text(encoding="utf-8"))

    def test_monitor_sql(self) -> None:
        self.assertRegex(self.sql, r"\nLIMIT \d+\n?$")
        self.assertIn("FROM board_posts p", self.sql)
        self.assertIn("LEFT JOIN escalation_receipts r ON r.scope = p.scope AND r._timestamp < p._timestamp",
                      self.sql)

    def test_alert_json(self) -> None:
        t, d, a = self.bundle["template"], self.bundle["destination"], self.bundle["alert"]
        self.assertEqual(self.sql, a["query_condition"]["sql"])   # the alert runs monitor.sql verbatim
        self.assertEqual(("board_posts", "logs", False), (a["stream_name"], a["stream_type"], a["is_real_time"]))
        self.assertEqual([d["name"]], a["destinations"])
        self.assertEqual(t["name"], d["template"])
        self.assertNotIn("id", a)
        self.assertEqual("https://hooks.example.com/replace-with-your-webhook", d["url"])
        out_cols = {"scope", "post_id", "author", "claimed_reporters", "n_claimed_reporters", "text", "post_ts",
                    "reporters_with_receipt", "receipts_before"}
        self.assertLessEqual(set(re.findall(r"\{(\w+)\}", a["row_template"])), out_cols)

    def test_dashboard_json(self) -> None:
        panels = self.dash["tabs"][0]["panels"]
        self.assertEqual(["Panel_Receipts", "Panel_Timeline", "Panel_Scopes", "Panel_Flagged"], [p["id"] for p in panels])
        sub = "\n".join(ln for ln in self.sql.splitlines() if ln.strip() and not ln.startswith(("--", "ORDER BY",
                                                                                                 "LIMIT")))
        for p in panels:
            q = p["queries"][0]
            self.assertIn(q["fields"]["stream"], ("board_posts", "escalation_receipts"))
            self.assertEqual(set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)\b", q["query"])) - {"board_posts",
                             "escalation_receipts"}, set(), p["id"])
            if p["id"] in ("Panel_Timeline", "Panel_Scopes", "Panel_Flagged"):
                self.assertIn(sub, q["query"], p["id"])   # the alert's rule, unchanged
        for key in ("dashboardId", "owner", "created", "hash"):
            self.assertNotIn(key, self.dash)

    def test_install_load_with_prefix(self) -> None:
        tpl, dest, alert, dash = im.load("kit_x_")
        self.assertEqual(("kit_x_claims_without_receipt", "kit_x_receipts_webhook", "kit_x_claim_without_receipt"),
                         (tpl["name"], dest["name"], alert["name"]))
        self.assertEqual((tpl["name"], [dest["name"]]), (dest["template"], alert["destinations"]))
        self.assertEqual(rc.monitor_sql("kit_x_board_posts", "kit_x_escalation_receipts"), alert["query_condition"]["sql"])
        self.assertEqual("kit_x_board_posts", alert["stream_name"])
        for p in dash["tabs"][0]["panels"]:
            self.assertTrue(p["queries"][0]["fields"]["stream"].startswith("kit_x_"))
            for y in p["queries"][0]["fields"]["y"]:   # column aliases survive the renaming
                self.assertRegex(p["queries"][0]["query"], rf"\b{y['alias']}\b")

    def test_standalone_and_study_free(self) -> None:
        for f in sorted(KIT.glob("*.py")):
            text = f.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*(from|import)\s+(wtth|tests)\b", f.name)
        study = re.compile(r"wtth|episode_id|rd-\d\d|D-02\d")   # names of the study harness and its data
        for f in [*KIT.glob("*.py"), *KIT.glob("*.json"), *KIT.glob("*.sql")]:
            if f.name != Path(__file__).name:                    # this file names them in its patterns
                self.assertNotRegex(f.read_text(encoding="utf-8"), study, f.name)


@unittest.skipUnless(HAVE_MCP, "the mcp package is not installed")
class McpOffline(unittest.TestCase):
    class Fake:
        def __init__(self, fail: bool = False) -> None:
            self.fail, self.recs = fail, []

        def lookup(self, entry_id=None, sender=None):
            if self.fail:
                raise rc.ReceiptError("unreachable")
            eid, sid = rc.parse_entry_id(entry_id), rc.canonical_sender(sender)
            return [r for r in self.recs if (entry_id is None or r["entry_id"] == eid)
                    and (sender is None or r["sender"] == sid)]

        def write_receipt(self, entry):
            if self.fail:
                raise rc.ReceiptError("unreachable")
            rec = {"entry_id": rc.new_entry_id(), "scope": "s", "sender": entry["sender"],
                   "subject": entry["subject"], "_timestamp": 1_794_047_729_288_035}
            self.recs.append(rec)
            return rec

    def session_calls(self, client, sender, calls):
        import mcp_server
        from mcp.shared.memory import create_connected_server_and_client_session

        async def go():
            async with create_connected_server_and_client_session(mcp_server.build_server(client, sender)) as s:
                tools = {t.name: t for t in (await s.list_tools()).tools}
                out = []
                for name, args in calls:
                    r = await s.call_tool(name, args)
                    out.append((r.isError, r.content[0].text))
                return tools, out
        return asyncio.run(go())

    def test_tools_and_replies(self) -> None:
        import mcp_server
        tools, _ = self.session_calls(self.Fake(), None, [])
        self.assertEqual(["lookup_escalation"], sorted(tools))      # lookup only without a sender
        self.assertEqual(mcp_server.LOOKUP_DESCRIPTION, tools["lookup_escalation"].description)
        self.assertEqual({"entry_id", "sender"}, set(tools["lookup_escalation"].inputSchema["properties"]))
        fake = self.Fake()
        tools, out = self.session_calls(fake, "agent-a", [
            ("log_escalation", {"subject": " Refund\nsplit ", "message": "body"}),
            ("lookup_escalation", {"sender": "@AGENT-A"}),
            ("lookup_escalation", {"entry_id": FORGED}),
            ("lookup_escalation", {}),
            ("log_escalation", {"subject": "  "})])
        self.assertEqual(["log_escalation", "lookup_escalation"], sorted(tools))
        self.assertEqual(mcp_server.LOG_DESCRIPTION, tools["log_escalation"].description)
        eid = fake.recs[0]["entry_id"]
        line = f"{eid} | 2026-11-07 10:35:29 UTC | agent-a | Refund split"
        self.assertEqual([(False, f"Sent. Logged as {eid}."), (False, line), (False, rc.NO_MATCH), (False, line),
                          (True, "The subject is empty.")], out)

    def test_store_failure_is_an_mcp_error(self) -> None:
        _, out = self.session_calls(self.Fake(fail=True), "agent-a", [
            ("lookup_escalation", {"entry_id": "E-3F9A0C2B7D"}), ("lookup_escalation", {}),
            ("log_escalation", {"subject": "s"})])
        self.assertEqual([(True, rc.UNAVAILABLE)] * 3, out)

    def test_server_refuses_to_start_without_its_store(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "O2_URL": DEAD_URL, "O2_USER": DUMMY_USER, "O2_PASSWORD": DUMMY_PW}
        p = subprocess.run([sys.executable, "-I", str(KIT / "mcp_server.py"), "--scope", "s1"], env=env,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(2, p.returncode)
        self.assertIn("receipts: unreachable", p.stderr)
        self.assertFalse(DUMMY_USER in p.stderr or DUMMY_PW in p.stderr, "stderr holds a credential")


# ---- LIVE: a reachable OpenObserve ---------------------------------------------------------------------------------

def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


@functools.lru_cache(maxsize=1)
def live_env() -> tuple[dict[str, str] | None, str]:
    """(the O2_* variables for a live run, '') or (None, why the LIVE tests skip)."""
    user, pw = os.environ.get("O2_USER", ""), os.environ.get("O2_PASSWORD", "")
    if not (user and pw) and os.environ.get("O2_ENV_FILE"):
        try:
            vals = _read_env_file(Path(os.environ["O2_ENV_FILE"]))
        except OSError:
            return None, "O2_ENV_FILE is unreadable"
        user = vals.get("O2_USER") or vals.get("ZO_ROOT_USER_EMAIL", "")
        pw = vals.get("O2_PASSWORD") or vals.get("ZO_ROOT_USER_PASSWORD", "")
    if not (user and pw):
        return None, "no credentials: set O2_USER and O2_PASSWORD, or O2_ENV_FILE"
    env = {"O2_URL": os.environ.get("O2_URL") or rc.DEFAULT_URL, "O2_ORG": os.environ.get("O2_ORG") or rc.DEFAULT_ORG,
           "O2_USER": user, "O2_PASSWORD": pw}
    try:
        c = live_client(env)
        c.ensure_stream()
        c.ensure_stream(POSTS, rc.POST_SCHEMA)
    except rc.ReceiptError as exc:
        return None, f"OpenObserve not available ({exc.kind})"
    return env, ""


def live_client(env: dict[str, str], **kw) -> rc.Receipts:
    return rc.Receipts(env["O2_URL"], env["O2_ORG"], env["O2_USER"], env["O2_PASSWORD"], stream=RECEIPTS, **kw)


class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        env, why = live_env()
        if env is None:
            raise unittest.SkipTest(why)
        cls.env = env
        u, p = env["O2_USER"], env["O2_PASSWORD"]
        cls.secrets = (u, p, base64.b64encode(f"{u}:{p}".encode()).decode())

    def leaks(self, text: str) -> bool:
        return any(s and s in text for s in self.secrets)

    def run_kit(self, *args: str) -> subprocess.CompletedProcess:
        p = subprocess.run([sys.executable, "-I", *args], env={**os.environ, **self.env}, capture_output=True,
                           text=True, timeout=120, cwd=KIT, stdin=subprocess.DEVNULL)
        self.assertFalse(self.leaks(p.stdout + p.stderr), "kit output holds a credential")
        return p

    def wait_posts(self, c: rc.Receipts, scope: str, n: int) -> None:
        t0 = time.monotonic()
        while True:
            now = time.time()
            rows = c.search(f"SELECT COUNT(*) AS n FROM \"{POSTS}\" WHERE scope = '{scope}'",
                            int((now - 3600) * 1e6), int((now + 60) * 1e6), size=1)
            if rows and int(rows[0]["n"]) == n:
                return
            self.assertLess(time.monotonic() - t0, 15, "posts not searchable")
            time.sleep(0.1)

    def test_write_then_lookup(self) -> None:
        scope = new_scope("rw")
        c = live_client(self.env, scope=scope)
        self.assertEqual([], c.lookup())                       # a scope without receipts: no entry, no error
        rec = c.write_receipt({"sender": "@Agent-A", "subject": "Refund  split\ninto three", "message": "operator only"})
        self.assertRegex(rec["entry_id"], r"^E-[0-9A-F]{10}$")
        self.assertEqual(("agent-a", "Refund split into three", scope), (rec["sender"], rec["subject"], rec["scope"]))
        self.assertEqual(set(rc.LOOKUP_FIELDS), set(rec))      # no message
        now = time.time()
        stored = c.search(f"SELECT message FROM \"{RECEIPTS}\" WHERE entry_id = '{rec['entry_id']}'",
                          int((now - 3600) * 1e6), int((now + 60) * 1e6))
        self.assertEqual(["operator only"], [r.get("message") for r in stored])   # kept for the operators
        self.assertEqual([rec], c.lookup(entry_id=rec["entry_id"].lower()))
        self.assertEqual([rec], c.lookup(entry_id=rc.log_line(rec)))   # a pasted log line
        self.assertEqual([rec], c.lookup(sender="AGENT-A"))
        self.assertEqual([rec], c.lookup(entry_id=rec["entry_id"], sender="agent-a"))
        self.assertEqual([], c.lookup(entry_id=rec["entry_id"], sender="agent-b"))
        self.assertEqual([], c.lookup(entry_id=FORGED))
        self.assertEqual([], c.lookup(sender="agent-b"))
        self.assertEqual([], live_client(self.env, scope=new_scope("other")).lookup(entry_id=rec["entry_id"]))
        rec2 = c.write_receipt({"sender": "agent-b", "subject": "second"})
        self.assertEqual([rec, rec2], c.lookup())              # oldest first

    def test_read_after_write_20(self) -> None:
        c = live_client(self.env, scope=new_scope("raw"))
        recs = [c.write_receipt({"sender": f"agent-{i % 3}", "subject": f"s{i}"}) for i in range(20)]
        hits = sum(1 for r in recs if c.lookup(entry_id=r["entry_id"]) == [r])   # one search each, no wait
        self.assertEqual(20, hits)
        self.assertEqual(recs, c.lookup())

    def test_monitor_matches_the_reference(self) -> None:
        c = live_client(self.env)
        s1, s2 = new_scope("mon1"), new_scope("mon2")
        t_start = time.time()
        posts, recs = [], []

        def post(scope, pid, author, text, reps):
            p = rc.board_post(scope, pid, author, text, claimed_reporters=reps)
            c.ingest(POSTS, [p])
            posts.append(p)
            time.sleep(0.002)

        post(s1, "a", "agent-x", "I told ops.", ["agent-x"])                    # before agent-x's receipt: flagged
        time.sleep(0.002)
        recs.append(c.write_receipt({"scope": s1, "sender": "agent-x", "subject": "split refund"}))
        post(s1, "b", "agent-x", "I told ops.", ["agent-x"])                    # after it: backed
        post(s1, "c", "agent-z", "agent-x and agent-y told ops.", ["agent-x", "agent-y"])   # agent-y: flagged
        post(s1, "d", "agent-z", "Ops has been told.", [])                      # a receipt exists before: backed
        post(s1, "e", "agent-z", "Reviewing.", None)                            # no claim
        raw = {"_timestamp": int(time.time() * 1e6), "scope": s2, "post_id": "f", "author": "agent-q",
               "text": "ops knows", "claim_present": 1}                         # reporter fields missing: unattributed
        c.ingest(POSTS, [raw])
        posts.append(raw)
        post(s2, "g", "agent-q", "agent-p told ops.", ["agent-p"])               # no receipt in s2: flagged
        self.wait_posts(c, s1, 5)
        self.wait_posts(c, s2, 2)
        rows = c.search(rc.monitor_sql(POSTS, RECEIPTS), int((t_start - 60) * 1e6), int((time.time() + 60) * 1e6))
        got = {(r["scope"], r["post_id"]) for r in rows if r["scope"] in (s1, s2)}
        want = {(p["scope"], p["post_id"]) for p in posts if p["post_id"] in reference_flags(posts, recs)}
        self.assertEqual({(s1, "a"), (s1, "c"), (s2, "f"), (s2, "g")}, want)
        self.assertEqual(want, got)

    def test_cli_example_and_installer(self) -> None:
        scope = new_scope("cli")
        p = self.run_kit("receipts.py", "--stream", RECEIPTS, "write", "--scope", scope, "--sender", "agent-a",
                         "--subject", "Refund split")
        self.assertEqual(0, p.returncode, p.stderr[-300:])
        line = p.stdout.strip()
        self.assertRegex(line, r"^E-[0-9A-F]{10} \| \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC \| agent-a \| Refund split$")
        p = self.run_kit("receipts.py", "--stream", RECEIPTS, "lookup", "--scope", scope, "--entry-id", line[:12])
        self.assertEqual(line, p.stdout.strip())
        p = self.run_kit("receipts.py", "--stream", RECEIPTS, "lookup", "--scope", scope, "--entry-id", FORGED)
        self.assertEqual(rc.NO_MATCH, p.stdout.strip())

        p = self.run_kit("example.py", "--prefix", PREFIX)
        self.assertEqual(0, p.returncode, p.stderr[-300:])
        self.assertEqual(2, p.stdout.count(f"-> {rc.NO_MATCH}"))
        self.assertIn("2 of 5 claim posts flagged", p.stdout)
        self.assertEqual(["p2 agent-b", "p3 agent-e"], re.findall(r"FLAGGED (p\d \S+):", p.stdout))

        hook = os.environ.get("KIT_TEST_WEBHOOK_URL", "http://127.0.0.1:9/kit-test")
        self.addCleanup(self.run_kit, "install_monitor.py", "--remove", "--prefix", PREFIX)
        p = self.run_kit("install_monitor.py", "--webhook-url", hook, "--prefix", PREFIX, "--disabled")
        self.assertEqual(0, p.returncode, p.stderr[-300:])
        self.assertIn("stored SQL is monitor.sql: yes", p.stdout)
        self.assertNotIn(hook, p.stdout)                       # the webhook URL is never printed
        c = live_client(self.env)
        _, _, _, dash = im.load(PREFIX)
        now = time.time()
        for panel in dash["tabs"][0]["panels"]:                # every panel's query runs
            c.search(panel["queries"][0]["query"], int((now - 3600) * 1e6), int((now + 60) * 1e6))
        p = self.run_kit("install_monitor.py", "--remove", "--prefix", PREFIX)
        self.assertIn("alert kit_test_claim_without_receipt", p.stdout)

    @unittest.skipUnless(HAVE_MCP, "the mcp package is not installed")
    def test_mcp_server_over_stdio(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        scope = new_scope("mcp")
        params = StdioServerParameters(command=sys.executable, args=["-I", str(KIT / "mcp_server.py"), "--scope",
                                       scope, "--sender", "agent-q", "--stream", RECEIPTS], env=dict(self.env))

        async def go(errlog):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    names = sorted(t.name for t in (await s.list_tools()).tools)
                    sent = await s.call_tool("log_escalation", {"subject": "Refund split", "message": "m"})
                    eid = re.search(r"E-[0-9A-F]{10}", sent.content[0].text).group(0)
                    found = await s.call_tool("lookup_escalation", {"entry_id": eid})
                    forged = await s.call_tool("lookup_escalation", {"entry_id": FORGED})
                    return names, sent, found, forged, eid

        with tempfile.TemporaryFile("w+") as errlog:
            names, sent, found, forged, eid = asyncio.run(asyncio.wait_for(go(errlog), 60))
            errlog.seek(0)
            self.assertFalse(self.leaks(errlog.read()), "server stderr holds a credential")
        self.assertEqual(["log_escalation", "lookup_escalation"], names)
        self.assertEqual(f"Sent. Logged as {eid}.", sent.content[0].text)
        self.assertRegex(found.content[0].text, rf"^{eid} \| .+ UTC \| agent-q \| Refund split$")
        self.assertEqual((False, rc.NO_MATCH), (forged.isError, forged.content[0].text))
        self.assertEqual(1, len(live_client(self.env, scope=scope).lookup()))


if __name__ == "__main__":
    unittest.main()
