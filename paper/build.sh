#!/bin/bash
# paper/build.sh: generators, then tectonic, then check_paper. Usage: build.sh [--offline] [--no-check]
set -e
cd "$(dirname "$0")"
PY=/Users/amey/.local/wtth/venv/bin/python
for t in make_placeholders design_macros extract_stimuli render_boxes deviations_table audit_table build_bib; do
  [ -f tools/$t.py ] && $PY tools/$t.py
done
FLAGS=""; [ "$1" = "--offline" ] && FLAGS="--only-cached"
tectonic -X compile main.tex --keep-logs --outdir build $FLAGS
[ "$1" = "--no-check" ] || [ ! -f tools/check_paper.py ] || $PY tools/check_paper.py --draft
