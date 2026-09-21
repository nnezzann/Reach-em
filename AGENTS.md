# AGENTS.md — Slack "Reach" Bot

## Git Commit Guidelines

When creating commits for this project:
- Do NOT add the "Co-Authored-By: Devin" message to commit messages
- Use conventional commit format with clear, descriptive messages
- Focus on the "why" rather than the "what" in commit messages
- Keep commits atomic and focused on single changes

## 1. Problem Statement

Slack workspaces have a recurring failure mode: when someone needs to reach
a person urgently and a direct DM isn't working (recipient away,
heads-down, Slack closed, or simply hasn't seen it), the fallback is
posting in a shared channel. This interrupts every member of that
channel — often dozens of people with no relationship to the person being
sought — just to reach the 1–2 people who might actually know where they
are or can relay the message.

**Core principle: minimize the number of people interrupted while still
surfacing the humans most likely to help.** The bot does not locate the
target user itself — it proposes a short, ranked list of candidates, and a
human makes the final call on who to message. The bot augments human
judgment; it does not replace it.

## 2. Non-Goals

- Not a presence-tracking surveillance tool. It only reads what Slack's API
  already exposes (presence, public channel membership, public thread
  activity).
- Not an auto-escalation system. It never messages anyone without a human
  explicitly composing and sending.
- Not trying to compute a single "best" person. It surfaces categorized
  candidates and lets the requester decide — no blended relevance score
  shown to the user.
- Does not and cannot read other people's private DMs (Slack API does not
  expose this to bots). All relational signal is derived from public
  channels and threads the bot has access to.

## 3. Core Flow (single-modal design)

The flow is a single modal with two stages, not a message-then-click
sequence — this removes an extra round trip and feels materially faster.

1. Requester types `/reach` (no arguments needed).
2. **Stage 1**: a modal opens immediately with one field — "Who are you
   trying to reach?" (a user picker).
3. On Stage 1 submit, the same modal **updates in place**
   (`response_action: "update"`) to show **Stage 2** immediately — no
   loading step, because no candidate computation runs (the ranking
   engine is dormant). Stage 2 (block layout in §5.2):
   - An optional "Add people who might know" multi-user picker — the
     primary, always-visible way to pick recipients.
   - A broadcast scope radio: just the people above (default), everyone
     in a channel, or everyone in the workspace. Choosing a channel
     reveals a channel picker, inserted/removed by re-rendering the
     modal on scope change.
   - A single editable message field, pre-filled with a default line
     built server-side from the resolved target's display name.
4. Requester hits **Send**. Hand-picked recipients are DM'd
   synchronously; if a non-"None" broadcast scope was chosen, a
   background fan-out DMs the channel's or workspace's human members
   (deduped against the hand-picked list, target and requester
   excluded, stopped early once the response cutoff is hit).
5. Each recipient receives the message with three reply actions:
   **I know** / **I don't know** / **Custom message** (see §6).
