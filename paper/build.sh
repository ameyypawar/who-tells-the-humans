#!/bin/bash
# paper/build.sh: generators, then tectonic, then check_paper.
# Usage: build.sh [--offline] [--no-check] [--final]   (--offline = tectonic --only-cached)
set -e
cd "$(dirname "$0")"
PY=/Users/amey/.local/wtth/venv/bin/python
OFFLINE=""; CHECK=--draft; RUN_CHECK=1
for a in "$@"; do
  case "$a" in
    --offline) OFFLINE="--only-cached" ;;
    --no-check) RUN_CHECK=0 ;;
    --final) CHECK=--final ;;
  esac
done
for t in make_placeholders design_macros extract_stimuli render_boxes deviations_table audit_table build_bib table2_narrow; do
  [ -f tools/$t.py ] && $PY tools/$t.py
done
mkdir -p build
tectonic -X compile main.tex --keep-logs --outdir build $OFFLINE
[ "$RUN_CHECK" = 1 ] && [ -f tools/check_paper.py ] && $PY tools/check_paper.py $CHECK
exit 0
