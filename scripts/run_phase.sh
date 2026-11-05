#!/bin/sh
# Start one phase in a detached tmux session under caffeinate.
#   scripts/run_phase.sh <phase> [extra `wtth run` args]     e.g. scripts/run_phase.sh g1 --level L2
# Live mode is the default. Refuses if ANTHROPIC_API_KEY or CLAUDECODE is set in this shell: the orchestrator reads
# the key file itself and must not run inside Claude Code.
set -eu
[ $# -ge 1 ] || { echo "usage: $0 <phase> [wtth run args...]" >&2; exit 2; }
if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${CLAUDECODE:-}" ] || [ -n "${CLAUDE_CODE_ENTRYPOINT:-}" ]; then
  echo "run_phase: refusing to start: ANTHROPIC_API_KEY / CLAUDECODE / CLAUDE_CODE_ENTRYPOINT is set in this environment" >&2
  exit 2
fi
R="$(cd "$(dirname "$0")/.." && pwd)"
PY="${WTTH_PYTHON:-/Users/amey/.local/wtth/venv/bin/python}"
PHASE="$1"; shift
MODE="--live"
for a in "$@"; do [ "$a" = "--fake" ] && MODE=""; done
mkdir -p "$R/runs/logs"
LOG="$R/runs/logs/${PHASE}-$(date -u +%Y%m%dT%H%M%SZ).log"
SESSION="wtth-$PHASE"
if /usr/bin/env tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "run_phase: tmux session $SESSION already exists" >&2; exit 5
fi
CMD="cd '$R' && caffeinate -dims '$PY' -I -m wtth run --phase '$PHASE' $MODE $* 2>&1 | tee -a '$LOG'"
/usr/bin/env tmux new-session -d -s "$SESSION" "$CMD"
echo "started tmux session $SESSION; log: $LOG"
echo "attach: tmux attach -t $SESSION    status: $PY -I -m wtth status"