6. Replies route back to the requester (directly or via the bot,
   depending on the requester's delivery preference — see §8).

The bot's own Stage 1/2 modal is never visible to anyone but the
requester. Only the final composed message is seen by candidates, and only
the specific candidates selected — never a channel-wide post.

## 4. Ranking Signals

Do not combine signals into a single opaque score. Rank internally using
combined signals, but the *reasoning* is never shown by default (see §5.3)
— only names, pre-checked in relevance order.

### 4.1 Presence (primary sort/bucket axis)
- Source: `users.getPresence` or presence change events, cached briefly
  (Redis, short TTL) to avoid hammering the API.
- Splits candidates into **Active now** / **Offline** groups within the
  suggestion checkboxes.

### 4.2 Shared channel membership, weighted by channel size
- Co-members of channels the target belongs to, weighted inversely by
  channel size — a shared 6-person channel is a much stronger signal than
  a shared 200-person one.
- Smallest mutual channel = strongest structural signal ("probably works
  closely with the target").

### 4.3 Thread co-occurrence, with recency decay
- People who've replied in the same threads as the target in public
  channels, within a recency window (default 7 days, configurable).
- A distinct, separate signal from channel membership — temporal/
  contextual proximity rather than structural proximity. Don't blend the
  two into one number; keep them as separate inputs to ranking.

### 4.4 Learned affinity (from real outcomes — v2)
- As people respond "I know" / "Don't know" to being pinged for a given
  target, that outcome data trains a per-(target, candidate) affinity
  score over time (see Technical Spec §3 for schema).
- Only applied once a candidate has enough historical samples
  (default: 3+) — below that threshold, ranking relies purely on
  structural signals (§4.2–4.3). This avoids one noisy data point
  distorting the ranking for a new pair.
- Manually-added candidates (§3, Stage 2 "add anyone else" field) who
  later get a positive outcome are a *stronger* training signal than
  algorithmically-suggested ones, since a human explicitly vouched for
  them — weight accordingly in the learning job.

### 4.5 Candidate cap
- Suggested list capped at ~5–6 total across both presence groups by
  default (configurable) — the modal should be scannable in a couple of
  seconds, not a long scroll. The manual "add anyone else" field has no
  cap, since that's explicit human input, not algorithmic noise.

## 5. Modal & Message Design

### 5.1 Guiding principle
The modal is a decision surface, not a report. Every element should let
the requester act with minimal reading. If filling out the modal takes
longer than just posting in the channel, the tool has failed its purpose.

### 5.2 Stage 2 layout
Stage 2 renders immediately on Stage 1 submit (`response_action:
"update"`) — no loading view, since no candidate computation runs.
Blocks, in this order:
- **People picker** (block_id `candidates`): `multi_users_select`
  (action_id `candidates`), optional, placeholder "Add people who might
  know" — the primary way to pick recipients.
- **Broadcast scope** (block_id `broadcast_scope`): `radio_buttons`
  (action_id `scope_choice`) with exactly three options — "None (just
  the people above)" (default), "Everyone in a channel", "Everyone in
  the workspace". The block is `optional: false`; radio buttons always
  carry a value once rendered, and the default covers the "false start"
  case.
- **Channel picker** (block_id `broadcast_channel`):
  `conversations_select` (action_id `channel_choice`), filtered to
  public + private channels, present ONLY when the scope is "Everyone
  in a channel". Block Kit has no native conditional visibility, so a
  `scope_choice` block_actions listener re-renders the modal with
  `views.update`, inserting or removing this block and preserving
  everything already entered. The re-render is a single update with
  static content — no loading state.
- **Message field** (block_id `message`): single `plain_text_input`,
  initial value built server-side from the resolved target's display
  name (editable), sent identically to every recipient.

Submission requires at least one hand-picked person or a non-"None"
scope; a channel scope without a chosen channel is a validation error
on `broadcast_channel`, never a silent fallback to "None."

### 5.3 "Why" detail (opt-in only, v2)
No ranking justification is shown by default. If added later, gate it
behind an explicit affordance (e.g. a small help/info element) rather than
inline text — the default view stays minimal regardless.

### 5.4 Things to deliberately avoid
- No per-candidate justification text in the default suggestion list.
- No color-coded elements beyond what Slack's own `primary`/`danger`
  button styles are used for semantically (e.g. `danger` reserved for a
  genuine escalation action, if ever added).
- No multi-step modal beyond the two stages described — don't fragment
  further.

## 6. Recipient-Side Reply Actions

Every outgoing message includes three actions, regardless of delivery
mode (bot-relay or send-as-user — see §8):

- **✅ I know** — opens a small one-field modal (a single-line text
  input) asking where or how the target can be reached. On submit the bot
  records the positive outcome, relays that detail back to the requester
  as a DM, and runs the response-limit check (§6.2).
- **❌ I don't know** — a plain button, no modal. Records an explicit
  negative outcome (distinct from a timeout/no-response — see Technical
  Spec §6.3 for why this distinction matters to the learning model). It
  does not interact with the response limit.
- **💬 Custom message** — opens a one-field modal for a free-text reply,
  relayed back to the requester as a DM. Does not count toward the
  response limit.

These actions are the system's primary source of outcome data, which
feeds the learned affinity ranking over time (§4.4). Framing them clearly
and making them low-effort (one tap, or one tap + one short field) is
directly what makes the learning loop viable — if replying is annoying,
people won't use the buttons and the model never improves.

### 6.1 Recipient message format

- Mentions are built server-side from stored IDs (`<@target_id>`,
  `<@requester_id>`) — never taken from what the requester typed in the
  modal, since `plain_text_input` values are always literal text that
  Slack never resolves into mentions.
- The requester's composed message is included as free text alongside the
  mentions, not instead of them.
- Layout stays minimal: `section` blocks with mrkdwn text plus exactly
  one `actions` block for the three reply buttons. No decorative images
  or avatar-heavy layouts, and no emoji in body text beyond the
  functional markers on the button labels. Visual weight is reserved for
  functionally meaningful changes (e.g. the status line that replaces the
  buttons once the response limit is reached).

### 6.2 Response-limit closure: two independent pools

Message closure is governed by **two independent pools with two entirely
separate counter systems**. They share no counter, no query, and no sweep,
and there is deliberately **no cross-influence in either direction**: a
DM response never affects a broadcast message, and a broadcast response
never affects the DM counter or the DM sweep. This decoupling is explicit
so a future maintainer does not accidentally reintroduce a shared counter.

#### Pool 1 — manual/hand-picked DM recipients (hunt-wide global threshold)

- One global counter per reach request (`reach_request_id`), counting
  **only "I know" responses from manual/hand-picked DM recipients**.
- Once 3 such responses exist, every still-open **DM** message for that
  request stops offering the three buttons: each is rewritten with one
  `chat.update` into a single plain-text status line (e.g. "Someone
  already confirmed a location for this — thanks!").
- Increment-and-check is atomic at the data layer — one statement that
  increments and returns the new count — so two near-simultaneous "I
  know" responses cannot both slip under the limit.
- The "I know" modal submit handler re-checks the count on submission, so
  anyone clicking after the limit was already hit sees the friendly
  status instead of having their response recorded twice.
- **Broadcast messages must never be included in this sweep.** The sweep
  lookup excludes broadcast pings at the data layer
  (`get_unresponded_pings` filters `candidate_id = ''`), so the global
  threshold can only ever close manual DM messages.

#### Pool 2 — broadcast messages (per-message local thresholds)

Each broadcast message (channel/workspace post; one ping per posted
channel, `candidate_id = ''`) carries **its own local, independent
counters**, keyed by the same reach-request + channel + message-ts record
used for delivery — never tied to the DM global counter in any way:

- `local_know_count` — increments only on "I know" responses to **this
  message**.
- `local_total_count` — increments on **any** response type to **this
  message** ("I know", "I don't know", "Custom message").

The message closes (buttons removed, replaced with the same status line,
single `chat.update` call) when **either**:

- (a) `local_know_count` reaches 3, or
- (b) `local_total_count` reaches 5,

whichever happens first. Concretely: 3 "I know" responses alone close it
even if total responses are fewer than 5; alternatively, if "I know"
responses trickle in slowly, up to 2 "I don't know"/"Custom message"
responses are tolerated before the 5-total cap forces closure regardless
of how many were "I know" at that point. Both counts keep incrementing
after that; the **first response that makes either condition true is the
closing trigger** — both conditions are checked after every single
increment, and a message never silently exceeds 5 total or 3 "I know"
while still showing live buttons.

Both counts increment atomically (the same atomic increment-and-check
pattern as the DM global counter, applied per-message instead of
hunt-wide) so two near-simultaneous responses on the same broadcast
message cannot both read a stale count and neither triggers the closure
they should.

A first response — or any response below both local thresholds — **never
closes** a broadcast message, and broadcast responses never touch the
DM-side global counter (`increment_known_count` is never called on their
path).

### 6.3 Per-responder acknowledgment & cleanup

Acknowledgment differs by pool, because a DM message has one recipient
while a broadcast message is shared by everyone in the channel:

**Pool 1 — manual DM recipients (visible in-place cleanup, unchanged):**

* After recording the response, the handler uses `chat.update` to remove
  the `actions` block from that responder's own DM message and replace it
  with a short thank-you line (e.g. "Thanks for your response!").
* This cleanup uses the stored `channel` and `message_ts` from the ping
  record, just like the threshold sweep does.
* The thank-you formatting is shared across all three response types to
  maintain visual consistency.

**Pool 2 — broadcast recipients (ephemeral-only acknowledgment):**

* Every response type on a broadcast message gets a **per-responder
  ephemeral acknowledgment** via `chat.postEphemeral` — visible only to
  that responder, never as a visible message edit.
* For button responses ("I don't know"), the channel comes from the
  block_actions payload (`channel_id`).
* For modal responses ("I know", "Custom message"), the channel is
  threaded through the modal's `private_metadata` at render time, since
  modal submissions carry no channel context of their own.
* Broadcast messages are **never** edited in place for an individual
  responder: many people share the same message, so the only visible
  edit is the closure status line from a local threshold trip (§6.2,
  Pool 2).

The purpose in both pools is the same: give responders immediate feedback
that their response was registered, preventing confusion or duplicate
responses.

#### Duplicate-click guard

All three response handlers check for an existing outcome before recording
or applying cleanup:

* If an outcome already exists for that ping (regardless of type), the
  handler responds with a friendly note and skips both recording and any
  message update. On a broadcast message this is an **ephemeral** note
  (no visible re-update of a closed message); on a DM it is the existing
  friendly DM note.
* This guard prevents double-recording and double-updating if a recipient
  clicks a stale button or submits a modal multiple times.
* The guard is type-agnostic: it checks for any response, not just the
  specific type being submitted.

#### Threshold sweep interaction

The two pools interact with sweeps as follows:

* The **DM global sweep** (Pool 1) replaces open DM messages with the
  cutoff status when the hunt-wide 3-"I know" threshold trips. It skips
  DM messages that have already been closed by ANY response type (the
  sweep excludes pings with any recorded outcome), and it **excludes
  broadcast pings entirely** — it can never touch or close a broadcast
  message.
* The **broadcast closure** (Pool 2) is not a sweep: the single closing
  response applies one `chat.update` to its own message only, after
  checking its local counters. The triggering responder's message is
  never double-updated because the closure check happens before the
  ack/update sequence.

## 8. Delivery Modes

### 8.1 Default: bot-relay
The message is sent from the bot's identity, clearly framed so the
recipient understands who is actually asking and why (e.g. "Reach,
relaying for @Niel: ..."). This requires no additional per-user setup and
works for every requester by default.

### 8.2 Opt-in: send as yourself
A requester can instead choose to have the message sent from their own
Slack identity rather than the bot's. This requires:
- A one-time per-user OAuth authorization (user token, distinct from the
  bot's install-time token) — see Technical Spec §5 for the flow.
- A **Settings tab** (App Home) where a user can connect/disconnect this,
  see its current status, and understand what it grants (see §7.4).
- A graceful fallback: if a requester has not connected, their messages
  send via bot-relay automatically — this is never a blocking
  requirement to use `/reach`.

### 8.3 Why this is opt-in, not default
Sending as the user requires a materially more sensitive credential (a
user-scoped token capable of posting as that person generally, not just
through this flow) and a real consent step. Bot-relay already conveys "who
is asking and why" clearly without that additional security surface — so
it remains the default, and send-as-yourself is offered as a preference
for requesters who specifically want the message to look and feel like it
came directly from them.

### 8.4 Settings tab requirements
Accessible from the app's Home tab. Must show, at minimum:
- Current delivery mode (bot-relay / send-as-yourself) with a toggle.
- Connect/Disconnect action for the user token when relevant.
- A short, plain-language explanation of what granting this permission
  allows (posting messages as you, via this app, when you use `/reach`) —
  not just a scope name.
- Disconnecting immediately reverts the user to bot-relay for all future
  messages; it does not affect already-sent messages.

## 9. Build Priority (v1 → v2)

**v1:**
- Two-stage modal (`/reach` → target picker → suggested + manual +
  message).
- Static ranking only (presence + channel-size); skip thread
  co-occurrence and learned affinity initially.
- Bot-relay delivery only — no send-as-user, no Settings tab yet.
- Reply buttons (I know / Don't know / Reply with more) wired to log
  outcomes, even if the learning job isn't consuming them yet.

**v2:**
- Thread co-occurrence signal with recency decay.
- Learned affinity scoring, blended into ranking once sample thresholds
  are met.
- Send-as-yourself delivery mode + per-user OAuth flow + Settings tab.
- "Why these people" opt-in detail view.

## 10. Open Decisions

- Tech stack: not yet locked (Node vs Python) — confirm before
  scaffolding.
- Default candidate cap and thread-recency window — tunable, proposed
  defaults given in §4, adjust after real usage.
- Whether "I know" should always prompt for a location/detail reply, or
  just log a positive outcome silently — leaning toward prompting, since
  the detail is the actually useful part for the requester, but confirm.

## 11. Deployment (Render free tier)

Deployment target: a **Render free-tier Web Service**, with **Socket Mode
as the sole Slack transport**. HTTP mode (Slack's Request-URL / event-
verification flow, `SLACK_SIGNING_SECRET`) was considered and explicitly
rejected: Slack talks to the app entirely over the existing Socket Mode
connection, and no public webhook endpoint is involved.

### 11.1 Why a Web Service, not a Background Worker

Render's free plan does not offer Background Workers. A long-lived
process whose only job is holding a Socket Mode connection is therefore
undeployable on the free tier, so the service is declared `type: web` in
`render.yaml` (on a paid tier a proper Background Worker would be the
right shape). A Web Service must bind `$PORT` and answer HTTP requests
to stay up — that is the *only* reason any HTTP server exists in this
project.

### 11.2 Health server alongside Socket Mode

`src/reach_bot/app.py` boots two things concurrently in one process, via
`asyncio.gather()`:

- The existing `AsyncSocketModeHandler` — unchanged, and still the only
  path by which Slack events reach the app.
- A minimal `aiohttp` server bound to `0.0.0.0` on the `PORT` env var
  (set by Render; never hardcoded), exposing a single `GET /health`
  route that returns `200 {"status": "ok"}`.

The health server carries no Slack traffic, needs no Slack signature
verification, and is not part of Bolt's request flow — it exists purely
so Render's service-level health check and the external keep-alive ping
(§11.4) have something to hit. It is free-tier plumbing, not
application surface.

### 11.3 Environment variables

Required, set as secrets in the Render dashboard and never committed to
the repo or `render.yaml`:

- `SLACK_BOT_TOKEN` (`xoxb-…`) — the bot token.
- `SLACK_APP_TOKEN` (`xapp-…`) — the app-level token for Socket Mode.
  Still required in this deployment (unlike an HTTP-mode deployment, it
  is not optional here — it authenticates the Socket Mode connection
  itself).
- `DATABASE_URL` — Supabase/Postgres connection string, read exactly as
  before (`Settings.database_url` → `PostgresRepository`;
  falls back to the in-memory repository when unset).

`SLACK_SIGNING_SECRET` is **not** needed: it only applies to HTTP-mode
request verification, which this deployment does not use. `PORT` is set
automatically by Render.

### 11.4 GitHub Actions keep-alive

`.github/workflows/keep-alive.yml` pings `${RENDER_APP_URL}/health`
(`RENDER_APP_URL` is a repo variable, not a secret, since the app URL is
not sensitive) with `curl -f` on a `*/10 2-21 * * *` cron — every 10
minutes, but only during 02:00–21:59 UTC. A non-200 or failed ping fails
the workflow run so keep-alive breakage is visible in the Actions tab.

- **Cadence:** the 10-minute interval is deliberately tighter than
  Render's ~15-minute free-tier idle window, leaving margin for GitHub
  Actions' own scheduling jitter (cron is not guaranteed to fire exactly
  on time).
- **Quiet window and timezone basis:** GitHub Actions cron is always
  evaluated in UTC. The excluded hours (22:00–01:59 UTC) correspond to
  00:00–04:00 in the app owner's local timezone (UTC+2). Conversion
  basis: local 00:00–04:00 at UTC+2 equals 22:00–02:00 UTC, so active
  pinging covers 02:00–22:00 UTC — exactly the `2-21` cron hour range.
  Anyone changing the owner's timezone or the desired quiet hours must
  redo that UTC+2 → UTC conversion rather than editing the cron hours by
  feel.
- **Why the quiet window exists at all:** Render's free plan caps usage
  at 750 total instance-hours per month. Pinging continuously 24/7 would
  consume nearly all of that budget on keep-alive traffic alone (~720
  instance-hours in a 30-day month, ~744 in a 31-day month), leaving no
  margin. The ~4-hour daily quiet window keeps monthly usage comfortably
  under the cap (~600–620 instance-hours/month).
- **Relationship to the broadcast fan-out:** this is an *unofficial*
  workaround — Render does not officially support pinging to avoid
  idle spin-down. Uptime matters beyond just avoiding slow responses:
  a spin-down also **silently kills any in-flight background broadcast
  fan-out task** (the fan-out is an in-process `asyncio` task per the
  core flow in §3), so recipients mid-fan-out would never be pinged and
  no error would be visible anywhere.
- **Accepted tradeoff (deliberate, not a bug):** during the 00:00–04:00
  local quiet window the app *will* be allowed to spin down, so any real
  Slack interaction in that window hits a cold start (tens of seconds)
  on its first request rather than an instant response. This is a
  deliberate cost/uptime tradeoff for staying on the free tier. Flag to
  revisit if the cold-start window turns out to matter in practice:
  narrow the quiet window, or move off the free tier (§11.6).

### 11.5 Reconnection

Bolt's `AsyncSocketModeHandler` already reconnects automatically when
the underlying process restarts or the container wakes from a Render
idle cycle — a fresh "session established" log line appears on every
process start, and this has been observed working. **Verify, don't
rebuild:** no custom reconnection/retry logic is layered on top of it.

### 11.6 Positioning: free-tier workaround, not a stance

All of §11 — the health server, the keep-alive cron, the accepted
cold-start window — is a **free-tier workaround rather than a permanent
architectural stance.** If the project ever moves to a paid tier where a
proper Background Worker is available, or to the earlier-considered
Oracle Cloud VM path, the health server and the keep-alive machinery can
be removed entirely and the app runs as a plain Socket Mode process.