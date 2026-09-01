# mta-subway-status — MCP server

Lets Claude answer questions like "any delays on the E right now" and
"when's the next 6 train at Union Sq" for the NYC subway, from any
Claude client (web, mobile, desktop) — by exposing three tools backed
by MTA's live GTFS-realtime feeds:

- **`get_subway_alerts(lines)`** — current service alerts: delays,
  planned repair work, reroutes, "no service" notices — optionally
  filtered to specific lines (e.g. `["7", "E", "M", "G"]`).
- **`find_stations(query)`** — look up stations by name (e.g. "union
  sq") to see which lines serve them and get their stop_ids. Several
  distinct real-world stations can share a name (four different "103
  St"s, for instance) — all matches come back so you can disambiguate
  by route.
- **`get_next_trains(station, lines, limit)`** — live predicted
  arrival times at a station, across the lines that serve it (or a
  subset you specify).

**What it deliberately doesn't do:** no multi-leg trip planning
("route me from A to B") — MTA doesn't publish a subway directions API;
their app's trip planner runs OpenTripPlanner behind the scenes, not
something exposed publicly. Building that would mean computing routes
ourselves on top of the static GTFS graph, which is a bigger project
than these three tools.

**No MTA API key needed** — subway GTFS-realtime feeds have been
open/keyless since 2024. If you later add bus data, that still requires
a free key from https://bustime.mta.info/wiki/Developers/Index.

## Tested so far

- `test_parsing.py` — validates the protobuf → dict parsing logic for
  both alerts and trip updates against synthetic feeds, plus
  `search_stations()` against the real bundled station data (passing).
- `fetch_alerts()` against the **real, live** MTA feed — confirmed
  working (Sept 2026). This caught a real bug: the feed URL's
  `camsys/subway-alerts` path segment needs its `/` percent-encoded
  as `%2F` (API Gateway treats it as one opaque path parameter);
  without that, every request 403'd with "Missing Authentication
  Token" — a misleading error that looks like a missing API key but
  actually just meant the route didn't match. Fixed in `mta_feeds.py`.
- Full MCP round trip verified locally on Python 3.14: `server.py
  --http` boots, responds to `initialize`, and a real `tools/call` for
  `get_subway_alerts` (lines `["7","E","M","G"]`) returns live,
  correctly filtered alert data end-to-end. `find_stations` verified
  the same way (fully offline — no network dependency).
- The 8 per-line trip-update feed URLs (`TRIP_UPDATE_FEED_URLS` in
  `mta_feeds.py`) follow the exact same host + `%2F`-encoding pattern
  already confirmed live for the alerts feed, and match what
  established NYC subway GTFS-rt clients use. They could **not** be
  live-tested from the environment these tools were built in (its
  network policy blocks `api-endpoint.mta.info` outright, the same
  host the already-working alerts feed uses) — verify `get_next_trains`
  against the real feed once deployed.

Note: `server.py` needs Python **3.10+** (the `mcp` package's
minimum) — the type hints in `server.py` also rely on this, since
unlike `mta_feeds.py` it doesn't have `from __future__ import
annotations`.

## 1. Try it locally first

```bash
pip install -r requirements.txt
python server.py --http
```

In another terminal:
```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_subway_alerts","arguments":{"lines":["7","E","M","G"]}}}'
```

If that returns real alert data (or an empty list, if everything's
running fine), the feed URL and parsing are working for you live.

## 2. Deploy somewhere public (Fly.io example)

Claude's connectors reach your server from Anthropic's cloud, not from
your phone or laptop directly — so it needs a real public HTTPS URL,
not just `localhost`.

```bash
# one-time setup
brew install flyctl        # or see fly.io/docs/flyctl/install
fly auth login

# from this project directory
fly launch --no-deploy      # accept defaults, or name it e.g. mta-mcp
fly deploy
```

Fly will build the Dockerfile in this folder and give you a URL like
`https://mta-mcp.fly.dev`. Your MCP endpoint is `https://mta-mcp.fly.dev/mcp`.

(Render.com's free "Web Service" tier works the same way if you'd
rather not use Fly — point it at this repo, it'll pick up the
Dockerfile automatically.)

## 3. Connect it in Claude

1. In the Claude app: **Settings → Connectors → Add custom connector**
2. Name: `MTA Subway Status` (or whatever you like)
3. Remote MCP server URL: `https://<your-app>.fly.dev/mcp`
4. No OAuth needed for v1 — leave Advanced settings blank
5. Add, then enable it for a conversation

Once connected, this works the same from your phone as from your
laptop — the request goes Claude mobile app → Anthropic's servers →
your Fly.io server → MTA, and back.

## 4. Try asking Claude things like

- "Any delays on the E, M, or G right now?"
- "Are any subway lines not running today?"
- "What's going on with the 7 train?"
- "When's the next 6 train at Union Sq?"
- "Which lines stop at 103 St?"

## Known limitations / next steps if you want to extend this

- Alert text comes straight from MTA's feed — no rewriting or
  summarizing happens server-side; Claude does that in conversation.
- No caching — every tool call hits MTA fresh. Fine at personal-use
  volume; add a short in-memory cache if you start calling it a lot
  (trip-update feeds refresh roughly every 30s server-side, so caching
  more aggressively than that just adds staleness for no benefit).
- If MTA ever moves the alerts or trip-update feed URLs, update
  `ALERTS_FEED_URL` / `TRIP_UPDATE_FEED_URLS` in `mta_feeds.py`.
- `data/stations.csv` is a static snapshot built from MTA's GTFS bundle
  (stops.txt + trips.txt + stop_times.txt) — station names/ids/routes
  essentially never change, but if MTA opens a new station or
  reroutes a line permanently, this needs regenerating from a fresh
  `https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip`.
- No live directions/trip-planning tool (see above) — `find_stations`
  + `get_next_trains` + `get_subway_alerts` together answer "what's
  running and when's the next train," but not "what's the best way to
  get from A to B." Building that would mean routing over the static
  GTFS graph yourself (or wiring in an external directions API).
