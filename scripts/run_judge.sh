#!/bin/bash
# Start one judge command in a detached tmux session under caffeinate.
#   scripts/run_judge.sh <command> [args...]     e.g. scripts/run_judge.sh validate ; scripts/run_judge.sh run --tier J1
# Same pattern as run_phase.sh: the judge is started through `env -i` with only HOME, PATH, LANG and TERM, so nothing
# from this shell (CLAUDECODE, ANTHROPIC_*, CLAUDE_*) reaches it and its own environment guard passes.
set -eu
[ $# -ge 1 ] || { echo "usage: $0 <command> [wtth.judge args...]" >&2; exit 2; }
R="$(cd "$(dirname "$0")/.." && pwd)"
PY="${WTTH_PYTHON:-/Users/amey/.local/wtth/venv/bin/python}"
CMD="$1"; shift
mkdir -p "$R/runs/logs"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$R/runs/logs/judge-${CMD}-${STAMP}.log"
LAUNCH="$R/runs/logs/judge-${CMD}-${STAMP}.launch.sh"
SESSION="wtth-judge"

# shell-quote one argument
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

{
  echo '#!/bin/bash'
  printf 'cd %s || exit 1\n' "$(q "$R")"
  printf 'env -i HOME=%s PATH=%s LANG=en_US.UTF-8 TERM=%s \\\n' "$(q "$HOME")" "$(q "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin")" "$(q "${TERM:-xterm-256color}")"
  printf '  caffeinate -dims %s -I -m wtth.judge %s' "$(q "$PY")" "$(q "$CMD")"
  for a in "$@"; do printf ' %s' "$(q "$a")"; done
  printf ' 2>&1 | tee -i -a %s\n' "$(q "$LOG")"
  echo 'rc=${PIPESTATUS[0]}'
  printf 'echo "wtth.judge exit code: $rc" | tee -i -a %s\n' "$(q "$LOG")"
  echo 'exit "$rc"'
} > "$LAUNCH"
chmod 755 "$LAUNCH"

if /usr/bin/env tmux has-session -t "=$SESSION" 2>/dev/null; then
  echo "run_judge: tmux session $SESSION already exists" >&2; exit 5
fi
/usr/bin/env tmux new-session -d -s "$SESSION" "$LAUNCH" \; set-window-option -t "=$SESSION:" remain-on-exit on
echo "started tmux session $SESSION; log: $LOG"
echo "attach: tmux attach -t $SESSION    status: $PY -I -m wtth.judge status"
