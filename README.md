# Reach'em

Reach'em is a Slack bot that helps a requester reach the smallest useful audience when a teammate is unavailable. Instead of posting in a shared channel and interrupting dozens of people, the bot allows requesters to manually select specific people to reach via direct messages, with optional broadcast scopes for channel or workspace-wide reach.

## Features

- **Manual recipient selection**: Requesters pick specific people to reach via a multi-user picker
- **Broadcast scopes**: Option to reach everyone in a channel or the entire workspace  
- **Per-recipient cleanup**: Respondents' messages are updated with a thank-you line after they respond
- **Three response types**: "I know", "I don't know", and "Custom message" for easy feedback
- **Response cutoff**: Stops asking for help after 3 people confirm they know where the target is
- **Privacy-first**: Never posts to public channels, reads private DMs, or displays affinity scores

## Slack App Setup

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click "Create New App"
2. Choose "From scratch" and give your app a name (e.g., "Reach'em")
3. Select your workspace

### 2. Configure Basic Information

- Add a display name and description
- Add an app icon (optional but recommended)

### 3. Add Bot Permissions

Navigate to **Bot Permissions** and add the following scopes:

- `commands` - Required for `/reach` slash command
- `chat:write` - Required to send DMs to recipients
- `channels:read` - Required to read public channel members for broadcast
- `groups:read` - Required to read private channel members for broadcast
- `users:read` - Required to resolve user profiles
- `im:write` - Required to send direct messages
- `im:history` - Required for message management

### 4. Enable Socket Mode

Navigate to **Socket Mode** and enable it. This is required for local development and simplifies deployment by avoiding the need for a public HTTP endpoint.

### 5. Install the App to Your Workspace

Navigate to **Install App** and click "Install to Workspace". This authorizes the bot with the permissions you configured.

### 6. Copy Tokens

After installation, you'll need two tokens:

- **Bot User OAuth Token**: Starts with `xoxb-...` (found in OAuth & Permissions → Bot User OAuth Token)
- **App-Level Token**: Starts with `xapp-...` (found in Basic Information → App-Level Token → Create Token)

Copy both tokens to your `.env` file as `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.

### 7. Apply Manifest

The included `manifest.json` file contains the required configuration. Apply it to your Slack app by navigating to the app's settings and importing the manifest, or manually verify that the settings match.

## Development

### Prerequisites

- Python 3.12+
- `uv` package manager
- PostgreSQL 15+ (optional, falls back to in-memory storage)
- Redis (optional, for presence caching)

### Setup

1. Clone the repository and navigate to the project directory
2. Install dependencies:

```bash
uv sync --extra dev
```

3. Copy the example environment file:

```bash
cp .env.example .env
```

4. Edit `.env` and add your Slack tokens:

```
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
SLACK_APP_TOKEN=xapp-your-app-token-here
```

5. Optionally configure database and Redis for production features:

```
DATABASE_URL=postgresql://user:password@localhost/reachem
REDIS_URL=redis://localhost:6379
```

### Running the Bot

For local development with Socket Mode:

```bash
uv run python app.py
```

The bot will connect to Slack via Socket Mode and respond to `/reach` commands.

For production deployments using HTTP mode (no Socket Mode):

```bash
uv run uvicorn reach_bot.app:api --host 0.0.0.0 --port 8000
```

In HTTP mode, configure Slack request URLs for `/slack/events` and set `SLACK_SIGNING_SECRET` in your environment.

### Testing

Run the test suite:

```bash
uv run pytest
```

Run linting and type checking:

```bash
uv run ruff check .
uv run mypy src
```

## Architecture

The bot uses a simple architecture with clear separation of concerns:

- **Handlers**: Slack interaction handlers for commands, views, and actions
- **Persistence**: PostgreSQL and in-memory storage for reach requests and pings
- **Rendering**: Slack Block Kit builders for modals and messages
- **Broadcast**: Background fan-out for channel/workspace broadcast scopes

The ranking/suggestion machinery is currently dormant; the bot uses manual recipient selection only. The ranking modules (`reach_bot.ranking`, `reach_bot.affinity`, `reach_bot.slack_provider`) are preserved for possible future use.

## Privacy & Security

- No tokens or message contents are logged
- The bot never posts to public channels
- The bot never reads private channels or DMs
- All suggestions are ephemeral (no persistent ranking displayed)
- Affinity scores are kept internal to ranking and never exposed as leaderboards

## Deployment

For production deployment:

1. Set `DATABASE_URL` and `REDIS_URL` environment variables
2. Choose between Socket Mode (easier, no public endpoint) or HTTP mode (requires public URL)
3. Configure your deployment to restart automatically on crashes
4. Ensure PostgreSQL and Redis are available and properly configured
5. Apply database migrations from `src/reach_bot/migrations/`

PostgreSQL migrations are versioned and should be applied in order.
