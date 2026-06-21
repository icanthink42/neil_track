# Neil Track Discord Bot

Discord bot that announces when Neil arrives in a new town or city based on Life360 location updates.

## Setup

1. Create a Discord bot, invite it to your server, and give it permission to send messages in the target channel.
2. Copy `.env.example` to `.env` and fill in the Discord values and `LIFE360_ACCESS_TOKEN`.
3. Install and run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m neil_track
```

## Configuration

All configurable values live in `.env`.

- `DISCORD_BOT_TOKEN`: Discord bot token.
- `DISCORD_CHANNEL_ID`: Channel where announcements are posted.
- `LIFE360_ACCESS_TOKEN`: Life360 Bearer token. This is the easiest no-password setup path.
- `LIFE360_USERNAME`: Optional label/account email. It is not used when `LIFE360_ACCESS_TOKEN` is set.
- `LIFE360_BASE_URL`: Life360 API base URL. Defaults to the current web client host.
- `LIFE360_IMPERSONATE`: Comma-separated browser profiles used for Life360 requests. Defaults to `chrome124,chrome120,chrome119`.
- `LIFE360_PASSWORD`, `LIFE360_CLIENT_BASIC`: Optional fallback values for fetching a token via the OAuth password grant.
- `LIFE360_MEMBER_ID`: Optional exact Life360 member id for Neil.
- `LIFE360_MEMBER_NAME`: Name fallback when `LIFE360_MEMBER_ID` is not set.
- `LIFE360_CIRCLE_ID`: Optional circle id to restrict searches. Setting it avoids an extra Life360 circle-list request.
- `POLL_SECONDS`: Poll interval.
- `STATE_FILE`: JSON file storing the last announced city/town.
- `ANNOUNCE_ON_STARTUP`: If `true`, announces the current place on first run.
- `NOMINATIM_USER_AGENT`: User agent sent to OpenStreetMap Nominatim reverse geocoding.
- `DISCORD_MESSAGE_TEMPLATE`: Announcement format. Available variables include `{name}`, `{place}`, `{city}`, `{state}`, `{country}`, `{route_percent}`, and `{route_percent_rounded}`.

The Life360 endpoints follow the dltHub Life360 context: base URL `https://www.life360.com/v3`, OAuth2 password grant at `/oauth2/token` when using the fallback credential flow, and Bearer-authenticated API requests for circles and members.
