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
ALTER TABLE pings
    ADD COLUMN IF NOT EXISTS broadcast_closed BOOLEAN NOT NULL DEFAULT FALSE;

-- A broadcast ping represents one shared channel message, so each responder
-- needs an independent duplicate-click record. The existing ping_outcomes
-- table remains one-row-per-DM-ping.
CREATE TABLE IF NOT EXISTS broadcast_responses (
    id UUID PRIMARY KEY,
    ping_id UUID NOT NULL REFERENCES pings(id) ON DELETE CASCADE,
    responder_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('helped', 'replied', 'unknown')),
    responded_at TIMESTAMPTZ,
    location TEXT,
    UNIQUE (ping_id, responder_id)
);
