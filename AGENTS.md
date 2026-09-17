# AGENTS.md — Slack "Reach" Bot

## 1. Problem Statement

Slack workspaces have a recurring failure mode: when someone needs to reach a person urgently and a direct DM isn't working (recipient away, heads-down, Slack closed, or simply hasn't seen it), the fallback is posting in a shared channel. This interrupts every member of that channel — often dozens of people with no relationship to the person being sought — just to reach the 1–2 people who might actually know where they are or can relay the message.

**Core principle: minimize the number of people interrupted while still surfacing the humans most likely to help.** The bot does not locate the target user itself — it proposes a short, ranked list of candidates, and a human makes the final call on who to ping. The bot augments human judgment; it does not replace it.

## 2. Non-Goals

- Not a presence-tracking surveillance tool. It only reads what Slack's API already exposes (presence, public channel membership, public thread activity).
- Not an auto-escalation system. It never pings anyone without a human explicitly choosing to.
- Not trying to compute a single "best" person. It surfaces categorized candidates and lets the requester decide — no blended relevance score.
- Does not and cannot read other people's private DMs (Slack API does not expose this to bots). All relational signal is derived from public channels and threads the bot has access to.

## 3. Core Flow

The primary v1 interface is a direct-message conversation with Reach. A
normal DM message is acknowledged and answered in the DM (preferably in a
thread). `/reach <natural-language request>` is also supported in that DM.
When `/reach` is invoked elsewhere, the bot responds ephemerally with an
instruction to DM the app and does not run the old public-channel ranking
flow.

1. Requester triggers `/reach @X` (optionally with a short note, e.g. `/reach @X urgent - deploy is broken`).
2. Bot computes a small set of candidate people ("near" X) using the ranking signals below.
3. Bot replies **ephemerally** (visible only to the requester) with a compact, categorized list of candidates as interactive buttons.
4. Requester taps a candidate's "Ping" button.
5. Bot opens a small pre-filled modal with an editable one-line message.
6. Requester confirms/edits and sends. Bot delivers it as a DM to the candidate, on the requester's behalf, with clear framing (see §6).
7. Candidate can reply directly to the requester or the bot can offer a one-tap "I'll relay" / "Don't know" quick response.

The bot's own suggestion message is **always ephemeral** — it must never itself become a new source of channel clutter.

## 4. Ranking Signals

Do not combine signals into a single opaque score. Surface them as categorized evidence; let the human weigh relevance vs. reachability.

### 4.1 Presence (primary sort axis)

- Source: `users.getPresence` or presence change events.
- Used to split candidates into two top-level buckets: **Active now** / **Offline**.
- Presence is a _filter/sort key_, not a multiplier — an offline person with strong relational signal should still surface, just in the offline bucket, not be buried by a low score.

### 4.2 Shared channel membership, weighted by channel size

- For each channel X belongs to, list co-members.
- Weight inversely by channel member count — two people sharing a 6-person channel is a much stronger proximity signal than sharing `#general` with 200 members.
- Use the **smallest** mutual channel(s) as the primary structural signal ("probably works closely with X").

### 4.3 Thread co-occurrence, with recency decay

- Source: people who have replied in the same threads as X in public channels the bot can see.
- This is a _temporal/contextual_ signal, distinct from channel membership — it answers "was recently talking to X," not "works near X."
- Apply a recency cutoff (default: 7 days). A strong co-occurrence signal from a month ago should rank below a weaker one from this morning.
- Do not fold this into the channel-size score — keep it as a separate, clearly labeled signal category.

### 4.4 Candidate cap

- Cap each bucket (Active / Offline) at 2–3 candidates by default, configurable. The goal is "minimum viable interrupt" — a long list defeats the purpose even if the underlying ranking is accurate.

## 5. Output / Message Design

### 5.1 Guiding principle

The message is a decision surface, not a report. Buttons are the content; text is only load-bearing where a tap can't replace it. If reading the suggestion takes longer than just posting in the channel, the tool has failed at its core purpose.

### 5.2 Default (minimal) view

```
🔍 Reaching @X

🟢 Active now
[ Ping @A ]  [ Ping @C ]

⚪ Offline
[ Ping @B ]  [ Ping @D ]

[ ⓘ Why these people ]
```

No channel names, no member counts, no per-person justification in the default view. Names + one tap is the entire interface.

### 5.3 "Why" detail (opt-in only)

Tapping "ⓘ Why these people" opens a modal or posts a second ephemeral message with the underlying evidence per candidate, e.g.:

```
@A — active · smallest shared channel (#taparide-backend, 6 members)
@D — offline · replied in 3 threads with X this week (#taparide-infra)
```

This detail must never appear in the default message. It exists purely for debugging/trust-building, not for the common-case interaction.

