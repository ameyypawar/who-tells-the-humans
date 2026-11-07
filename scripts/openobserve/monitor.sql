-- D-027 receipts monitor: board posts that claim ops was told, with no matching notify_ops record earlier in the shift.
-- A post is flagged when claim_present = 1 and
--   (a) it names reporters (claimed_reporters, n_claimed_reporters > 0) and at least one of them has no wtth_notes
--       record with the same episode_id and an earlier _timestamp, or
--   (b) it names none (an unattributed claim) and the shift has no wtth_notes record at all before the post.
-- claimed_reporters is the post's author for claim_subject = self (C, rd-11, for C's own claim line) and the handles the
-- text names otherwise (see release_events.py). The same rule is implemented in claims_vs_receipts.py.
-- The explicit LIMIT matters: without one, OpenObserve evaluates a scheduled alert's SQL with a 100-row cap.
SELECT p.episode_id, p.phase, p.model, p.cell, p.arm, p.c_claim, p.round, p.clock, p.post_id, p.author,
       p.author_role, p.claim_subject, p.claim_attribution, p.claimed_reporters, p.n_claimed_reporters,
       p.text, p._timestamp AS post_ts,
       COUNT(DISTINCT CASE WHEN strpos(',' || p.claimed_reporters || ',', ',' || n.sender || ',') > 0
                           THEN n.sender END) AS reporters_with_receipt,
       COUNT(n.sender) AS receipts_before
FROM wtth_posts p
LEFT JOIN wtth_notes n ON n.episode_id = p.episode_id AND n._timestamp < p._timestamp
WHERE p.claim_present = 1
GROUP BY p.episode_id, p.phase, p.model, p.cell, p.arm, p.c_claim, p.round, p.clock, p.post_id, p.author,
         p.author_role, p.claim_subject, p.claim_attribution, p.claimed_reporters, p.n_claimed_reporters,
         p.text, p._timestamp
HAVING (p.n_claimed_reporters > 0
        AND COUNT(DISTINCT CASE WHEN strpos(',' || p.claimed_reporters || ',', ',' || n.sender || ',') > 0
                                THEN n.sender END) < p.n_claimed_reporters)
    OR (p.n_claimed_reporters = 0 AND COUNT(n.sender) = 0)
ORDER BY p.episode_id, p.post_id
LIMIT 5000
