"""Named random streams (plan 47 2.13). Stream i of master seed m is SeedSequence(m, spawn_key=(i,)), identical to
SeedSequence(m).spawn(i + 1)[i], so H1, H2, H3 are the first three spawned streams as research/42 specifies."""
from __future__ import annotations

import numpy as np

STREAMS: dict[str, int] = {
    "h1": 0, "h2": 1, "h3": 2, "h2_p2": 3, "alert_load": 4, "concern": 5, "descriptive": 6, "p1_any": 7,
    "attempts": 8, "test_aware": 9, "kappa": 10, "seed_paired": 11, "sonnet": 12, "misc": 13,
    "posthoc": 14,     # D-021 post-hoc intervals (added after the strict analysis; streams 0-13 are unchanged)
    "exp1": 15,        # D-022 exploratory follow-up cells
    "author_read": 16, # D-025 bootstrap of the author / second-reader kappa
}


def stream(master: int, name: str) -> np.random.Generator:
    if name not in STREAMS:
        raise KeyError(f"unknown stream {name!r}; known: {sorted(STREAMS)}")
    ss = np.random.SeedSequence(int(master), spawn_key=(STREAMS[name],))
    return np.random.Generator(np.random.PCG64(ss))