### 5.4 Block Kit structure

- `header` — "Reaching @X"
- `context` — one line, only if a note was included with the command
- `section` + `actions` block per bucket (Active / Offline), buttons only
- `actions` block (single button) for "Why these people," rendered as a low-emphasis element (plain button, no `style` override)
- Use `button` elements exclusively for candidate selection — no `select` / dropdown menus. A dropdown costs an extra interaction (open, then choose); a button is one tap. This matters under time pressure.

### 5.5 Things to deliberately avoid

- No emoji or avatar per person — adds visual noise without decision value.
- No color-coded button styles, except reserving Slack's `danger` style for a genuine escalation action (e.g. "Escalate to manager"), if that feature is ever added. Never use color as decoration.
- No dividers between every sub-section — one divider between Active/ Offline is sufficient structure.
- No full sentences justifying a candidate in the default view.

## 6. Ping Interaction (button click)

Tapping "Ping @A" does **not** silently fire a message. It:

1. Opens a modal (`views.open`) with a single pre-filled, editable text field: a short templated message (e.g. "Hey, trying to reach X urgently — do you know if they're around?").
2. Requester can edit the line before sending.
3. On submit, bot sends it as a DM to the candidate, framed clearly so the recipient understands why they're being pinged and by whom — never as an anonymous or unexplained interruption.
4. Optionally: candidate gets one-tap quick replies ("I'll relay" / "Don't know") to close the loop fast.

This is the only "form" in the system — one field, pre-filled, editable. No multi-field forms under time pressure.

## 7. Architecture Notes

- **Trigger**: slash command (`/reach`), not passive event listening — this keeps the bot opt-in and avoids background surveillance concerns.
- **Data fetching**: presence + channel membership + thread replies via Slack Web API. Cache channel-membership data where reasonable to avoid hammering the API on every invocation; presence should be fetched fresh.
- **State**: minimal. The bot doesn't need persistent storage for v1 beyond short-lived interaction state (which candidate list belongs to which ephemeral message, for button handling).
- **Privacy boundary**: only public channels/threads the bot is installed into are used for signal. No private channel or DM content is read, consistent with Slack API limitations and this project's non-goals.

## 8. Build Priority (v1 → v2)

**v1 (ship this first):**

- Slash command → presence + channel-size ranking only (skip thread co-occurrence initially)
- Ephemeral Block Kit message, Active/Offline buckets, capped candidates
- Ping button → editable modal → DM send
- No "Why" detail yet if it slows down shipping

**v2 (after validating v1 against real usage):**

- Add thread co-occurrence signal with recency decay
- Add "Why these people" detail view
- Add quick-reply buttons for the pinged candidate
- Revisit candidate cap / bucket design based on real feedback — don't over-tune ranking before seeing where v1 picks bad candidates

## 9. Open Decisions (flag to requester if ambiguous during implementation)

- Tech stack: Python, using the locked `uv` workflow and the dependencies declared in `pyproject.toml`.
- Whether the bot sends the ping DM as itself ("relaying for Niel") or the requester sends it directly via a Slack-generated draft — confirm before building the send path.
- Default recency window for thread co-occurrence (currently proposed: 7 days) — adjustable, not fixed by this spec.

## 10. Project Development Guidelines

- Build the project in Python. Prefer the standard library and established project dependencies over introducing new packages without a clear need.
- Use a supported, modern Python version and keep runtime, development, and test dependencies explicitly declared.
- Use type hints for public interfaces and keep Slack API, ranking, and presentation concerns separated into focused modules.
- Keep secrets, signing tokens, and workspace-specific configuration out of source control. Load them from environment variables or a local, ignored configuration file.
- Treat Slack user data as sensitive. Request only the scopes required for the feature, preserve the public-channel privacy boundary, and do not log message contents or tokens.
- Operational logs are timestamped and default to stderr; `LOG_FILE` is an
  optional local file destination. Never log message contents, credentials,
  or API keys.
- Add focused automated tests for ranking, candidate caps, presence buckets, Block Kit payloads, and interaction validation as those components are implemented.
- Run the relevant formatter, linter, type checker, and tests before completing a change. Do not claim a change is complete when required validation is failing.
- Update this document and other directly related documentation when implementation decisions change the stated behavior.

## 11. Git and Commit Guidelines

- The repository is named `Reach'em`.
- Each feature or fix must be an atomic commit: one coherent behavior or correction per commit, with no unrelated cleanup mixed in.
- Commit messages should be short, imperative, and describe the user-visible or technical change.
- Do not create commits with a `Co-authored-by: Copilot` trailer or any other Copilot co-author attribution.
- Do not amend existing commits unless explicitly requested.
- Keep generated files, local environments, credentials, tokens, and other machine-specific artifacts out of commits.
