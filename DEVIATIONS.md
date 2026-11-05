# Deviations from the pre-registration (PREREG v1, tag `prereg-v1`)

Every change after the freeze is recorded here with a timestamp, the reason, and its effect on the design.

## D-001 — 2026-11-03 (before the sprint, before any data): billing and authentication route
- **Was (PREREG §21):**
  - Max 5x subscription only.
  - setup-token authentication.
  - No API keys; `ANTHROPIC_API_KEY` unset.
- **Now:**
  - **Every experiment agent call** authenticates with an API key from the Console organization linked to the author's Max plan, and is billed to that plan's $100 monthly API credit (renews Nov 13, so it covers the sprint). This covers pilots, the Haiku core, the Sonnet subset, and the judge.
  - The author's own development, analysis and writing sessions stay on the Max 5x subscription.
  - **One route for all agent calls, never switched mid-run.** If the credit is not enough, the Lean tier and the cut order (PREREG §8, §18) apply, instead of moving some calls to the subscription.
  - `ANTHROPIC_API_KEY` is set only in the environment of the experiment's own subprocesses, never globally.
  - `--bare` mode (API-key only) becomes available and is assessed at gate G0 as part of the isolation stack. The setup-token item is replaced by API-key authentication.
  - `costBasis` is logged on every turn.
- **Reason:**
  - The credit became available (claimed Nov 3).
  - It is a separate pool, which protects the subscription for development.
  - It is the lowest-policy-risk route (research/20).
  - It gives exact cost accounting.
- **Effect on the design:**
  - No change to cells, outcomes, hypotheses, tests or seeds.
  - The agent's system-level context may differ (for example, `--bare` may drop Claude Code's identity line). Whatever it is, it is the same in every cell and is recorded by the G0 audit.

## D-002 — 2026-11-03 (before the sprint): pinned CLI version
- **Was (PREREG §21):** the pinned 2.1.311 binary.
- **Now:** the pinned **2.1.317** binary, a copy kept outside the auto-updater's folder, with its SHA-256 recorded.
- **Reason:** the auto-updater installed 2.1.314, 2.1.316 and 2.1.317 and pruned 2.1.311 before it could be copied.
- **Effect:** none on the design. The version is asserted on every call (`init.claude_code_version`).

## D-003 — 2026-11-05 (before the sprint, before any data): larger samples, and the Sonnet arm moved to Saturday
- **Was (PREREG v1 §7–§9):**
  - Haiku core: 4×40 + 7×30 = 370 episodes.
  - Sonnet subset: 5 cells × 15 = 75 episodes, run after the Sunday reset.
- **Now:**
  - Haiku core: **4×80 + 7×50 = 670 episodes**. NONE, CLAIM, NONE-R and NO-RECEIPT get 80 seeds; CONCERN, POLICY, RECEIPT, FORGED, NEG, POS and B-NONE get 50.
  - Sonnet subset: **the same 5 cells × 30 = 150 episodes**, run on Saturday right after the Haiku core.
  - The Lean tier and the cut order still apply if needed.
- **Reason:** a toolchain rehearsal on an unrelated toy task measured realistic turns at about $0.0006–0.0009 (Haiku) and $0.0023–0.0093 (Sonnet). The whole design costs about $50–60 of the $99.99 credit, so power can be raised at no extra cost.
  - Per-witness MDE falls from about 22 pp to about 16 pp in the 80-seed cells.
  - The H2 difference-in-differences MDE falls from about 31 pp to about 23 pp.
  - The CI-based null rule becomes attainable.
  - The Sonnet arm no longer depends on the subscription reset, because all agent calls use the API credit (D-001).
- **Effect on the design:** cells, outcomes, hypotheses and tests are unchanged. Only n changes, and the change was fixed before any data was collected.
