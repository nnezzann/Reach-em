-- Rebuild persisted broadcast counters from the per-responder response ledger.
-- This repairs rows created while the broadcast threshold behavior was being
-- rolled out, so a stale broadcast_closed flag cannot close a message on its
-- first response.
WITH response_counts AS (
    SELECT
        ping_id,
        COUNT(*)::INTEGER AS total_count,
        COUNT(*) FILTER (WHERE outcome = 'helped')::INTEGER AS know_count
    FROM broadcast_responses
    GROUP BY ping_id
)
UPDATE pings AS p
SET local_know_count = COALESCE(r.know_count, 0),
    local_total_count = COALESCE(r.total_count, 0),
    broadcast_closed = (
        COALESCE(r.know_count, 0) >= 3
        OR COALESCE(r.total_count, 0) >= 5
    )
FROM response_counts AS r
WHERE p.id = r.ping_id
  AND p.candidate_id = '';

UPDATE pings
SET local_know_count = 0,
    local_total_count = 0,
    broadcast_closed = FALSE
WHERE candidate_id = ''
  AND NOT EXISTS (
      SELECT 1 FROM broadcast_responses WHERE ping_id = pings.id
  );
