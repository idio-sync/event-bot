# Baltimore Showtimes — design

A self-hosted web page (Docker on Unraid) that collects upcoming movies and
showtimes from a chosen set of Baltimore-area theaters, labels screenings
(re-release, special event, advance, format), and ties into Plex and Radarr.

## Theaters and data sources

| Theater | Source | Notes |
|---|---|---|
| The Charles | Ready Theatre Systems schedule JSON, `app.formovietickets.com/schedules/scheduleV1/L467271.json` | All future shows; `ReleaseUS` (original release), sometimes a TMDB/IMDb link in `webSite` |
| The Senator | Same system, `L997115.json` | Same format |
| Cinemark Towson and XD | Cinemark site JSON, `/papi/theaters/1072/showtime-dates` and `/showtimes?date=` | Times in UTC; print type and attributes (XD, 3D, open caption, subtitles) |
| SNF Parkway | Eventive events API | **Off unless configured** — the only access is an API key; see "Open decisions" |
| Warehouse Cinemas Rotunda | Its GraphQL API sits behind a Cloudflare browser challenge | **Not implemented** — see "Open decisions" |

Each source is a module with one async `fetch(client) -> list[Showing]`.
A failing source keeps its last good data and reports the error on the
status page; it never wipes listings.

## Data flow

1. Every `REFRESH_MINUTES` (default 240) the app fetches all enabled sources.
2. Showings are upserted into SQLite (`/data/showtimes.db`). Each keeps
   `first_seen`, which powers "new" badges, the RSS feed and alerts. Future
   showings that disappear from a successful fetch are marked inactive.
3. Each distinct title is matched to TMDB once (cached): direct TMDB id or
   IMDb id from the source when present, otherwise a title search using a
   cleaned title (event suffixes, language notes, "3D" etc. stripped) and the
   source's release year as a hint.
4. Plex library/watchlist and Radarr library are refreshed (if configured).
5. Discord alerts are sent (see "Discord (optional)").

## Labels

Computed at query time so settings changes apply immediately.

- **Re-release**: the film's original release date (TMDB, else source) is at
  least N months before the screening. The filter bar offers presets
  (any older film = `RERELEASE_MONTHS`, default 9; 5+ years; 25+ years).
- **Special event**: source says so (Parkway), or title/attributes match event
  keywords (Q&A, panel, discussion, Met Opera, Fathom, anniversary, live,
  festival, sing-along, marathon, Exhibition on Screen, NT Live, ...).
- **Advance**: screening is before the film's release date.
- **Format**: 3D, XD, IMAX, 70mm/35mm, open captions, subtitled.
- **New**: first seen within the last 7 days.

## Page

One page with a filter bar (theaters, labels, re-release age, date range,
search, Plex filters) and two views:

- **By date**: each day, films with times per theater.
- **By film**: one card per film (poster, year, labels, Plex/Radarr status)
  listing every theater and time; "Opening soon" groups films whose first
  showing is in the future.

Also: `/feed.xml` (RSS of new showings, honours the same filters),
`/calendar.ics` (filtered showings), `/status` (last fetch per source).

## Plex (optional)

- Server (`PLEX_URL`, `PLEX_TOKEN`): movie libraries with TMDB guids and
  watched state → "In library" / "Watched" badges, "hide watched" and
  "not in library" filters.
- Watchlist (plex.tv, same account token): the main "tell me" list.
  Watchlist films get a badge, sort first, and drive Discord alerts.

## Radarr (optional)

- `RADARR_URL`, `RADARR_API_KEY`: library status badges (monitored /
  downloaded), expected digital release date, and an "Add to Radarr" button
  (quality profile and root folder from config, or Radarr's first ones).
- `/radarr/list.json`: films matching the given filters, in a format Radarr's
  Custom List / StevenLu list imports accept (`id`/`tmdbId`, `imdb_id`, `title`).

## Discord (optional)

Two modes. **Bot** (`DISCORD_BOT_TOKEN`, discord.py over the gateway, so no public
URL is needed) gives slash commands, `/follow` and DMs. **Webhook**
(`DISCORD_WEBHOOK_URL`) only posts to one channel. The bot runs in the same asyncio
loop as the web app (`app/bot.py`); command logic lives in `app/commands.py` and
returns plain reply data, so it is tested without Discord.

People: `discord_users` (name, chosen delivery) and `follows` (user × film key).
`/follow` autocompletes playing films plus TMDB search results, so films that are not
showing yet can be followed by TMDB id; without TMDB a typed title is followed by its
normalized title.

Settings page (`/settings`, stored as `prefs` in `kv`, env vars as starting values):
alerts channel, default delivery for followers (channel @mention by default, or DM),
channel feed kinds (new events, new re-releases), the admin's Plex watchlist alerts
(DM by default, channel, off), the admin's Discord user id (blank = bot owner), and
the return gap. It also lists users and follows, with a remove button.

Alerts (`app/alerts.py`) happen once per *run*: a film's showings at one theater
without a gap of the return days. `alert_runs` keeps `covered_until` and what was sent
this run: `event`, `rerelease`, `plex`, `follow:<user>`. Showings added inside a run
extend `covered_until` silently; one starting more than the gap later begins a new
run. Event/re-release feed items only fire when a run starts with newly listed
showings (or a film returns); follows and the Plex watchlist fire once per run,
including mid-run follows. `route()` turns due alerts into channel cards (feed items,
@mentions for followers who get alerts in the channel, Plex in the channel) and DM
cards. Runs are saved only after the channel post succeeds; a failed DM is logged
and not retried.

## Deployment

One Docker image (Python 3.13 slim, FastAPI + uvicorn), port 8080, volume
`/data`. Configuration is environment variables. An Unraid template is in
`unraid/`.

## Open decisions

- **Parkway**: Eventive has no public schedule feed; the Parkway site calls
  Eventive's API with a key embedded in its page. Using that key from another
  app needs the user's sign-off (or a key/feed from the Parkway).
- **Warehouse Cinemas Rotunda**: its API and Atom Tickets' page both serve a
  Cloudflare bot challenge to non-browser clients. Options: skip it, ask the
  theater for a feed, or run a real headless browser in the container (heavier,
  and it is still automating past a bot check — user's call).
