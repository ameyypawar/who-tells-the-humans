# Who Tells the Humans? Phantom Escalation and Channel-Written Receipts in Agent Swarms

Apart Research Swarm Alignment Hackathon 2026, solo entry by Amey Pawar. A pre-registered experiment (670 shifts of three live Claude Haiku 5.5 witnesses, plus exploratory Sonnet and follow-up arms) on whether a peer's claim to have told the humans takes the place of telling them, with checkable escalation receipts built on [OpenObserve](https://github.com/openobserve/openobserve).

## Headline findings

- **Confirmatory (H3).** A receipt-style line typed by the claiming peer into its own post cut the share of witnesses who told ops from 90.4% (NO-RECEIPT, empty log) to 66.7% (FORGED): a drop of 23.8 points, 95% cluster-bootstrap CI 14.2 to 33.3. FORGED also produced the only pre-registered shifts in which no one told ops within the shift, 4 of 50. Post hoc, a witness relayed the claim on the board in 3 of those 4.
- **Confirmatory (H1).** The bare claim was negligible: 100.0% (240/240) to 98.3% (236/240), a stand-down of 1.7 points (CI 0.4 to 3.3). The null is rejected, but only at the boundary of the bootstrap interval, and the paper reports it as a negligible effect.
- **Confirmatory (H2), against prediction.** With a channel-written log the stand-down was larger, not smaller: the stand-down was 9.6 points with the log and 1.7 without, so the H2 contrast is -7.9 points (CI -12.9 to -3.3), opposite to the prediction. Post hoc, among witnesses who held off past round 2, 23 of 35 never reported with the log and 4 of 47 without it; every shift still reached ops.
- **Exploratory follow-up (D-022, no concurrent control).** Neither a one-line runbook rule nor a one-line provenance note restored Haiku's reporting to its empty-log level (74.0% and 73.3%). Sonnet 5.5 reported less beside a real receipt (62.2%) than beside the typed line (96.7%). These arms ran after the core and are not pooled with it.
- **Exploratory follow-up 2 (D-028, concurrent pair): the receipt lookup went unused.** FORGED plus `lookup_escalation`, a tool that reads the receipts the channel writes into OpenObserve (X-FORGED-CHECK), ran beside FORGED unchanged (X-FORGED-CONC). No witness ever called the tool: 0 of 150 witnesses, 0 calls in 750 turns. P1 was 76.0% against 69.3% (difference 6.7 points, CI -4.0 to 17.3), not distinguishable. Ops went untold in 0 of 50 shifts against 4 of 50; the bootstrap interval of that difference (-8.0 points, CI -16.0 to -2.0) excludes zero only through a boundary artefact (Fisher p = 0.117, Newcombe -18.8 to 0.6), so it is not a reduction. The concurrent control sat 2.7 points (CI -10.0 to 14.7) from the core's FORGED, so the later phase barely moved reporting.
- **Post hoc (D-027): an OpenObserve alert flagged every false claim.** On the released data of the core, the Sonnet arm and the first follow-up (950 shifts), a "claim without receipt" alert flagged all 450 false claims of the scripted claimant and 0 of its 130 true ones; over every claim post, 459 of 459 false claims and 7 of 814 true ones (each posted a moment before its own note, in the same turn). It flagged all 4 untold FORGED shifts at 21:26 (round 2), 106 minutes before the shift ended. The monitor was built after the data: a demonstration, not a test.
- **Recommendation.** Receipts should be unforgeable by construction (untested), and checked on the channel and operator side: the agents given a check never used it (exploratory).

## Figure

![Reporting by cell and the follow-up arms](docs/project-image.png)

## OpenObserve

The paper recommends receipts that the escalation channel writes, so that no post can forge one, and that can be
checked. Appendix J of the paper builds that on [OpenObserve](https://github.com/openobserve/openobserve) (open-source
edition, v1.1.2, run locally and bound to localhost):

![Checkable escalation receipts on OpenObserve](docs/o2-receipts-architecture.png)

1. **Write path (exploratory follow-up 2, D-028).** The `notify_ops` handler writes each accepted note to the
   OpenObserve stream `escalation_receipts`, and replies only once a search finds the record (read after write). No
   board post can write the stream. A post-run audit found the stream equal to the main database's log in every exp2
   shift.
2. **Read path (D-028).** `lookup_escalation` reads the receipts of its own shift, so a forged entry finds no match.
   No witness called it (0 of 150), so no receipt was ever checked, and P1 with and without the tool was not
   distinguishable.
