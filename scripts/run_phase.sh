#!/bin/bash
# Start one phase in a detached tmux session under caffeinate.
#   scripts/run_phase.sh <phase> [extra `wtth run` args]     e.g. scripts/run_phase.sh g1 --level L2
# Live mode is the default (--fake in the extra args turns it off). Works from a Claude Code shell: the orchestrator
# is started through `env -i` with only HOME, PATH, LANG and TERM, so nothing from this shell or from the tmux
# server's environment (CLAUDECODE, ANTHROPIC_*, CLAUDE_*) reaches it; its own live guard then passes.
set -eu
[ $# -ge 1 ] || { echo "usage: $0 <phase> [wtth run args...]" >&2; exit 2; }
R="$(cd "$(dirname "$0")/.." && pwd)"
PY="${WTTH_PYTHON:-/Users/amey/.local/wtth/venv/bin/python}"
PHASE="$1"; shift
MODE="--live"
for a in "$@"; do [ "$a" = "--fake" ] && MODE=""; done
mkdir -p "$R/runs/logs"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$R/runs/logs/${PHASE}-${STAMP}.log"
LAUNCH="$R/runs/logs/${PHASE}-${STAMP}.launch.sh"
SESSION="wtth-$PHASE"

# shell-quote one argument
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

{
  echo '#!/bin/bash'
  printf 'cd %s || exit 1\n' "$(q "$R")"
  printf 'env -i HOME=%s PATH=%s LANG=en_US.UTF-8 TERM=%s \\\n' "$(q "$HOME")" "$(q "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin")" "$(q "${TERM:-xterm-256color}")"
  printf '  caffeinate -dims %s -I -m wtth run --phase %s' "$(q "$PY")" "$(q "$PHASE")"
  [ -n "$MODE" ] && printf ' %s' "$MODE"
  for a in "$@"; do printf ' %s' "$(q "$a")"; done
  printf ' 2>&1 | tee -i -a %s\n' "$(q "$LOG")"
  echo 'rc=${PIPESTATUS[0]}'
  printf 'echo "wtth exit code: $rc" | tee -i -a %s\n' "$(q "$LOG")"
  echo 'exit "$rc"'
} > "$LAUNCH"
chmod 755 "$LAUNCH"

if /usr/bin/env tmux has-session -t "=$SESSION" 2>/dev/null; then
  echo "run_phase: tmux session $SESSION already exists" >&2; exit 5
fi
/usr/bin/env tmux new-session -d -s "$SESSION" "$LAUNCH" \; set-window-option -t "=$SESSION:" remain-on-exit on
echo "started tmux session $SESSION; log: $LOG"
echo "attach: tmux attach -t $SESSION    status: $PY -I -m wtth status"
