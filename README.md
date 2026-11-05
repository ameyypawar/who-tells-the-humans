# Pre-registration (Apart Research Swarm Alignment Hackathon 2026)

This repository holds a pre-registration written before the sprint. It is frozen as `prereg/PREREG.md` v1 (2026-10-30) and marked by the git tag `prereg-v1`.

All code and results will be added during the sprint (Nov 6–8, 2026). Withheld stimuli are pre-registered by SHA-256 only (PREREG §23) and are not in this repository.

## Sprint log

- **T0 (sprint start): Thu 2026-11-05 21:26:10 IST (15:56:10 UTC)**, earlier than the originally planned Sat Nov 7 03:00 IST kickoff.
- Permission: Apart Research staff on the hackathon Discord (#help-desk, Nov 5) said: "You can start today 🙂 Friday has already started in some parts of the world".
- Everything before T0 was preparation and is disclosed in the report: research notes, the pre-registration, prompt drafts, references, a toolchain rehearsal on an unrelated toy task (its code was discarded), and toolchain probes.

## Running the harness

Setup (once per checkout): `scripts/install_pth.sh` (makes `import wtth` work under `python -I`). All commands use
`/Users/amey/.local/wtth/venv/bin/python -I -m wtth …`, written `wtth …` below. Run live phases from a plain terminal,
not from inside Claude Code: the orchestrator refuses to start if `ANTHROPIC_API_KEY`, `CLAUDECODE` or
`CLAUDE_CODE_ENTRYPOINT` is set. It reads the key file itself.

- `wtth init-db` creates `runs/wtth.sqlite` (also done by `run`).
- `wtth manifest --phase core --verify` prints the startup problems (none = ready) and the run manifest.
- `wtth render --cell NONE --seed 1 --round 2 --slot 1` prints a prompt (refuses withheld-text seeds without `--unmask`).
- `wtth run --phase <p> [--level L] --live` runs a phase; `scripts/run_phase.sh <p> [args]` does the same inside
  tmux + `caffeinate`. Phases: `g0dry g0 g1 g1b g2 g2fix core sonnet mvp pivotA pivotB backup`.
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