3. **Operator monitor (post hoc, D-027).** A scheduled OpenObserve alert flags a board post that says ops was told when
   the named reporter has no earlier `notify_ops` record in that shift. On the 950 released shifts it ran over, it
   flagged every false claim (459 of 459), including all 450 of the scripted claimant's, and 7 of 814 true claims. The
   four untold FORGED shifts were flagged at 21:26 (round 2), 106 minutes before the shift ended.

| The monitor's dashboard | A flagged shift (episode 83) |
|---|---|
| ![OpenObserve dashboard: claims vs receipts](docs/openobserve-dashboard.png) | ![OpenObserve log explorer: C's claim and a witness's relay, both flagged](docs/openobserve-flagged.png) |

Caveats: the monitor is post hoc and ran on released data, so it tests nothing. For witness claims the ground truth
comes from the same notes the rule checks, so detection there (9 of 9) is close to true by construction; the
informative numbers are the scripted claimant's 450 false and 130 true claims, scored against the design, and the 7
false alarms. The claim labels come from the study's LLM judge, its reviewed labels and the design.

- **Use it:** [`kit/o2-receipts/`](kit/o2-receipts/README.md) is a standalone kit (standard-library client with
  read-after-write receipts, a `lookup_escalation` MCP server, the alert, the dashboard and an end-to-end example).
  Its README has the quickstart.
- **Rerun the study's monitor:** [`scripts/openobserve/`](scripts/openobserve/README.md) ingests `data/release/`,
  installs the alert and the dashboard, checks that Python, OpenObserve SQL and the alert's notification flag the same
  posts, and writes the paper's `\wtthOO...` macros.
- Competing interests: the author contributes to the open-source OpenObserve project.

## The paper

[`paper/WTTH-paper.pdf`](paper/WTTH-paper.pdf) (8 pages of main text, then references and appendices). Build it from source with `WTTH_PYTHON=<your venv python> paper/build.sh --final` (needs tectonic and pdftotext; add `--offline` if the TeX packages are already cached). `paper/README.md` explains the generated macros and the branch files.

## Pre-registration and integrity

