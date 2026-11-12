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

## Run on Unraid

1. Build the image on the Unraid box (Terminal), from a copy of this repo:
   ```sh
   docker build -t baltimore-showtimes:latest /path/to/baltimore-showtimes
   ```
2. Copy `unraid/baltimore-showtimes.xml` to
   `/boot/config/plugins/dockerMan/templates-user/my-baltimore-showtimes.xml`.
3. Docker tab → Add Container → pick **baltimore-showtimes**, fill in the keys you
   use, Apply. The page is on port 8080.

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
| `DISCORD_WEBHOOK_URL` | Alerts when a film on your Plex watchlist gets showings at a theater (once per film × theater). |
| `PUBLIC_URL` | How you reach the page, for links in Discord messages. |

Finding your Plex token: open any item in Plex Web → ⋯ → Get Info → View XML; the URL ends in `X-Plex-Token=...`.

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
