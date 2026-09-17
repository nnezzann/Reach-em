# Technical Specification — Slack "Reach" Bot

Companion to `AGENTS.md` (product/UX spec). This document defines the
system architecture, data layer, API surface, and build sequence for the
coding agent. Where product behavior is described, `AGENTS.md` is the
source of truth — this document covers *how* to build it.

---

## 1. System Overview

A Slack app (slash command + interactivity endpoints) that, given a target
user, returns a ranked, categorized, minimal list of candidates likely to
help reach them — combining live Slack signals (presence, channel
structure, thread activity) with a learned affinity model that improves
from real outcomes over time.

```
Slack Workspace
     │
     │ slash command, button clicks, modal submits
     ▼
[ Bolt App / HTTP Server ]  ──────►  [ Slack Web API ]
     │                                  (presence, channels, threads)
     ▼
[ Ranking Engine ]
     │
     ├── Static signals (real-time)
     └── Learned affinity (from Postgres, precomputed)
     │
     ▼
[ Postgres ]  ◄── background aggregation job (decayed scores)
     │
[ Redis ] (presence cache, short TTL)
```

---

## 2. Tech Stack

| Layer            | Choice                                  | Notes |
|-------------------|------------------------------------------|-------|
| Runtime            | Python 3.12+                            | Project decision; dependencies are managed with `uv` |
| Slack framework    | `slack-bolt`                             | Handles slash commands, block actions, view submissions, signature verification |
| Primary DB         | PostgreSQL 15+                          | Source of truth: pings, outcomes, affinity scores |
| Cache              | Redis                                   | Presence cache only, short TTL (30–60s) |
| Database driver    | `psycopg`                               | PostgreSQL adapter for transactional writes and affinity reads |
| Background jobs    | Python scheduler integration            | Nightly + on-write decay recomputation |
| Deployment         | Self-hosted (Podman), per existing infra pattern | Matches current self-hosted infra approach |
| Secrets            | Environment injection                   | Slack tokens, signing secret; use the deployment's secret manager |

---

## 3. Data Layer

### 3.1 Schema (Prisma-style, illustrative)

```prisma
model Ping {
  id              String   @id @default(cuid())
  requesterId     String
  targetId        String
  candidateId     String
  channelContext  String?  // channel ID this candidate was surfaced from, if any
  presenceAtPing  String   // "active" | "offline"
  createdAt       DateTime @default(now())

  outcome         PingOutcome?
}

model PingOutcome {
  id                      String   @id @default(cuid())
  pingId                  String   @unique
  ping                    Ping     @relation(fields: [pingId], references: [id])
  outcome                 String   // "helped" | "relayed" | "no_response" | "unknown" | "wrong_person"
  respondedAt             DateTime?
  responseLatencySeconds  Int?
}

model AffinityScore {
  targetId     String
  candidateId  String
  score        Float    @default(0)
  sampleSize   Int      @default(0)
  lastUpdated  DateTime @updatedAt

  @@id([targetId, candidateId])
}
```

### 3.2 Write path
- `Ping` row inserted synchronously when a candidate list is generated
  (one row per candidate shown, not just the one clicked) — this is
  needed so "shown but not clicked" can later be distinguished from
  "clicked but no response," which matters for outcome modeling.
- `PingOutcome` inserted/updated asynchronously — either via the quick-reply
  buttons ("I'll relay" / "Don't know") or a timeout job marking
  `no_response` after N hours with no reply.

### 3.3 Aggregation job (decay computation)
- Runs on a schedule (nightly) and/or triggered on new outcome writes.
- For each `(targetId, candidateId)` pair, recompute `score` as an
  exponentially decayed weighted average of outcomes, more recent outcomes
  weighted higher.
- Update `sampleSize` alongside `score` — the ranking engine must know how
  much evidence backs a score (see §4.3, cold-start gating).
- Implementation detail left to the coding agent: a simple decayed average
  is sufficient for v1; do not over-engineer into a full Bayesian model
  before there's enough real data to justify it.

### 3.4 Presence cache (Redis)
- Key: `presence:{userId}`, value: `active` | `offline`, TTL 30–60s.
- Ranking engine reads from cache first; falls back to a live
  `users.getPresence` call on cache miss, then repopulates the cache.
- This is the only ephemeral/non-durable data in the system — never
  written to Postgres.

---

## 4. Ranking Engine

Pure function, no side effects, testable in isolation from Slack:

```
rankCandidates(targetId, requesterId) -> { active: Candidate[], offline: Candidate[] }
```

### 4.1 Candidate generation (static signals)
1. Fetch `targetId`'s public channel memberships (Slack API,
   `users.conversations`).
2. For each channel, fetch co-members; weight by inverse channel size.
3. Fetch recent thread replies (last 7 days, configurable) in those
   channels where `targetId` participated; extract co-repliers.
