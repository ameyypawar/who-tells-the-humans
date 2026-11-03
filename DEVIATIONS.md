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
