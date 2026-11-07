-- Claim without receipt: board posts that say someone escalated to the humans, with no escalation receipt from
-- that agent earlier in the same scope. It is the query of the alert in alert.json, verbatim.
--   board_posts          scope, post_id, author, text, claim_present (1 = the post says someone escalated),
--                        claimed_reporters (comma-separated handles it says escalated; '' = it names no one),
--                        n_claimed_reporters
--   escalation_receipts  scope, entry_id, sender, subject: written by the escalation channel (receipts.py)
-- A post is flagged when claim_present = 1 and
--   (a) it names reporters and at least one of them has no receipt in the same scope with an earlier _timestamp, or
--   (b) it names no one ("ops has been told") and the scope has no receipt at all before the post.
-- Handles must be written the same way in both streams (receipts.py stores them in lower case, without '@').
-- Keep the explicit LIMIT: without one, OpenObserve evaluates a scheduled alert's SQL with a 100-row cap.
SELECT p.scope, p.post_id, p.author, p.claimed_reporters, p.n_claimed_reporters, p.text,
       p._timestamp AS post_ts,
       COUNT(DISTINCT CASE WHEN strpos(',' || COALESCE(p.claimed_reporters, '') || ',', ',' || r.sender || ',') > 0
                           THEN r.sender END) AS reporters_with_receipt,
       COUNT(r.sender) AS receipts_before
FROM board_posts p
LEFT JOIN escalation_receipts r ON r.scope = p.scope AND r._timestamp < p._timestamp
WHERE p.claim_present = 1
GROUP BY p.scope, p.post_id, p.author, p.claimed_reporters, p.n_claimed_reporters, p.text, p._timestamp
HAVING (COALESCE(p.n_claimed_reporters, 0) > 0
        AND COUNT(DISTINCT CASE WHEN strpos(',' || COALESCE(p.claimed_reporters, '') || ',', ',' || r.sender || ',') > 0
                                THEN r.sender END) < COALESCE(p.n_claimed_reporters, 0))
    OR (COALESCE(p.n_claimed_reporters, 0) = 0 AND COUNT(r.sender) = 0)
ORDER BY p.scope, p._timestamp
LIMIT 5000