4. Merge into a single candidate pool with two labeled signal types:
   `channel_proximity` and `thread_recency`. Do not blend into one number
   at this stage — keep them as separate fields per candidate.

### 4.2 Candidate ranking (blend static + learned)
1. Fetch `AffinityScore` rows for `(targetId, *)` from Postgres.
2. For any candidate with `sampleSize >= MIN_SAMPLE_THRESHOLD` (default: 3),
   apply the learned score as a **boost** on top of the static ranking —
   never as a full replacement. Candidates below the threshold are ranked
   purely on static signals.
3. Sort within each presence bucket by: learned boost (if eligible) →
   channel_proximity weight → thread_recency weight.

### 4.3 Bucketing and capping
1. Split ranked candidates into `active` / `offline` using the Redis
   presence cache.
2. Cap each bucket at `MAX_PER_BUCKET` (default: 3), per AGENTS.md §4.4.
3. Return the capped, bucketed result — this is what the Block Kit layer
   renders directly, with no further logic in the presentation layer.

### 4.4 Config surface
Expose as environment variables / config, not hardcoded:
- `MIN_SAMPLE_THRESHOLD` (default 3)
- `MAX_PER_BUCKET` (default 3)
- `THREAD_RECENCY_DAYS` (default 7)
- `AFFINITY_DECAY_HALFLIFE_DAYS` (default 30 — tune after v1 usage data)

---

## 5. Slack App Surface

### 5.1 Slash command: `/reach @X [note]`
- Verify request signature (Bolt handles this).
- Parse target user + optional note.
- Call `rankCandidates`.
- Render Block Kit message (per AGENTS.md §5) as an **ephemeral** response.
- Log a `Ping` row per candidate shown.

### 5.2 Block action: "Ping @A" button click
- `action_id`: `ping_candidate`, payload includes `candidateId`, `targetId`,
  originating `pingId`.
- Open a modal (`views.open`) with a single pre-filled `plain_text_input`
  block containing the templated message.
- On modal submit (`view_submission`): send DM to candidate via
  `chat.postMessage`, framed with requester identity (per AGENTS.md §6,
  open decision on "sent as bot vs. requester" — default to "bot relaying
  on behalf of X" unless told otherwise).

### 5.3 Block action: quick replies (candidate-side, v2)
- `action_id`: `outcome_helped` / `outcome_relayed` / `outcome_unknown`.
- Writes to `PingOutcome` for the corresponding `pingId`.
- Triggers (or waits for the scheduled) aggregation job to update
  `AffinityScore`.

### 5.4 Block action: "Why these people" (v2)
- Opens a modal or posts a second ephemeral message with per-candidate
  evidence, sourced directly from the same data used in ranking (do not
  recompute — pass the evidence through from `rankCandidates`).

---

## 6. Build Sequence

Follow AGENTS.md's v1/v2 split. Within v1, build in this order so each
piece is independently testable before the next depends on it:

1. **Schema + migrations** (Prisma, Postgres) — `Ping`, `PingOutcome`,
   `AffinityScore` tables, even though learned scoring isn't used until v2.
   Logging pings from day one means v2's learning loop has data to work
   with immediately instead of starting cold.
2. **Static ranking engine** (`rankCandidates`, static signals only —
   presence + channel-size; skip thread co-occurrence in v1 per AGENTS.md).
   Unit-testable with mocked Slack API responses.
3. **Redis presence cache** wired into the ranking engine.
4. **Slash command + Block Kit rendering** (ephemeral message, capped
   buckets, buttons only, no "Why" detail yet).
5. **Ping button → modal → DM send** flow.
6. **Outcome logging** (even a manual/basic version) so data starts
   accumulating during v1 usage, ahead of v2's learned-scoring work.

v2 additions (after v1 is validated against real usage):
7. Thread co-occurrence signal with recency decay.
8. Aggregation job computing `AffinityScore`, wired into the ranking blend.
9. Quick-reply outcome buttons on the candidate side.
10. "Why these people" detail view.

---

## 7. Constraints & Guardrails (carry into implementation)

- Bot must never post into a public channel on the requester's behalf —
  all bot-generated suggestion messages are ephemeral; only explicit
  requester-approved DMs (via the modal) are actually sent.
- No private-channel or DM content is ever read — all signal derives from
  public channels/threads the bot is installed into, per Slack API limits
  and the project's stated privacy boundary.
- `AffinityScore` data (ping frequency, response patterns) is used only
  for ranking. It must never be exposed as a per-person leaderboard,
  "most interruptible" metric, or visible reputation score to any user —
  this is a hard constraint, not a v2-nice-to-have, given its effect on
  whether people keep cooperating with the outcome buttons.
- All tunable thresholds (§4.4) must be config, not hardcoded, since
  correct values will only be known after real usage.
