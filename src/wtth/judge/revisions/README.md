# Judge prompt revisions

The judge has one prompt, v1, built by `wtth.judge.prompt.build_system_prompt` from the hash-checked rubric.

A single revision is permitted (rubric section 5, D-007): `system_v2.txt` in this directory. It is written by the
lead only after validation of v1 has failed (fewer than 11 of 12 held-out items) and after a DEVIATIONS entry
exists. It holds the system prompt only; the user template stays that of v1. `judge validate --prompt-version v2
--deviation D-0nn` then re-runs the held-out items plus the same real items. The schema CHECK on
`prompt_versions` makes a third version impossible, and a failure of v2 is a STOP.
