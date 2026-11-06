"""Submission gate (plan 49 section 1.6).  Usage: check_paper.py --draft | --final
--draft : hard-fails only items 3 (bare digits), 6 (private strings), 7 (glyphs/handles) and undefined control sequences;
          everything else is reported as a warning.
--final : every item must pass.
"""
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I tools/check_paper.py` drops the script dir
from _common import PAPER, REPO  # noqa: E402

FINAL = "--final" in sys.argv
fails: list[str] = []
warns: list[str] = []


def bad(item: str, msg: str, hard_in_draft: bool = False) -> None:
    (fails if (FINAL or hard_in_draft) else warns).append(f"[{item}] {msg}")


def strip_comments(t: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", t)


def tex_files(*dirs: str):
    return sorted(p for d in dirs for p in (PAPER / d).rglob("*.tex")) if dirs else []


LOG = (PAPER / "build/main.log").read_text(errors="replace") if (PAPER / "build/main.log").exists() else ""


def pdftext(pages: bool = False) -> str:
    pdf = PAPER / "build/main.pdf"
    if not pdf.exists():
        bad("1", "build/main.pdf missing", True)
        return ""
    return subprocess.run([shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True).stdout


TEXT = pdftext()
PAGES = TEXT.split("\f")

# ---- 1 status ---------------------------------------------------------------------------------------------------
chain = [PAPER / "generated/analysis/macros.tex", PAPER / "generated/analysis/macros.provisional.tex",
         PAPER / "generated/placeholder/macros.placeholder.tex"]
src = next(p for p in chain if p.exists())
m = re.search(r"\\newcommand\{\\wtthStatus\}\{([A-Z]+)\}", src.read_text())
status = m.group(1) if m else "?"
if status != "FINAL":
    bad("1", f"wtthStatus = {status} (loaded from {src.relative_to(PAPER)})")
for w in ("PLACEHOLDER", "PROVISIONAL", "n/a", "??", "[AUTHOR", "[A:", "TODO", "[STUB"):
    # the watermark is drawn text; in a FINAL build it is absent
    if w in TEXT:
        bad("1", f"PDF text contains {w!r}")

# ---- 2 definitions ----------------------------------------------------------------------------------------------
loaded = [PAPER / n for n in ("preamble.tex", "loaders.tex", "branch.tex", "main.tex", "cited.tex")] + [src] + \
         [PAPER / f"generated/design/{n}.tex" for n in ("design", "stimuli", "audit")]
defs = set()
for f in loaded:
    t = f.read_text(encoding="utf-8")
    defs |= set(re.findall(r"\\(?:newcommand|renewcommand|providecommand|def)\*?\s*\{?\\((?:wtth|cited)[A-Za-z]+)", t))
    for n in re.findall(r"\\newif\\if(wtth[A-Za-z]+)", t):
        defs |= {n, n + "true", n + "false"}
used = set()
for f in tex_files("sections") + [PAPER / "main.tex"]:
    used |= set(re.findall(r"\\((?:wtth|cited)[A-Za-z]+)", strip_comments(f.read_text(encoding="utf-8"))))
und = sorted(u for u in used if u not in defs)
if und:
    bad("2", f"used but not defined: {und}", True)
if "Undefined control sequence" in LOG:
    bad("2", "log: Undefined control sequence", True)
for pat in ("Citation `", "Reference `", "There were undefined"):
    if re.search(pat + r".*undefined", LOG):
        bad("2", f"log: undefined citations or references ({pat})")
        break
for ln in (PAPER / "cited.tex").read_text().splitlines():
    if r"\newcommand" in ln and "verified=yes" not in ln:
        bad("2", f"cited.tex not verified: {ln.split('}')[0]}}}")
cited_keys = set(re.findall(r"\\cite[a-z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}", "\n".join(strip_comments(f.read_text()) for f in tex_files("sections"))))
cited_keys = {k.strip() for g in cited_keys for k in g.split(",")}
status_by_key = {r["key"]: r["status"] for r in csv.DictReader((PAPER / "bib/citations-verified.csv").open())}
for k in sorted(cited_keys):
    if status_by_key.get(k) != "verified":
        bad("2", f"cited key not verified: {k} ({status_by_key.get(k, 'not listed')})")

# ---- 3 bare-digit lint ------------------------------------------------------------------------------------------
ALLOW = re.compile(r"\b(?:H[1-3]|P[12]['\u2032]?|P2\\prime|RQ[1-4]|G[0-2]|L[1-3]|R[01]|D-\d{3}|[Rr]ound \d|SHA-256|v1)\b|P2\$\\prime\$|P2\\ensuremath\{\\prime\}")
ARGS = re.compile(r"\\(?:cite[a-z]*\*?|ref|eqref|label|input|include|includegraphics|url|href|path|texttt|wtthFig|wtthTab|wtthInputBranch|"
                  r"wtthStub|begin|end|section|subsection|paragraph|hypertarget|hyperlink|caption|bibliography|DefineVerbatimEnvironment)\*?(?:\[[^\]]*\])*\{[^}]*\}")
DIM = re.compile(r"-?\d*\.?\d+\s*(?:pt|em|ex|in|cm|mm|bp|\\[A-Za-z]+)")
SPELLED = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|hundred|thousand)\b", re.I)
digit_hits = 0
for f in tex_files("sections"):
    for no, ln in enumerate(strip_comments(f.read_text(encoding="utf-8")).splitlines(), 1):
        s = ARGS.sub("", ln)
        s = re.sub(r"\\[A-Za-z]+", "", s)          # macro names (digit-free by construction)
        s = DIM.sub("", s)
        s = ALLOW.sub("", s)
        if re.search(r"\d", s):
            digit_hits += 1
            bad("3", f"bare digit {f.relative_to(PAPER)}:{no}: {ln.strip()[:90]}", True)
        if SPELLED.search(ARGS.sub("", ln)) and not re.search(r"\\wtthStub", ln):
            warns.append(f"[3] spelled number {f.relative_to(PAPER)}:{no} (use \\wtthDesign...Word): {ln.strip()[:70]}")

# ---- 4 page count -----------------------------------------------------------------------------------------------
n_pages = len(PAGES) - 1 if PAGES and PAGES[-1].strip() == "" else len(PAGES)
ref_page = next((i + 1 for i, p in enumerate(PAGES) if re.search(r"^\s*References\s*$", p, re.M)), None)
if ref_page is None:
    bad("4", "no References heading found")
else:
    last_main = ref_page if re.search(r"Author Contributions", "\n".join(PAGES[:ref_page])) else ref_page
    if last_main > 8:
        bad("4", f"main text ends on page {last_main} (limit 8)")
    print(f"[4] total pages {n_pages}; References on page {ref_page}")
for h in ("Introduction", "Related Work", "Methods", "Results", "Discussion", "Conclusion", "Code and Data", "Author Contributions"):
    pg = next((i + 1 for i, p in enumerate(PAGES) if re.search(rf"^\s*(?:\d+\s+)?{h}", p, re.M)), None)
    print(f"[4]   {h}: page {pg}")

# ---- 5 abstract ---------------------------------------------------------------------------------------------------
am = re.search(r"Abstract\s*\n(.*?)\n\s*1\s+Introduction", TEXT, re.S)
(PAPER / "build").mkdir(exist_ok=True)
if am:
    words = am.group(1).split()
    (PAPER / "build/abstract.txt").write_text(" ".join(words) + "\n")
    print(f"[5] abstract words: {len(words)}")
    if not 150 <= len(words) <= 250:
        bad("5", f"abstract has {len(words)} words (need 150 to 250)")
else:
    bad("5", "abstract not found in PDF text")

# ---- 6 private strings --------------------------------------------------------------------------------------------
try:
    sys.path.insert(0, str(REPO / "src"))
    from wtth.config import load_settings
    from wtth.prereg import load_private
    from wtth.redact import _Redactor

    red = _Redactor(dict(load_private(load_settings())))
    leaks = {}
    for p in sorted(x for x in PAPER.rglob("*") if x.is_file() and x.suffix in {".tex", ".bib", ".csv", ".yaml", ".py", ".sh", ".txt", ".log"} and ".git" not in x.parts):
        hit = red.residual(p.read_bytes())
        if hit:
            leaks[str(p.relative_to(PAPER))] = sorted(hit)
    hit = red.residual(TEXT.encode())
    if hit:
        leaks["<pdf text>"] = sorted(hit)
    if leaks:
        bad("6", f"private string ids found (ids only): {leaks}", True)
    else:
        print("[6] private-string scan: {}")
except Exception as e:  # noqa: BLE001
    bad("6", f"private scan could not run: {type(e).__name__}: {str(e)[:120]}", True)

# ---- 7 glyphs and handles -------------------------------------------------------------------------------------------
if "Missing character" in LOG:
    bad("7", "log: Missing character", True)
nourl = re.sub(r"https?://\S+|\b[\w.+-]+@[\w-]+\.[\w.]+", "", TEXT)
if "@" in nourl:
    bad("7", "'@' in PDF text outside URLs/emails", True)

# ---- 8 provenance ---------------------------------------------------------------------------------------------------
d = (PAPER / "generated/design/design.tex").read_text()
if "wtthDesignProvisional}{yes}" in d:
    bad("8", "design.tex was generated from the main fallback, not frozen-v1")
if FINAL:
    nj = PAPER / "generated/analysis/numbers.json"
    if not nj.exists():
        bad("8", "generated/analysis/numbers.json missing")

for w in warns:
    print("WARN", w)
for f in fails:
    print("FAIL", f)
print(f"check_paper {'--final' if FINAL else '--draft'}: {len(fails)} fail, {len(warns)} warn")
sys.exit(1 if fails else 0)
