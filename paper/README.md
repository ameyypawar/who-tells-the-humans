# paper/

LaTeX sources of "Who Tells the Humans? Phantom Escalation and Channel-Written Receipts in Agent Swarms".
The built PDF is committed as `WTTH-paper.pdf`.

- Build: `WTTH_PYTHON=<your venv python> paper/build.sh [--offline] [--final]`. It runs the generators in `tools/`
  (design macros, stimuli boxes, deviations and audit tables, bibliography), compiles `main.tex` with tectonic,
  then runs `tools/check_paper.py`. `--offline` compiles from cached TeX packages; `--final` makes every check a hard
  failure. The checks include page count, the References heading, the 250-word abstract limit, and a lint that bans
  bare digits in `sections/`.
- Numbers: no result is typed into the text. `generated/analysis/numbers.json` holds every number, with its source
  and display string; `generated/analysis/macros.tex` turns each key into a macro (`\wtthHaikuHThreeDelta`, ...).
  `wtth.analysis` writes both from the private run database. `generated/design/` holds macros from the frozen
  configs and the pre-registration (`data/prereg_constants.yaml`, `data/process_facts.yaml`).
- Branch files: the text was written before the data. `sections/branches/*.tex` holds one variant per possible verdict
  (S supported, O opposite, NR not rejected, D descriptive, N/I for H1), and `branch.tex` inputs the one that matches
  the generated verdict. A final build fails if any verdict maps to no branch.
- Figures and tables are generated into `generated/`. `bib/` holds the references.
- The headline P1, P2 and H3 numbers can be recomputed from the released CSVs with `scripts/release_summary.py`.
