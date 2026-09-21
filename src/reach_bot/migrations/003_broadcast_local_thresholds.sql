-- Broadcast messages (channel/workspace posts) close via their own local,
-- per-message thresholds, entirely decoupled from the DM-side global
-- known_count on reach_requests. Broadcast pings are exactly those with
-- candidate_id = '' (a channel post creates one ping per posted channel,
-- with no pre-known recipient; the responder's identity is read lazily
-- from the clicker at response time).
--
-- local_know_count: increments only on "I know" responses to THIS message.
-- local_total_count: increments on ANY response type to THIS message.
-- The message closes when EITHER count reaches its threshold (3 / 5).
ALTER TABLE pings
    ADD COLUMN IF NOT EXISTS local_know_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pings
    ADD COLUMN IF NOT EXISTS local_total_count INTEGER NOT NULL DEFAULT 0;
