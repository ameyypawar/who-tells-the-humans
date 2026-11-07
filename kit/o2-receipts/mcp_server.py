"""MCP server for checkable escalation receipts (the `mcp` package, stdio transport).

Tools
  lookup_escalation(entry_id?, sender?)  the receipts of this server's scope that match, one log line each, oldest
                                         first, or "No matching entry."
  log_escalation(subject, message?)      only when started with --sender: the escalation channel. It writes a receipt
                                         stamped with this server's sender and scope, waits until a search finds it
                                         and replies "Sent. Logged as E-...". The message is kept for the operators
                                         and is never returned by lookup_escalation.

The scope and the sender are fixed when the server starts and never come from a tool call, so an agent can neither
read another scope's receipts nor write one in another agent's name. Run one server per agent (with --sender) or one
lookup-only server per scope. When the store fails, a call returns an MCP error ("The escalation log is unavailable
right now."), never a success reply such as a false "No matching entry.".

    O2_URL=... O2_ORG=... O2_USER=... O2_PASSWORD=... \\
        python3 mcp_server.py --scope incident-42 [--sender agent-a] [--stream escalation_receipts]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import receipts as rc  # noqa: E402

LOOKUP_DESCRIPTION = ("Look up escalation-log entries by entry id or by sender. The escalation channel writes an "
                      "entry each time an escalation is sent.")
LOG_DESCRIPTION = ("Send an escalation to the human operators. The escalation channel writes an entry to the "
                   "escalation log and replies with its entry id.")


def _reply(text: str, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=error)


def build_server(client: Any, sender_handle: str | None = None, name: str = "escalation-receipts") -> FastMCP:
    """A FastMCP server over `client` (a receipts.Receipts with its scope set, or a stand-in with the same lookup and
    write_receipt methods). log_escalation is listed only when `sender_handle` (canonical form) is given."""
    server = FastMCP(name, log_level="WARNING")

    @server.tool(name="lookup_escalation", description=LOOKUP_DESCRIPTION)
    async def lookup_escalation(
            entry_id: Annotated[str | None, Field(description="an entry id such as E-3F9A0C2B7D")] = None,
            sender: Annotated[str | None, Field(description="an agent handle")] = None) -> CallToolResult:
        try:
            recs = await anyio.to_thread.run_sync(lambda: client.lookup(entry_id=entry_id, sender=sender))
        except rc.ReceiptError:
            return _reply(rc.UNAVAILABLE, error=True)
        return _reply(rc.lookup_reply(recs))

    if sender_handle is not None:
        @server.tool(name="log_escalation", description=LOG_DESCRIPTION)
        async def log_escalation(
                subject: Annotated[str, Field(description="a short subject line")],
                message: Annotated[str, Field(description="the escalation itself")] = "") -> CallToolResult:
            entry = {"sender": sender_handle, "subject": rc.one_line(subject)[:rc.SUBJECT_MAX]}
            if not entry["subject"]:
                return _reply("The subject is empty.", error=True)
            if isinstance(message, str) and message.strip():
                entry["message"] = message[:rc.MESSAGE_MAX]
            try:
                rec = await anyio.to_thread.run_sync(lambda: client.write_receipt(entry))
            except rc.ReceiptError:
                return _reply(rc.UNAVAILABLE, error=True)
            return _reply(f"Sent. Logged as {rec['entry_id']}.")

    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mcp_server.py", description="MCP server: lookup_escalation (and, with "
                                 "--sender, log_escalation) over an OpenObserve receipts stream. O2_URL, O2_ORG, "
                                 "O2_USER and O2_PASSWORD come from the environment.")
    ap.add_argument("--scope", required=True, help="the run, session or incident this server serves")
    ap.add_argument("--sender", help="the agent this server serves; enables log_escalation")
    ap.add_argument("--stream", default=rc.RECEIPTS_STREAM, help="receipts stream (default %(default)s)")
    ap.add_argument("--name", default="escalation-receipts", help="MCP server name (default %(default)s)")
    a = ap.parse_args(argv)
    try:
        sender = None
        if a.sender is not None:
            sender = rc.canonical_sender(a.sender)
            if sender is None:
                raise ValueError("--sender must be a handle such as agent-a")
        client = rc.Receipts(stream=a.stream, scope=a.scope)
        client.ensure_stream()   # reachable, authorised, stream present (created with its schema if missing)
    except (rc.ReceiptError, ValueError) as exc:
        print(f"mcp_server.py: {exc}", file=sys.stderr)   # a label only; stdout is the MCP channel
        return 2
    build_server(client, sender, a.name).run()   # stdio
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
