# NUS NextBus Telegram Bot

A Telegram bot for **NUS internal shuttle bus** arrivals and campus-aware route
planning. It answers "when's the next bus?" and "how do I get from A to B?" using
live NUS NextBus data and Google Maps for off-campus legs.

## Features

| Command | What it does |
|---------|--------------|
| `/arrivals <stop>` | Live bus times at a stop (or pick from a list) |
| `/all` | Every stop at a glance |
| `/go CLB to UTOWN` | Route between any two stops / buildings / locations |
| `/go` | Interactive: tap stops or share your location |
| `/bus A1` | A bus's route and operating schedule |
| `/fav` | Your saved stops ⭐ |

## Architecture

The code is split by responsibility:

| Module | Responsibility |
|--------|----------------|
| `bot.py` | Telegram handlers, conversation flow, message formatting, `main()` |
| `routing.py` | Campus route-planning engine (shuttle routing, transfers, companion-stop crossings, Bukit Timah gateways). No Telegram imports — testable in isolation. |
| `routes.py` | Static domain data: route stop-sequences, schedules, gateways, companion-stop pairs |
| `api.py` | NUS NextBus API client + arrival-time extrapolation |
| `planner.py` | Google Maps (directions, geocoding, places) + haversine |
| `stops.py` | Static stop list + `find_stop` / `nearby_stops` |
| `favourites.py` | SQLite-backed per-user favourites |

The NUS NextBus feed is a daily ~3am schedule snapshot served all day, so
`api._resolve_eta` infers a headway from the first few scheduled trips and
extrapolates the next arrival. It is an estimate, not live GPS.

## Setup

Requires **Python 3.12**.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt   # or requirements.txt for runtime only

cp .env.example .env          # then fill in the values (see below)
python bot.py
```

### Environment variables

See [`.env.example`](.env.example). Required:

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `NEXTBUS_API_URL` — `https://nnextbus.nus.edu.sg`
- `NEXTBUS_BASIC_AUTH` — Base64 `username:password` for the NUS NextBus HTTP
  Basic Auth (sourced from the NUS NextBus app)
- `GOOGLE_MAPS_API_KEY` — for route planning. **Restrict it** to the Directions,
  Geocoding, and Places APIs and to your server IP in the Google Cloud console.

Optional: `DB_PATH` (favourites DB location), `ADMIN_USER_ID` (Telegram user ID
allowed to run the `/debugplan` diagnostic).

`.env` is gitignored — never commit real secrets. If a secret is ever exposed,
rotate it (regenerate the bot token via BotFather, regenerate the Maps key).

## Development

```bash
ruff check .     # lint
pytest -q        # tests
```

CI (GitHub Actions, `.github/workflows/ci.yml`) runs both on every push and PR.

### Tests

The routing engine is covered by characterization / golden-output tests
(`tests/`) that pin current behaviour — run them before and after any change to
`routing.py` to catch regressions. Network calls (NUS NextBus, Google Maps) are
monkeypatched, so the suite needs no credentials and makes no requests.

## Deployment

Runs as a long-polling worker (`Procfile`: `worker: python bot.py`). A
`Dockerfile` (`python:3.12-slim`) is provided for container hosts.
