-- Recipient response updates: per-request "I know" counter, stored
-- delivery coordinates for post-cutoff chat.update, and the location
-- detail collected by the "I know" modal.
ALTER TABLE reach_requests ADD COLUMN IF NOT EXISTS known_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pings ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT '';
ALTER TABLE pings ADD COLUMN IF NOT EXISTS message_ts TEXT NOT NULL DEFAULT '';
ALTER TABLE ping_outcomes ADD COLUMN IF NOT EXISTS location TEXT;
