# Release data: Who Tells the Humans?

Licence: CC BY 4.0 (see `LICENSE` in this directory). Code in the repository is MIT.

Contents: masked transcripts, outcome tables and labels for the confirmatory episodes. The withheld strings are
available on request and are not in this repository. Raw API bodies, token usage, billing and environment data are
not released.

## Files

| File | Rows | What |
|---|---|---|
| `outcomes_per_shift.csv` | 820 | One row per episode ("shift"): 670 confirmatory Haiku 5.5 (`phase=core`), 150 confirmatory Sonnet 5.5 (`phase=sonnet`). |
| `outcomes_per_witness.csv` | 2,460 | One row per witness (3 per shift): 2,010 Haiku, 450 Sonnet. |
| `labels_judge.csv` | 8,454 | One row per judged item (board post, note, D1 answer, D2 answer) of those episodes: the LLM-judge label (active prompt version v2), with `judge_status` valid, invalid or refused. Labels are empty for invalid and refused rows. |
| `labels_final.csv` | 8,450 | Final label per item: the reviewed label where one exists (212 items), otherwise the judge label (8,238 items). Four items have no valid judge label and are absent. |
| `transcripts_masked.jsonl.gz` | 820 lines | One JSON object per episode, see below. About 6 MB compressed. |

All files are far below 50 MB.

## Outcome tables

Built with the repository's analysis code (`wtth.analysis.outcomes`: `witness_outcomes`, `episode_outcomes`) from the
final-attempt views of the main database and the final labels, restricted to the analysis set. Column meaning is
documented in `src/wtth/analysis/outcomes.py`. Key columns:

- `outcomes_per_shift.csv`: `episode_id, attempt, model, cell, seed, world, n_witnesses`, `y` (witnesses with P1),
  `p2`, `p2prime` (P2'), `p2prime_lo`/`p2prime_hi` (bounds when labels are unresolved), `any_note`, `first_round`,
  `alert_load`, `truth_any`, `truth_viol`, `concern_episode`, `c_note`, plus `phase`. Empty means undefined (P2 and P2'
  exist only in the eight P2 cells).
- `outcomes_per_witness.csv`: `episode_id, attempt, slot, handle, phase, model, cell, world, seed`, `p1`, `any_note`,
  `n_notes`, `first_round`, `concern`, `esc_post`, claim counts, debrief answers (`d1_verdict`, `believes`, `basis`,
  `reasons`), `ta_max` (test awareness), phantom flags and `n_posts`. Boolean columns are 0/1; list columns are
  joined with `|`.

`attempt` is the final (done) attempt of the episode. Failed and abandoned attempts are not released.

## Labels

Columns: item identity (`item_id` is opaque; `source_table` and `source_id` give the row in the transcripts: `posts.id`,
`notes.id`, or `episode:attempt:slot:D1|D2` for debrief answers), the episode keys (`phase, model, cell, seed, world`),
`round, slot, handle, kw_match` (notes only), then the label columns (`concern`, `about_violation`,
`addressed_to_humans`, `escalation_to_humans`, `refers_to_violation`, `test_awareness`, `d1_verdict`, `basis`,
`reason_codes`, and so on; which apply depends on the item type), and `uncertain`.

`label_source` in `labels_final.csv` is `judge` or `reviewed`. `reviewed` means: the item was drawn into a review queue,
an AI annotator labelled it, and the author approved or corrected that label (AI-audited, author-approved). The 615
queue rows voided by D-019 (duplicate H-5 draw) are excluded, as in the analysis. Judge quotes and justifications are
not released.

## Masked transcripts

Each line is one episode: `episode_id, attempt, phase, model, cell, seed, world, scenario, handles`, and

- `board_posts`: every post on the board (scripted and live): `id, round, sender, slot, clock, kind, line_id,
  is_private, text, was_cut, was_collapsed, visible_from_round, turn_id`;
- `notes`: notes to ops (`subject`, `message`, `source` tool or script);
- `escalation_log`: the log lines shown to the witnesses (`ref_no`, `clock`, `sender`, `subject`);
- `turns`: per agent turn (`turn_type` witness, d1, d2; `round, slot, handle, try_no, class`), each with `tool_calls`
  (`tool, args, accepted, reject_reason`), `tool_uses` (the tool-use blocks the agent emitted, with `result`) and
  `debrief` answers.

Not included: raw API bodies, prompts, hidden reasoning, usage, cost, timing, environment and path data.

## Masking rule

Every withheld string is replaced, raw and in both JSON-escaped forms, wherever it occurs:

- `[WITHHELD:claim-paraphrase]` for the withheld paraphrases of the C claim (main and backup scenario);
- `[WITHHELD:forged-line]` for the withheld forged receipt line (main and backup).

Beyond exact matches, any span of 30 or more characters shared with a withheld string is replaced by the same
placeholder, unless the span also occurs in a public pre-registered line. For the forged line the exemption covers the
pre-registered public files (`prereg/PREREG.md`, `prereg/prompts/*`), because the forged line follows the public log
layout; only the entry id is private. Real receipts in the RECEIPT, POS, NONE-R, NEG and related cells stay intact, and
bare entry ids are never masked (witness notes cite E-numbers). In FORGED the receipt-style line in C's post is
replaced by `[WITHHELD:forged-line]`, and it follows the public log layout.

Every released file was scanned after writing: 0 exact residuals of any withheld string and 0 remaining 30-character
fragments under the rule above.

## Counts and the paper

Episodes per cell (shifts): NONE 110, NONE-R 110, CLAIM 110, NO-RECEIPT 110 (80 Haiku + 30 Sonnet each), FORGED 80
(50 + 30), B-NONE, CONCERN, POLICY, RECEIPT, POS, NEG 50 each (Haiku only). Witnesses are three times that. These match
`paper/generated/analysis/numbers.json` (`Haiku/SonnetPOne<Cell>N` witnesses, `PTwo<Cell>N` episodes; 670 and 150 in
set).

| Paper element | Files |
|---|---|
| P1 rates by cell (Table of hypotheses, Fig. P1 by cell, Kaplan-Meier) | `outcomes_per_witness.csv` (`p1`, `first_round`) |
| P2 / P2' (episode-level escalation) | `outcomes_per_shift.csv` (`p2`, `p2prime`) |
| Any-note rates | `any_note` in either outcome file |
| Judge validation, confusion, review corrections | `labels_judge.csv` against `labels_final.csv` (`label_source`) |
| Debrief and test-awareness tables | `d1_verdict`, `believes`, `basis`, `reasons`, `ta_max` in `outcomes_per_witness.csv` |
| Qualitative examples | `transcripts_masked.jsonl.gz` |

The exporter is `src/wtth/release.py` (`build_data_release`).
