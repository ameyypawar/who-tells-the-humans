#!/bin/sh
# Writes wtth.pth into the wtth venv's site-packages. Idempotent, no network.
# Line 1: <repo>/src   (so `import wtth` works under `python -I`, research/41 D8)
# Line 2: <repo>       # Not in 41 ("one line"). Needed so `from tests.util import ...` works under
#                      `python -I -m unittest discover -s tests` (discover puts tests/ itself, not the repo root,
#                      on sys.path, and -I drops the cwd).
set -eu
R="$(cd "$(dirname "$0")/.." && pwd)"
PY="${WTTH_PYTHON:-/Users/amey/.local/wtth/venv/bin/python}"
SP="${WTTH_SITE_PACKAGES:-$("$PY" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')}"  # override: tests use a temp dir
[ -d "$SP" ] || { echo "install_pth: site-packages not found: $SP" >&2; exit 1; }
printf '%s\n%s\n' "$R/src" "$R" > "$SP/wtth.pth"
echo "install_pth: wrote $SP/wtth.pth -> $R/src, $R"
