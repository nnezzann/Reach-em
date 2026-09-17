CREATE TABLE IF NOT EXISTS pings (
    id UUID PRIMARY KEY, requester_id TEXT NOT NULL, target_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL, channel_context TEXT, presence_at_ping TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ping_outcomes (
    id UUID PRIMARY KEY, ping_id UUID NOT NULL UNIQUE REFERENCES pings(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (outcome IN ('helped','relayed','no_response','unknown','wrong_person')),
    responded_at TIMESTAMPTZ, response_latency_seconds INTEGER
);
CREATE TABLE IF NOT EXISTS affinity_scores (
    target_id TEXT NOT NULL, candidate_id TEXT NOT NULL, score DOUBLE PRECISION NOT NULL DEFAULT 0,
    sample_size INTEGER NOT NULL DEFAULT 0, last_updated TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS pings_target_candidate_idx ON pings(target_id, candidate_id);
