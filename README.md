# Baltimore Showtimes

A self-hosted page listing upcoming movies and showtimes at the theaters you pick,
with filters for re-releases, special events and advance screenings, plus
Plex, Radarr and Discord tie-ins. Design notes: [docs/design.md](docs/design.md).

## Theaters

| Theater | Status |
|---|---|
| The Charles | Works |
| The Senator | Works |
| SNF Parkway | Needs `PARKWAY_EVENTIVE_KEY` (see below) |
| Cinemark Towson and XD | Opt-in; currently blocked (see below) |
| Warehouse Cinemas Rotunda | Not implemented (see below) |

## Publishing the image

GitHub Actions (`.github/workflows/docker.yml`) runs the tests and pushes
`<dockerhub-user>/event-bot` to Docker Hub on every push to `main`
(tags `latest` and `sha-<commit>`; a `v1.2.3` git tag also publishes `1.2.3`).
It needs two repository secrets (GitHub → Settings → Secrets and variables → Actions):

- `DOCKERHUB_USERNAME`: your Docker Hub username
- `DOCKERHUB_TOKEN`: a Docker Hub access token (Account settings → Personal access
  tokens → Generate, with Read & Write)

## Run on Unraid

1. Copy `unraid/event-bot.xml` to
   `/boot/config/plugins/dockerMan/templates-user/my-event-bot.xml` (it pulls
   `idiosync000/event-bot:latest`).
2. Docker tab → Add Container → pick **event-bot**, fill in the keys you
   use, Apply. The page is on port 8080.
3. To update: Docker tab → **Check for Updates** → apply the update when one shows.

Or anywhere with Docker Compose: copy `.env.example` to `.env`, fill it in, `docker compose up -d --build`.

## Settings

All environment variables; everything but `TZ` is optional.

| Variable | What it does |
|---|---|
| `TMDB_API_KEY` | v3 key or v4 read token. Posters, original release years (better re-release detection), Plex/Radarr matching. Strongly recommended. |
| `THEATERS` | Comma-separated: `charles,senator,parkway,cinemark-towson`. Default `charles,senator,parkway`. |
| `REFRESH_MINUTES` | Fetch interval, default 240 (min 30). |
| `RERELEASE_MONTHS` | What "any older film" means in the re-release filter. Default 9 months; the page also offers 5+ and 25+ years. |
| `PLEX_URL`, `PLEX_TOKEN` | Library and watched badges, "Hide watched" / "Not in Plex" filters. The token also reads your plex.tv watchlist. |
| `RADARR_URL`, `RADARR_API_KEY` | Radarr status badges and the "+ Radarr" button. |
| `RADARR_QUALITY_PROFILE`, `RADARR_ROOT_FOLDER` | For films added from the page; default to Radarr's first ones. |
| `DISCORD_BOT_TOKEN` | Discord bot: slash commands, `/follow`, DMs (see below). |
| `DISCORD_WEBHOOK_URL` | Simpler alternative to the bot: channel posts only. Ignored when a bot token is set. |
| `DISCORD_ALERTS`, `ALERT_RETURN_DAYS` | Starting values for the Settings page (feed kinds, Plex watchlist on/off; days before a returning film alerts again). After you save Settings, the page wins. |
| `PUBLIC_URL` | How you reach the page, for links in Discord messages. |

Finding your Plex token: open any item in Plex Web → ⋯ → Get Info → View XML; the URL ends in `X-Plex-Token=...`.

## Discord

### Bot (recommended)

1. <https://discord.com/developers/applications> → **New Application** → name it
   (e.g. "Showtimes") → **Bot** → **Reset Token** → copy it into `DISCORD_BOT_TOKEN`.
   No privileged intents are needed.
2. Restart the container, open the app's **Settings** page, use **Invite it to a
   server**, and pick your server.
3. Back on Settings: choose the alerts channel, put in your Discord user ID (makes
   you admin and the recipient of Plex watchlist DMs), Save.

Commands (replies to the listing commands are visible to the channel; the personal
ones only to the person using them):

| Command | |
|---|---|
| `/showtimes [theater] [when]` | What's playing: today, tomorrow, this weekend, next 7 or 30 days |
| `/film <title>` | Every showtime for one film (autocompletes) |
| `/events`, `/rereleases`, `/coming` | Special events, re-releases, films that haven't opened, next 3 months |
| `/follow <title>` | Alert me when it plays — autocomplete includes films not showing anywhere yet |
| `/unfollow <title>`, `/following` | Manage your list; `/following` shows when each is next on |
| `/alerts <delivery>` | Channel @mention, DM, or the server default |
| `/refresh` | Fetch now (admin only) |

### Alerts

After each refresh the app sends a card per film × theater: title and year, why,
showtimes, ticket link, poster.

| Who | When | Where |
|---|---|---|
| Followers (`/follow`) | A followed film is playing; also if you follow it mid-run | Channel @mention by default; DM per person (`/alerts`) or as the server default (Settings) |
| Everyone (channel feed) | A newly listed special event or re-release | The alerts channel; each kind can be turned off in Settings |
| You (Plex watchlist) | A film on your Plex watchlist is playing (needs `PLEX_TOKEN`, TMDB) | DM to you by default; or the channel, or off |

Each film at each theater alerts once per *run*: its showings there with no gap of
the Settings page's "alert again after" days (default 30). Extra showtimes during a
run stay quiet; a film coming back after a longer break alerts again, marked "Back
after a break". A theater's first fetch doesn't announce its existing events and
re-releases, so turning the app on doesn't flood the channel. If the channel post
fails, nothing is recorded and everything is retried next refresh; a person with
DMs closed is skipped.

The Settings page also lists who uses the bot and every follow (with a Remove
button). The web pages have no login — keep the port on your LAN.

### Webhook only

Without a bot token, `DISCORD_WEBHOOK_URL` posts the same channel cards (followers
@mentioned, Plex watchlist in the channel). There are no slash commands in this
mode, so follows only exist if they were made while a bot was set up.

## Feeds and Radarr list

Every link in the toolbar keeps the page's current filters:

- **RSS** (`/feed.xml`): newly listed screenings.
- **Calendar** (`/calendar.ics`): subscribe in Google/Apple Calendar.
- **Radarr list** (`/radarr/list.json`): in Radarr, Settings → Import Lists → + →
  *Custom Lists*, and paste the URL (for example
  `http://<unraid-ip>:8080/radarr/list.json?kinds=watchlist` or `?kinds=rerelease`).

## Blocked theaters

- **Parkway** sells through Eventive, which has no public feed. Its website reads the
  schedule from Eventive's API using a key embedded in the page. Setting
  `PARKWAY_EVENTIVE_KEY` to that key makes this app read the same list. It is the
  Parkway's key, not yours, so this is your call; asking the Parkway
  (info@snfparkway.org) for permission or a feed is the clean route.
- **Cinemark** and **Warehouse Cinemas** put their schedule APIs behind Cloudflare bot
  checks that reject this app. Getting past them means disguising the app as a
  browser or running a real headless browser in the container, which this app
  deliberately doesn't do. Cinemark's code is here and will work if the block lifts
  (add `cinemark-towson` to `THEATERS` and check the Status page).

## Development

```sh
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # bin/ on Linux/macOS
.venv/Scripts/python -m pytest
DATA_DIR=./data .venv/Scripts/python -m uvicorn app.main:app_factory --factory --port 8080
```
