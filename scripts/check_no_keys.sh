#!/bin/bash
# Pre-release / G0 key-leak check (research/41 rule 5, G0-10).
# Looks for the API key prefix in the repo, git history, runs/ and the slot dirs. Exits 1 on any hit, 0 if clean.
# Usage: scripts/check_no_keys.sh [--no-git] [PATH ...]   (default PATHs: repo root, runs/, ~/.local/wtth/slots)
# The prefix is composed here at runtime, so this file never matches itself. Only file names are printed, never
# the matching lines (a hit may be a live key).
set -u
P="sk-""ant"
R="$(cd "$(dirname "$0")/.." && pwd)"
use_git=1
defaulted=0
paths=()
for a in "$@"; do
  case "$a" in
    --no-git) use_git=0 ;;
    *) paths+=("$a") ;;
  esac
done
if [ "${#paths[@]}" -eq 0 ]; then
  defaulted=1
  paths=("$R")
  [ -d "$R/runs" ] && paths+=("$R/runs")
  [ -d "$HOME/.local/wtth/slots" ] && paths+=("$HOME/.local/wtth/slots")
fi
hits=0
for p in "${paths[@]}"; do
  if [ ! -e "$p" ]; then
    echo "check_no_keys: path not found: $p" >&2
    exit 2
  fi
  # .pyc files fold "sk-" + "ant-" into one constant, so compiled caches are not scanned; .git is covered below.
  out="$(/usr/bin/grep -r -a -l -F "$P" --exclude-dir=.git --exclude-dir=__pycache__ --exclude='*.pyc' "$p" 2>/dev/null)"
  # default scan only: fake-run shims/slots and test scratch dirs are not release material
  if [ "$defaulted" -eq 1 ] && [ -n "$out" ]; then
    out="$(printf '%s\n' "$out" | /usr/bin/grep -v -e '/runs/fake/' -e '/tests/tmp/')"
  fi
  if [ -n "$out" ]; then
    echo "KEY PATTERN HIT in files:"
    echo "$out"
    hits=1
  fi
done
if [ "$use_git" -eq 1 ] && git -C "$R" rev-parse --git-dir >/dev/null 2>&1; then
  n="$(git -C "$R" log -p --all 2>/dev/null | /usr/bin/grep -a -c -F "$P")"
  if [ "${n:-0}" -gt 0 ]; then
    echo "KEY PATTERN HIT in git history ($n lines)"
    hits=1
  fi
fi
if [ "$hits" -eq 0 ]; then
  echo "check_no_keys: no hits"
fi
exit "$hits"