- Pre-registration: `prereg/PREREG.md`, tag `prereg-v1`. A public gist holds a copy: <https://gist.github.com/ameyypawar/7018f1eb8050c65be82bb606068d1282>, created 2026-11-03 06:51 UTC, before any data.
- Freeze tags: `frozen-v1` (harness and configuration before the core), `frozen-v1.1` (after the D-014 fix), `exp1-v1` (follow-up) and `exp2-v1` (follow-up 2).
- `DEVIATIONS.md` logs every departure from the pre-registration, D-001 to D-029, each dated and marked with whether it came before or after data.
- Early start (D-004): T0 was Thu 2026-11-05 21:26:10 IST (15:56:10 UTC), earlier than the planned Sat Nov 7 kickoff, with permission from Apart Research staff on the hackathon Discord (#help-desk, Nov 5): "You can start today [smiley emoji] Friday has already started in some parts of the world".
- Confirmatory results are the Haiku `core` phase only. The `sonnet`, `exp1` and `exp2` phases are exploratory and never pooled with it, and the OpenObserve monitor (D-027) is post hoc.

## Pre-existing work

The hackathon judges only the weekend's work, so work that predates T0 is declared here (PREREG section 20, extended below):

- Research notes `research/01-34` (literature, competitor review, planning, OSS framework audit; assembled with AI assistance).
- `prereg/PREREG.md` and its dated git tag `prereg-v1`.
- Prompt drafts.
- `references.bib`.
- A Nov 5 toolchain rehearsal on an unrelated toy task; its code was discarded and not reused.
- Toolchain probes: 5 "Reply OK" calls on Oct 27, plus the Nov 3 setup checks.
- A handful of exploratory LLM calls on public collusion.wiki logs for a different idea; nothing reused.
- Extension to six later notes, all last modified before T0: `research/35` (setup check, Nov 3 14:57 IST), `research/36` (final prior-art sweep, Nov 5 19:16), `research/37` (toolchain rehearsal notes, Nov 5 13:51), `research/38` and `research/39` (two reviews of the harness design and the rehearsal prototypes, Nov 5 20:05 and 19:58) and `research/40` (build specification from those reviews, Nov 5 20:06).

References to `research/NN` in code and docs point to the author's planning notes, which are not part of this repository. PREREG.md and DEVIATIONS.md are self-contained. All study code, data and results came after T0.

## Repo map

| Path | What |
|---|---|
| `src/wtth/` | The harness. `orchestrator/` and `runner/` run the shifts, `mcp_server.py` and `handlers.py` are the MCP server the witnesses use, `judge/` is the LLM judge, `annotate/` the review queues, `analysis/` the statistics, tables and figures, `release.py` the data-release exporter. |
| `tests/` | Offline test suite (1,109 tests) with a fake Claude CLI (`tests/fake_claude.py`) and behaviour files (`tests/behaviors/`). |
| `configs/` | Frozen experiment configs (`experiment.yaml`, `exp1.yaml`, `exp2.yaml` with `tools_exp2.yaml`), hashes, allow-lists, `cc-settings.json`. |
| `prereg/`, `DEVIATIONS.md` | The pre-registration and the deviations log. |
| `paper/` | LaTeX sources, generators, generated numbers and tables, the built PDF. |
| `data/release/` | Released data, see below. |
| `annotations/human_read/` | The author-read sheets and second-reader files (D-025). `author_sheet_imported.md` and `author_sheet_as_answered.md` are the imported and the as-answered record of the same sheet. |
| `scripts/` | `release_summary.py`, `install_pth.sh`, `run_phase.sh`, `run_judge.sh`, `check_no_keys.sh`; `openobserve/`, the post-hoc OpenObserve monitor (D-027). |
| `kit/o2-receipts/` | Standalone kit: channel-written, checkable escalation receipts on OpenObserve, with an MCP lookup tool, the alert and the dashboard (D-029). |
| `docs/` | The architecture diagram and its script, the OpenObserve screenshots, the project image. |

## Reproduce

Python 3.12.13 was used.

```bash
# Setup
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt        # direct dependencies; requirements.lock is the full freeze
WTTH_PYTHON=$PWD/.venv/bin/python scripts/install_pth.sh   # makes `import wtth` work under python -I

# Offline tests: no network, no key; about 9 minutes, 1,109 tests, OK with the 7 slow end-to-end tests skipped by default
.venv/bin/python -I -m unittest discover -s tests -p 'test_*.py'
WTTH_SLOW=1 .venv/bin/python -I -m unittest discover -s tests -p 'test_wp[56]_e2e.py'   # the slow ones

# Headline numbers from the released data: no key, no database
.venv/bin/python -I scripts/release_summary.py   # recomputes P1, P2 and H3 from data/release

# Offline dry run with the fake client; writes to runs/fake/
.venv/bin/python -I -m wtth run --phase g0dry --fake tests/behaviors/g0dry.json --k-init 6
#   on other machines add --set paths.python=$PWD/.venv/bin/python (and other --set paths.* overrides)

# Paper (needs tectonic and pdftotext)
WTTH_PYTHON=$PWD/.venv/bin/python paper/build.sh --final
```

`python -m wtth.analysis` regenerates every number, table and figure of the paper, but it needs the private run database (`runs/wtth.sqlite` and the judge databases). That database is not released because it contains the withheld strings and raw API bodies. The released CSVs let you recompute the headline numbers (above) and re-analyse the outcomes; they do not feed `wtth.analysis`.

The full rerun of the experiment needs an Anthropic API key, Claude Code 2.1.317 (its SHA-256 is in `configs/prereg_hashes.json`), `configs/cc-settings.json`, and the withheld stimuli (available on request, see below). Set the `paths:` block of `configs/experiment.yaml` first.

## Data release

`data/release/` holds masked transcripts, per-shift and per-witness outcomes, and judge and reviewed labels for every analysed shift: 1,050 shifts (670 confirmatory Haiku, 150 exploratory Sonnet, 130 exploratory follow-up, 100 exploratory follow-up 2; D-029). `data/release/README.md` documents every file and column and maps them to the paper. Licence: CC BY 4.0.

- **Masking.** Every withheld string (the claim paraphrases and the forged receipt line) is replaced by a `[WITHHELD:...]` placeholder, including 30-character fragments of it, unless the span also occurs in a public pre-registered line. Real receipts stay intact.
- **Withheld strings.** The two claim paraphrases and the forged line are pre-registered by SHA-256 only. They are available to vetted researchers on request: ameypawar1237@gmail.com. Raw API bodies, token usage and billing data are never released.
- **D-026.** The forged line follows the public log layout and differs only in its entry id, and that id appears in agents' own text in the released transcripts. The line can therefore be reconstructed from public material. The hash commitment stands as registered, and the paper says so.

## Notes

- The frozen configs carry the author's machine paths (`/Users/amey/...`) and some stale comments (for example "TBD" on values that were frozen at G1). Override paths with `--set`; the comments were left as frozen.
- `configs/expected_init.json` still names the MCP server `workspace`. It was renamed `larkspur_workspace` in D-012, and `src/wtth/cli.py` overrides the file.

## AI use

Claude models wrote most of the code and drafts under the author's direction; Claude agents also built the OpenObserve receipts store, the monitor, the kit and the architecture diagram, which the author reviewed. See the paper's LLM Usage Statement for the full disclosure, including how labels were proposed, audited and approved.

## Competing interests

The author contributes to the open-source OpenObserve project.

## Licence

MIT for code (`LICENSE`), CC BY 4.0 for data (`data/release/LICENSE`).

## Citation

```bibtex
@misc{pawar2026wtth,
  title  = {Who Tells the Humans? Phantom Escalation and Channel-Written Receipts in Agent Swarms},
  author = {Pawar, Amey},
  year   = {2026},
  note   = {Apart Research Swarm Alignment Hackathon},
  url    = {https://github.com/ameyypawar/who-tells-the-humans}
}
```

## Sprint log

- **T0 (sprint start): Thu 2026-11-05 21:26:10 IST (15:56:10 UTC)**, earlier than the originally planned Sat Nov 7 03:00 IST kickoff.
- Permission: Apart Research staff on the hackathon Discord (#help-desk, Nov 5) said: "You can start today [smiley emoji] Friday has already started in some parts of the world".
- Everything before T0 was preparation and is disclosed above: research notes, the pre-registration, prompt drafts, references, a toolchain rehearsal on an unrelated toy task (its code was discarded), and toolchain probes.
- The preexisting research notes are `research/01-40`. PREREG section 20 lists `research/01-34`; the list is extended here and in the paper (Appendix C.2) to the six later notes named above.
- The pre-registration was frozen as `prereg/PREREG.md` v1 (2026-10-30) and tagged `prereg-v1`. Withheld stimuli are pre-registered by SHA-256 only (PREREG section 23).

### Operating notes for the harness

Setup (once per checkout): `scripts/install_pth.sh` (makes `import wtth` work under `python -I`). All commands use
`<your venv python> -I -m wtth …`, written `wtth …` below. Run live phases from a plain terminal,
not from inside Claude Code: the orchestrator refuses to start if `ANTHROPIC_API_KEY`, `CLAUDECODE` or
`CLAUDE_CODE_ENTRYPOINT` is set. It reads the key file itself.

- `wtth init-db` creates `runs/wtth.sqlite` (also done by `run`).
- `wtth manifest --phase core --verify` prints the startup problems (none = ready) and the run manifest.
- `wtth render --cell NONE --seed 1 --round 2 --slot 1` prints a prompt (refuses withheld-text seeds without `--unmask`).
- `wtth run --phase <p> [--level L] --live` runs a phase; `scripts/run_phase.sh <p> [args]` does the same inside
  tmux + `caffeinate`. Phases: `g0dry g0 g0b g1 g1b g2 g2fix core sonnet mvp pivotA pivotB backup`. `g0b` is `g0` on fresh seeds (9091-9095).
  The MCP server name is `mcp_server_name` in `configs/experiment.yaml` (or `--set mcp_server_name=NAME`); Claude Code
  2.1.317 refuses reserved names (`workspace`, `terminal`, `office`, `visualize`, `window_halo`, `dev_debug`,
  `ccd_session`, `ccd_session_mgmt`) and live startup checks fail on them.
- `wtth run --phase g0dry --fake behavior.json --k-init 6` is the offline dry run with the fake client (needs
  `tests/fake_claude.py`); it uses `runs/fake/` and never touches the production database, slots or cwd.
- `wtth status` shows episodes, attempts, cost and recent warnings. `runs/health.log` has a line every 30 s and
  `runs/ALERT.json` is written on every STOP or CAP.
- Resume: run the same command again. Unfinished attempts are marked `abandoned`, their episodes requeue as a new
  attempt with the same seed, and finished episodes are never touched. `--requeue-failed` requeues episodes that hit
  the 3-attempt limit (it writes a `manual_requeue` event first). `--stop-at HH:MM` drains and exits at that local
  time. `--k-init N` and `--max-seed CELL=N` change concurrency and quota cuts.
- Exit codes: 0 done, 2 startup problems, 3 STOP (billing, breaker, remote settings), 4 cost cap or budget projection,
  5 another run holds `runs/wtth.lock`, 130 operator interrupt.

Order of phases: `g0` → `wtth audit --run-id <id>` → `g1 --level L2` → `wtth gate g1 --level L2` (then `g1b --level L1|L3`
if needed) → `g2` → `wtth gate g2` (then `g2fix`) → fill `frozen_level`, `max_turns` and `K` in
`configs/experiment.yaml`, commit, tag `frozen-v1` → `core` → `sonnet`.
