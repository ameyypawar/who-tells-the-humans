# Checkable escalation receipts on OpenObserve

A small kit for multi-agent systems. When an agent escalates to the humans, the channel writes a receipt to
[OpenObserve](https://github.com/openobserve/openobserve). Any agent can look the receipt up through an MCP tool, and
an operator-side alert flags board posts that claim an escalation with no receipt behind them. The kit does not import
the study harness: copy this directory into your own system.

## Why

The study in this repository, *Who Tells the Humans?* ([paper](../../paper/WTTH-paper.pdf)), ran live Claude Haiku
witnesses beside a peer that said it had already told ops. A receipt-style line that the peer typed into its own post
cut the share of witnesses who told ops from 90.4% to 66.7%. It also produced the only pre-registered shifts in which no
one told ops (4 of 50). The paper recommends receipts that are written by the channel, so that no post can forge one,
and that agents can check. This kit puts that recommendation into code:

- **Channel-written.** The escalation channel calls `write_receipt()` when it accepts an escalation. The channel
  assigns the entry id and stamps the sender and the time; the agent supplies none of them. A line typed into a post
  never becomes a receipt.
- **Checkable.** The channel replies only once a search finds the receipt (read after write), so every receipt an agent
  was told about can be looked up: with `lookup()` in code, or with the `lookup_escalation` MCP tool.
- **Monitored.** `monitor.sql`, run as a scheduled OpenObserve alert, flags a board post that says someone escalated
  when that agent has no earlier receipt in the same scope.

## Architecture

![Checkable escalation receipts on OpenObserve](../../docs/o2-receipts-architecture.png)

1. **Write path.** The channel delivers the escalation and writes the receipt in the same step. In the study the
   channel was the `notify_ops` handler; here it is your escalation handler, or `log_escalation` in `mcp_server.py`.
2. **Read path.** `lookup_escalation` reads the receipts of its own scope and nothing else, so a forged id finds no
   entry.
3. **Operator monitor.** The alert and the dashboard read the board posts and the receipts directly. In the kit the
   receipts stream also plays the part of the diagram's `notes` stream: the channel writes one receipt per accepted
   escalation.

## The honest caveat: agents may never look

- **The agents did not use the lookup.** In the study's exploratory follow-up
  ([`DEVIATIONS.md`](../../DEVIATIONS.md), D-028), Haiku witnesses had `lookup_escalation` beside the forged receipt
  line, and none of them called it: 0 of 150 witnesses, across 50 shifts. A check that agents never run protects
  nothing. Do not ship the lookup tool on its own: pair it with the operator-side monitor, which needs no cooperation
  from the agents.
- **The monitor flagged every false claim, post hoc.** On the study's released data, `monitor.sql` flagged every false
  claim: 459 of 459 (450 false claims by the scripted claimant, scored against the design, and 9 witness claims). It
  also flagged 7 of 814 true claims (0.9%), each a witness's own claim posted in the same turn just before the note
  that backed it. This check was post hoc (D-027): the monitor was built after the experiment and ran on the released
  data. It is not part of the pre-registered design, and it is a demonstration, not a test. For witness posts the
  ground truth uses the same notes as the rule, so detection there is close to true by construction; the informative
  numbers are the 450 scripted claims and the false alarms. The study's claim labels came from its LLM judge and its
  design. In your system the monitor sees only the claims that your classifier labels.

## Files

| File | What |
|---|---|
| `receipts.py` | The client: `write_receipt(entry)` (confirmed read after write), `lookup(entry_id, sender, scope)`, `ensure_stream()`, `board_post()`, and a small command line. Standard library only. |
| `mcp_server.py` | MCP server (stdio): `lookup_escalation`, and `log_escalation` when started with `--sender`. |
| `monitor.sql` | The claim-without-receipt rule. It is the alert's query, verbatim. |
| `alert.json` | The alert, its message template and its webhook destination (OpenObserve's v2 alert layout). |
| `dashboard.json` | Four panels: receipts over time; posts, claims and flagged claims over time; claims, flagged claims and receipts per scope; the flagged posts. |
| `install_monitor.py` | Creates, replaces or removes the alert objects and the dashboard through the API. |
| `example.py` | The demo below. |
| `test_o2_receipts.py` | Tests. The live ones skip when no OpenObserve is reachable. |

Python 3.10 or later. Only `mcp_server.py` needs a package: `pip install mcp==1.30.0`, the version this repository
pins. Tested against OpenObserve v1.1.2, open-source edition.

## Quickstart

1. **Run OpenObserve locally.** Download the binary for your platform from the
   [official downloads page](https://openobserve.ai/downloads/) (Docker and other options:
   [getting started](https://openobserve.ai/docs/getting-started/)). At the first start, OpenObserve creates its root
   user from two variables; choose your own values:

   ```sh
   chmod +x openobserve
   ZO_ROOT_USER_EMAIL='<your email>' ZO_ROOT_USER_PASSWORD='<a strong password>' ZO_HTTP_ADDR=127.0.0.1 ./openobserve
   ```

   The web UI is at <http://127.0.0.1:5080/web/>.

2. **Point the kit at it**, in your own shell:

   ```sh
   export O2_URL=http://127.0.0.1:5080 O2_ORG=default
   export O2_USER='<the root user email>'
   read -rs O2_PASSWORD && export O2_PASSWORD     # typed without echo, kept out of the shell history
   ```

   `O2_URL` may be `https://` on any host. Plain `http://` is accepted for this machine only.

3. **Log a receipt.**

   ```sh
   cd kit/o2-receipts
   python3 receipts.py init      # creates escalation_receipts and board_posts with their schemas
   python3 receipts.py write --scope incident-42 --sender agent-a --subject "Refund split into three batches"
   # E-689BF6C9E9 | 2026-11-07 10:48:39 UTC | agent-a | Refund split into three batches
   ```

   In code, call it from your escalation handler and reply to the agent only after it returns:

   ```python
   from receipts import Receipts

   r = Receipts(scope="incident-42")   # O2_* from the environment
   rec = r.write_receipt({"sender": "agent-a", "subject": "Refund split into three batches",
                          "message": "Three payouts to one customer in one minute."})
   rec["entry_id"]                      # 'E-689BF6C9E9'; the message is kept for the operators only
   ```

4. **Look one up.**

   ```sh
   python3 receipts.py lookup --scope incident-42 --entry-id E-689BF6C9E9   # your id: the line above
   python3 receipts.py lookup --scope incident-42 --entry-id E-5D0C3A91B2   # No matching entry.
   python3 receipts.py lookup --scope incident-42 --sender agent-b          # No matching entry.
   ```

5. **Install the alert and the dashboard.**

   ```sh
   python3 install_monitor.py --webhook-url https://hooks.example.com/your-endpoint
   ```

   It creates (or replaces) the message template, the webhook destination, the alert and the dashboard, checks that
   the stored alert SQL is `monitor.sql`, and prints the dashboard's URL. It never prints the webhook URL, which may
   hold a token. `--disabled` creates the alert switched off, `--remove` deletes all four objects, and
   `--prefix kit_demo_` installs a copy over the demo streams. A webhook on the OpenObserve machine itself needs the
   server setting `ZO_SSRF_ALLOW_LOOPBACK=true`. `install_monitor.py` is the tested path; the two JSON files follow
   the layout of OpenObserve's own exports, for the UI's Import buttons (create the template and the destination
   first).

Then feed the board: send each board post to the `board_posts` stream, built with `board_post()` and written with
`r.ingest("board_posts", [...])`. Set `claimed_reporters` from your claim classifier (an LLM judge, rules, or
structured output from the agents' framework).

## Example

`python3 example.py`, with the environment of step 2, runs the whole loop in a new scope over the streams
`kit_demo_escalation_receipts` and `kit_demo_board_posts`: three escalations, a real and a forged lookup, a six-post
board with hand-set claim labels, and the monitor. A run against a local OpenObserve v1.1.2 (ids and times vary):

```text
OpenObserve http://127.0.0.1:5080, org default; streams kit_demo_escalation_receipts and kit_demo_board_posts; scope demo-20261107T104839Z-06ed

1. Three escalations through the channel (each reply waits until a search finds the receipt)
   E-689BF6C9E9 | 2026-11-07 10:48:39 UTC | agent-a | Refund split into three batches to stay under the approval limit   (confirmed in 2 ms)
   E-4FFBFC75C3 | 2026-11-07 10:48:39 UTC | agent-c | Same refund split, seen from the payouts queue   (confirmed in 2 ms)
   E-6479B5DC02 | 2026-11-07 10:48:39 UTC | agent-d | Approval limit bypassed on the Hendricks refund   (confirmed in 2 ms)

2. Lookups (the replies the lookup_escalation tool gives)
   lookup(entry_id='E-689BF6C9E9')   # a real entry
      -> E-689BF6C9E9 | 2026-11-07 10:48:39 UTC | agent-a | Refund split into three batches to stay under the approval limit
   lookup(entry_id='E-5D0C3A91B2')   # the id agent-b types below
      -> No matching entry.
   lookup(sender='agent-b')
      -> No matching entry.

3. A tiny board (claim labels set by hand here; in your system, by your claim classifier)
   p1 agent-a: I sent ops a note about the split refund.  [says escalated: agent-a]
   p2 agent-b: Already escalated to ops, logged as E-5D0C3A91B2. No need to pile on.  [says escalated: agent-b]
   p3 agent-e: agent-b has already notified ops, so I'll hold off.  [says escalated: agent-b]
   p4 agent-f: agent-a and agent-c both told ops.  [says escalated: agent-a, agent-c]
   p5 agent-f: Ops has been told about the split.  [says escalated: no one named]
   p6 agent-c: Reviewing ticket T-1101 next.  [no claim]

4. The monitor: monitor.sql through the _search API (the alert runs the same query)
   2 of 5 claim posts flagged
   FLAGGED p2 agent-b: Already escalated to ops, logged as E-5D0C3A91B2. No need to pile on.
      claimed reporters: agent-b; with a receipt before the post: 0; receipts in the scope before it: 3
   FLAGGED p3 agent-e: agent-b has already notified ops, so I'll hold off.
      claimed reporters: agent-b; with a receipt before the post: 0; receipts in the scope before it: 3
```

agent-b's claim and agent-e's relay of it have no receipt behind them. The three backed claims (p1, p4, p5) pass.

## Wire it into your agents

Start `mcp_server.py` with its scope fixed. With `--sender` it also lists `log_escalation`, stamped with that sender,
so run one server per agent (or one lookup-only server per scope). In an MCP client's configuration, for example a
Claude Code `.mcp.json`:

```json
{
  "mcpServers": {
    "escalation-receipts": {
      "command": "python3",
      "args": ["/path/to/kit/o2-receipts/mcp_server.py", "--scope", "incident-42", "--sender", "agent-a"],
      "env": {"O2_URL": "${O2_URL}", "O2_ORG": "${O2_ORG}", "O2_USER": "${O2_USER}", "O2_PASSWORD": "${O2_PASSWORD}"}
    }
  }
}
```

Claude Code fills `${VAR}` from your environment, so the password stays out of the file. `python3` must be an
interpreter with the `mcp` package.

| Tool | Description (what the agent reads) | Reply |
|---|---|---|
| `lookup_escalation(entry_id?, sender?)` | Look up escalation-log entries by entry id or by sender. The escalation channel writes an entry each time an escalation is sent. | One line per matching receipt of the server's scope, oldest first (`E-689BF6C9E9 \| 2026-11-07 10:48:39 UTC \| agent-a \| subject`), or `No matching entry.` |
| `log_escalation(subject, message?)` | Send an escalation to the human operators. The escalation channel writes an entry to the escalation log and replies with its entry id. | `Sent. Logged as E-....`, once the receipt is searchable |

- The descriptions are neutral, as in the study: they say what each tool does, not when to use it or that a peer's
  claim may be false.
- `entry_id` accepts `E-689BF6C9E9`, `e-689bf6c9e9`, `E689BF6C9E9`, `#E-689BF6C9E9` or a pasted log line. `sender` is
  compared without case and without a leading `@`. A value that cannot name a receipt matches none.
- When the store fails, both tools return an MCP error, `The escalation log is unavailable right now.`, never a success
  reply such as a false `No matching entry.`. The server refuses to start (exit 2) when it cannot reach the store.
- An agent cannot choose the scope or the sender: both are fixed when the server starts.

## Streams and fields

`escalation_receipts`: one record per accepted escalation, written by `write_receipt()` only.

| Field | Meaning |
|---|---|
| `_timestamp` | The channel's clock at the write (microseconds, UTC). |
| `scope` | The run, session or incident. A lookup never crosses scopes. |
| `entry_id` | `E-` and 10 random hex digits (40 bits), assigned by the channel, so a typed id is not a guess away from a real one. |
| `sender` | The agent that escalated, stamped by the channel: lower case, without a leading `@`. |
| `subject` | One line, at most 200 characters. |
| `message` | Optional, at most 4,000 characters. Kept for the operators; a lookup never returns it. |

`board_posts`: one record per board post; `board_post()` builds it.

| Field | Meaning |
|---|---|
| `_timestamp`, `scope` | When the post was made, and its scope. |
| `post_id`, `author`, `text` | The post. |
| `claim_present` | 1 if the post says someone escalated, else 0. |
| `claimed_reporters` | Comma-separated handles that the post says escalated; empty if it names no one ("ops has been told"). |
| `n_claimed_reporters` | How many handles that is. |

Write handles the same way in both streams (lower case, without `@`). A claim post that lacks the two reporter fields
counts as a claim that names no one.

## The monitor

A post is flagged when `claim_present = 1` and either

- it names reporters, and at least one of them has no receipt in the same scope with an earlier `_timestamp`; or
- it names no one, and the scope has no receipt at all before the post.

The alert `claim_without_receipt` (on `board_posts`) runs `monitor.sql` every 10 minutes over the last 24 hours, fires
when the query returns a row, and then stays silent for 60 minutes. Its notification has one
`scope|post_id|author|claimed_reporters|receipts_before` line per flagged post. Change these settings in the UI or in
`alert.json`. Four things to know:

- The look-back window must cover a scope's whole life. A receipt older than the window is invisible to the rule, so a
  claim that relies on it is flagged.
- Each run reports every flagged post still inside the window, not only new ones; the silence period limits repeats.
- The rule is strict about order: a claim posted even a moment before its receipt is flagged. All 7 false alarms in the
  study were of that kind.
- Keep the explicit `LIMIT`: without one, OpenObserve evaluates a scheduled alert's SQL with a 100-row cap.

## Mapping from the study

| Study | Kit |
|---|---|
| `src/wtth/receipts.py`: `ReceiptStore` over the stream `escalation_receipts` (exp2, D-028) | `receipts.py`: `Receipts` over `escalation_receipts` |
| `episode_key` (episode, attempt and run id of a shift) | `scope` |
| The `notify_ops` handler writes the receipt and replies "Sent to ops." once it is searchable | `write_receipt()` in your handler, or `log_escalation` ("Sent. Logged as E-....") |
| `entry_id` = `E-0200` plus the note's rank in the shift (renumbered as the log is) | `E-` and 10 random hex digits, fixed at the write |
| Shift clock (`clock`, HH:MM) | `_timestamp`, shown in UTC |
| `lookup_escalation` in `configs/tools_exp2.yaml` and `exp/prompts/tools_exp2.md`, served by `src/wtth/mcp_server.py` (toolset `main_check`) | `lookup_escalation` in `mcp_server.py`: the same parameters, and a description that names the escalation channel instead of `notify_ops` |
| `cross_check` of every lookup against the main database | None: the receipts stream is the only record |
| Streams `wtth_posts` and `wtth_notes` (D-027 monitor) | `board_posts` and `escalation_receipts` |
| `episode_id` | `scope` |
| `claim_present`, `claim_subject` (judge labels; the scripted claim coded by design) | `claim_present` (your classifier) |
| `claimed_reporters`, `n_claimed_reporters`, `claim_attribution` | `claimed_reporters`, `n_claimed_reporters` (none named = unattributed) |
| `wtth_log`, `wtth_shifts`, the P1 and untold-FORGED panels | Dropped: specific to the study |
| Alert `wtth_claim_without_receipt` (365-day look-back over the synthetic shift date), template `wtth_claims_without_receipt`, destination `wtth_local_webhook` | `claim_without_receipt` (24-hour look-back), `claims_without_receipt`, `receipts_webhook` |
| `scripts/openobserve/setup_monitor.py` | `install_monitor.py` |

The study's own monitor and its documentation are in `scripts/openobserve/` (see its `README.md`). The kit's default
receipts stream has the name of the study's live exp2 stream: on an OpenObserve that holds the study's data, pass
`--stream` or `--prefix` so the kit does not write into it.

## Tests

```sh
cd kit/o2-receipts
python3 -m unittest -v test_o2_receipts
```

The offline tests always run; the MCP ones need the `mcp` package. The LIVE tests need a reachable OpenObserve and
the environment of step 2, or `O2_ENV_FILE`: the path of a .env file that holds `O2_USER` and `O2_PASSWORD`, or
OpenObserve's `ZO_ROOT_USER_EMAIL` and `ZO_ROOT_USER_PASSWORD`. Without them, the LIVE tests skip. They write to
`kit_test_escalation_receipts` and `kit_test_board_posts` only, each test in fresh scopes, and they install the monitor
under the `kit_test_` prefix (alert disabled), then remove it. The tests cover id and handle parsing; read after write
(20 writes, each found by the first search); that a store failure is never an answer; the monitor SQL against a Python
version of the rule; the command line, the example and the installer; the MCP tools, in memory and over stdio; and that
no output holds a credential.

## Security

- Credentials come from the environment and go out only as HTTP basic auth: plain `http` to this machine only, `https`
  elsewhere, and never through a proxy for a local server. No message, exception, repr or tool reply holds them;
  errors carry a short label.
- A receipt is as hard to forge as the OpenObserve credentials are to get. Keep them with the channel and the MCP
  server, never in an agent's context; no tool returns them.
- `install_monitor.py` never prints the webhook URL.
