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

### 6.2 Three-response cutoff

Once 3 people have responded "I know" for a given reach request, the
remaining open recipient messages for that request stop offering the
three buttons.

- The count is tracked per reach request (`reach_request_id`), never
  globally and never per message.
- Increment-and-check is atomic at the data layer — one statement that
  increments and returns the new count — so two near-simultaneous
  "I know" responses cannot both slip under the limit.
- Slack buttons have no native disabled state. "Disabling" means
  rewriting each still-open message for that request with `chat.update`
  into a single plain-text status line (e.g. "Someone already confirmed a
  location for this — thanks!"), which requires the channel and message
  timestamp of every sent recipient message (hand-picked or broadcast) to
  be stored on its ping record.
- The "I know" modal submit handler re-checks the count on submission, so
  anyone clicking after the limit was already hit sees the friendly
  "someone already found them" status instead of having their response
  recorded twice.
- The cutoff applies uniformly no matter how the recipient message was
  sent (hand-picked or via channel/workspace broadcast); broadcast
  recipients are never special-cased.

## 7. Delivery Modes

### 7.1 Default: bot-relay
The message is sent from the bot's identity, clearly framed so the
recipient understands who is actually asking and why (e.g. "Reach,
relaying for @Niel: ..."). This requires no additional per-user setup and
works for every requester by default.

### 7.2 Opt-in: send as yourself
A requester can instead choose to have the message sent from their own
Slack identity rather than the bot's. This requires:
- A one-time per-user OAuth authorization (user token, distinct from the
  bot's install-time token) — see Technical Spec §5 for the flow.
- A **Settings tab** (App Home) where a user can connect/disconnect this,
  see its current status, and understand what it grants (see §7.4).
- A graceful fallback: if a requester has not connected, their messages
  send via bot-relay automatically — this is never a blocking
  requirement to use `/reach`.

### 7.3 Why this is opt-in, not default
Sending as the user requires a materially more sensitive credential (a
user-scoped token capable of posting as that person generally, not just
through this flow) and a real consent step. Bot-relay already conveys "who
is asking and why" clearly without that additional security surface — so
it remains the default, and send-as-yourself is offered as a preference
for requesters who specifically want the message to look and feel like it
came directly from them.

### 7.4 Settings tab requirements
Accessible from the app's Home tab. Must show, at minimum:
- Current delivery mode (bot-relay / send-as-yourself) with a toggle.
- Connect/Disconnect action for the user token when relevant.
- A short, plain-language explanation of what granting this permission
  allows (posting messages as you, via this app, when you use `/reach`) —
  not just a scope name.
- Disconnecting immediately reverts the user to bot-relay for all future
  messages; it does not affect already-sent messages.

## 8. Build Priority (v1 → v2)

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

## 9. Open Decisions

- Tech stack: not yet locked (Node vs Python) — confirm before
  scaffolding.
- Default candidate cap and thread-recency window — tunable, proposed
  defaults given in §4, adjust after real usage.
- Whether "I know" should always prompt for a location/detail reply, or
  just log a positive outcome silently — leaning toward prompting, since
  the detail is the actually useful part for the requester, but confirm.