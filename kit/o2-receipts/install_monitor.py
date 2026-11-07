"""Install the claim-without-receipt monitor in OpenObserve: the alert of alert.json, with its message template and
webhook destination, and the dashboard of dashboard.json. It uses the O2_* environment, like receipts.py.

    python3 install_monitor.py --webhook-url URL [--prefix P] [--disabled]
    python3 install_monitor.py --remove [--prefix P]

Running it again replaces the four objects. --prefix P reads the streams P+board_posts and P+escalation_receipts and
puts P in the object names (a second deployment, a demo or a test). --disabled creates the alert switched off;
OpenObserve evaluates an enabled alert as soon as it is created. The webhook URL is never printed (it may hold a
token). A webhook on the same machine as OpenObserve needs the server setting ZO_SSRF_ALLOW_LOOPBACK=true.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import receipts as rc  # noqa: E402


def load(prefix: str = "") -> tuple[dict, dict, dict, dict]:
    """(template, destination, alert, dashboard) from alert.json and dashboard.json, for the prefixed streams."""
    bundle = json.loads((HERE / "alert.json").read_text(encoding="utf-8"))
    dash = json.loads((HERE / "dashboard.json").read_text(encoding="utf-8"))

    def rn(text: str) -> str:
        return rc.rename_streams(text, prefix + rc.POSTS_STREAM, prefix + rc.RECEIPTS_STREAM)

    tpl, dest, alert = bundle["template"], bundle["destination"], bundle["alert"]
    tpl["name"] = prefix + tpl["name"]
    dest["name"], dest["template"] = prefix + dest["name"], tpl["name"]
    alert["name"], alert["destinations"] = prefix + alert["name"], [dest["name"]]
    alert["stream_name"] = rn(alert["stream_name"])
    alert["query_condition"]["sql"] = rn(alert["query_condition"]["sql"])
    if prefix:
        dash["title"] = f"{dash['title']} ({prefix})"
    for tab in dash["tabs"]:
        for p in tab["panels"]:
            for q in p["queries"]:
                q["query"] = rn(q["query"])
                q["fields"]["stream"] = rn(q["fields"]["stream"])
    return tpl, dest, alert, dash


def _exists(c: rc.Receipts, path: str) -> bool:
    try:
        c.request("GET", path)
        return True
    except rc.ReceiptError as exc:
        if exc.kind in ("http_400", "http_404"):
            return False
        raise


def find_alert(c: rc.Receipts, name: str) -> str | None:
    for a in (c.request("GET", f"/api/v2/{c.org}/alerts") or {}).get("list", []):
        if a.get("name") == name:
            return a.get("alert_id") or a.get("id")
    return None


def find_dashboards(c: rc.Receipts, title: str) -> list[str]:
    return [d["dashboard_id"] for d in (c.request("GET", f"/api/{c.org}/dashboards") or {}).get("dashboards", [])
            if d.get("title") == title and d.get("dashboard_id")]


def install(c: rc.Receipts, webhook_url: str, prefix: str = "", enabled: bool = True) -> dict[str, Any]:
    tpl, dest, alert, dash = load(prefix)
    for kind, body in (("templates", tpl), ("destinations", {**dest, "url": webhook_url})):
        if _exists(c, f"/api/{c.org}/alerts/{kind}/{body['name']}"):
            c.request("PUT", f"/api/{c.org}/alerts/{kind}/{body['name']}", body)
        else:
            c.request("POST", f"/api/{c.org}/alerts/{kind}", body)
    old = find_alert(c, alert["name"])
    if old:
        c.request("DELETE", f"/api/v2/{c.org}/alerts/{old}")
    alert.update(org_id=c.org, enabled=enabled)
    alert_id = c.request("POST", f"/api/v2/{c.org}/alerts", alert)["id"]
    stored = c.request("GET", f"/api/v2/{c.org}/alerts/{alert_id}")["query_condition"]["sql"]
    for d in find_dashboards(c, dash["title"]):
        c.request("DELETE", f"/api/{c.org}/dashboards/{d}", query={"folder": "default"})
    res = c.request("POST", f"/api/{c.org}/dashboards", dash, query={"folder": "default"})
    dash_id = (res.get("v8") or res)["dashboardId"]
    return {"template": tpl["name"], "destination": dest["name"], "alert": alert["name"], "alert_id": alert_id,
            "alert_sql_stored": stored.strip() == alert["query_condition"]["sql"].strip(),
            "dashboard": dash["title"], "dashboard_id": dash_id,
            "dashboard_url": (f"{c.base}/web/dashboards/view?org_identifier={c.org}&dashboard={dash_id}"
                              "&folder=default")}


def remove(c: rc.Receipts, prefix: str = "") -> list[str]:
    tpl, dest, alert, dash = load(prefix)
    gone = []
    aid = find_alert(c, alert["name"])
    if aid:
        c.request("DELETE", f"/api/v2/{c.org}/alerts/{aid}")
        gone.append(f"alert {alert['name']}")
    for d in find_dashboards(c, dash["title"]):
        c.request("DELETE", f"/api/{c.org}/dashboards/{d}", query={"folder": "default"})
        gone.append(f"dashboard {dash['title']!r}")
    for kind, name in (("destinations", dest["name"]), ("templates", tpl["name"])):   # destination first
        if _exists(c, f"/api/{c.org}/alerts/{kind}/{name}"):
            c.request("DELETE", f"/api/{c.org}/alerts/{kind}/{name}")
            gone.append(f"{kind[:-1]} {name}")
    return gone


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="install_monitor.py", description="Install (or remove) the claim-without-"
                                 "receipt alert and the dashboard in OpenObserve (O2_* environment).")
    ap.add_argument("--webhook-url", help="where the alert posts its notification (http or https)")
    ap.add_argument("--prefix", default="", help="stream and object name prefix, e.g. kit_demo_")
    ap.add_argument("--disabled", action="store_true", help="create the alert switched off")
    ap.add_argument("--remove", action="store_true", help="delete the alert, dashboard, destination and template")
    a = ap.parse_args(argv)
    if a.prefix and not rc._STREAM_RE.fullmatch(a.prefix + rc.POSTS_STREAM):
        ap.error("--prefix: lower case letters, digits and _, starting with a letter")
    if not a.remove and urllib.parse.urlparse(a.webhook_url or "").scheme not in ("http", "https"):
        ap.error("--webhook-url (http or https) is required")
    try:
        c = rc.Receipts()
        if a.remove:
            gone = remove(c, a.prefix)
            print("removed: " + (", ".join(gone) if gone else "nothing to remove"))
            return 0
        out = install(c, a.webhook_url, a.prefix, enabled=not a.disabled)
    except rc.ReceiptError as exc:
        print(f"PROBLEM: {exc}{': ' + exc.detail if exc.detail else ''}", file=sys.stderr)
        return 2
    print(f"template {out['template']}, destination {out['destination']} (webhook set)")
    print(f"alert {out['alert']}: id {out['alert_id']}, {'disabled' if a.disabled else 'enabled'}; "
          f"stored SQL is monitor.sql: {'yes' if out['alert_sql_stored'] else 'NO'}")
    print(f"dashboard {out['dashboard']!r}: {out['dashboard_url']}")
    return 0 if out["alert_sql_stored"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
