# Baltimore Showtimes

A self-hosted page listing upcoming movies and showtimes at the theaters you pick,
with filters for re-releases, special events and advance screenings, plus
Plex, Radarr and Discord tie-ins. Design notes: [docs/design.md](docs/design.md).

## Theaters

| Theater | ID for `THEATERS` | Enabled by default | Schedule handling |
|---|---|---|---|
| The Charles | `charles` | Yes | Full RTS schedule |
| The Senator | `senator` | Yes | Full RTS schedule |
| SNF Parkway | `parkway` | Yes | Automatic browser-key discovery; partial Eventive snapshot |
| Cinemark Towson and XD | `cinemark-towson` | No | Direct HTTP; partial snapshot within the configured horizon |
| Warehouse Cinemas Rotunda | `warehouse-rotunda` | No | Public website data; partial snapshot |

Parkway, Cinemark and Warehouse all retain cached screenings missing from later
snapshots because complete coverage is not guaranteed. See [Theater sources and
coverage](#theater-sources-and-coverage) for access requirements and limitations.

## Quick start with Docker Compose

1. Copy `.env.example` to `.env` and edit the values you use. To enable all five
   theaters, set:

   ```dotenv
   THEATERS=charles,senator,parkway,cinemark-towson,warehouse-rotunda
   ```

2. Run `docker compose up -d --build` from the repository root. Compose builds the
   local source and stores the database under `./data`.
3. Open `http://localhost:8080` (or your server's address) and check **Status** for
   fetch errors and coverage warnings. Recreate the container after changing
   environment settings with `docker compose up -d`.

The image and CI tests target Python 3.13. The container runs as UID 99, GID 100;
a custom data mount must be writable by that user.

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
   use, Apply. The page is on port 8080. Under advanced settings, set **Theaters**
   to `charles,senator,parkway,cinemark-towson,warehouse-rotunda` to enable all five.
3. To update: Docker tab → **Check for Updates** → apply the update when one shows.

## Settings

All settings are environment variables with defaults; no variable is required
for a basic setup. Optional integrations need their respective credentials.

| Variable | What it does |
|---|---|
| `TZ` | Display and source timezone. Default `America/New_York`. |
| `DATA_DIR` | Database directory; default `/data`. Compose mounts `./data` there. For a local Python run, set a writable directory such as `./data`. |
| `TMDB_API_KEY` | v3 key or v4 read token. Posters, original release years (better re-release detection), Plex/Radarr matching. Strongly recommended. |
| `THEATERS` | Comma-separated: `charles,senator,parkway,cinemark-towson,warehouse-rotunda`. Default `charles,senator,parkway`. |
| `PARKWAY_EVENTIVE_KEY` | Optional override for the automatically discovered website browser key. An explicit key disables discovery. |
| `PARKWAY_EVENTIVE_BUCKET` | Parkway program bucket; default `67fb1b7ba2fc1ffee7cc9d0c`. |
| `CINEMARK_DAYS_AHEAD` | Fetch advertised Cinemark dates through this many days ahead; default 365, minimum 1. |
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

## Theater sources and coverage

Enable additional theaters through `THEATERS`, for example
`charles,senator,parkway,cinemark-towson,warehouse-rotunda`. Existing defaults
are unchanged. Unknown theater names are rejected at startup.

- **Parkway**: automatically discovers the browser API key published by
  `snfparkway.org` on each refresh. No manual key is needed. Set
  `PARKWAY_EVENTIVE_KEY` to override discovery; an explicit key is used as-is,
  without silently falling back to the website. `PARKWAY_EVENTIVE_BUCKET`
  selects the program bucket (default `67fb1b7ba2fc1ffee7cc9d0c`).
  Authentication uses headers; discovered keys are kept in memory and never
  saved to the database or committed. If the page changes or discovery fails,
  cached showtimes remain and Status reports the failure. Eventive pagination
  and complete coverage are unverified, so successful Parkway results are also
  marked partial.
- **Cinemark Towson**: the existing JSON parser uses a standard-library HTTP
  transport. This succeeded in development where HTTPX received 403 responses,
  but access can vary by network. `CINEMARK_DAYS_AHEAD` sets the fetch horizon,
  default 365 days (minimum 1). Only dates advertised by Cinemark within that
  horizon are requested. A failed day aborts that theater's refresh and preserves
  its prior data. Successful results are still marked partial.
- **Warehouse Rotunda**: reads Movie/ScreeningEvent JSON-LD from the theater's
  own homepage and follows up to 40 same-origin movie links per refresh for
  additional screenings. Unavailable movie pages and the discovery cap appear
  in coverage warnings. It does not depend on the previously blocked GraphQL
  endpoint. Website pages can expose
  only part of the schedule; the Status page reports this limitation.

Limited or unverified schedule snapshots add/update screenings without
deactivating missing ones. This prevents a short page or fetch horizon from
removing valid future listings, but a canceled screening may remain until its
start time passes. Full snapshots can deactivate missing screenings. A verified
complete empty snapshot clears future listings; ambiguous empty results retain
last-good data. Check **Status** for access errors, coverage warnings and the
last successful fetch. A coverage warning can accompany a successful fetch and
a nonzero count; it does not necessarily mean access failed.

Cinemark and Warehouse require no headless browser or new Python packages.
Validate fetching from your Unraid network before relying on alerts; no claim
of complete Warehouse coverage is made.

## Development

Use Python 3.13 to match the Docker image and CI. Run these commands from the
repository root. Local Python runs do **not** automatically load `.env`; Compose
loads that file. Set any local integration credentials in your shell environment.

Linux/macOS:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
DATA_DIR=./data .venv/bin/python -m uvicorn app.main:app_factory --factory --port 8080
```

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
$env:DATA_DIR = "./data"
.\.venv\Scripts\python.exe -m uvicorn app.main:app_factory --factory --port 8080
```

Both commands serve the app at `http://localhost:8080`. The server fetches enabled
theaters on startup and periodically afterward; configured Discord integrations
can send alerts.
