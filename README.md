# mta-subway-status — MCP server (v1)

Lets Claude answer "any delays or lines not running right now" for the
NYC subway, from any Claude client (web, mobile, desktop) — by exposing
one tool, `get_subway_alerts`, backed by MTA's live GTFS-realtime alerts
feed.

**What it does:** current service alerts — delays, planned repair work,
reroutes, "no service" notices — optionally filtered to specific lines
(e.g. `["7", "E", "M", "G"]`).

**What it deliberately doesn't do (v1 scope):** no live arrival
countdowns, no route optimization. Just "what's broken right now."

**No MTA API key needed** — subway GTFS-realtime feeds have been
open/keyless since 2024. If you later add bus data, that still requires
a free key from https://bustime.mta.info/wiki/Developers/Index.

## Tested so far

- `test_parsing.py` — validates the protobuf → dict parsing logic
  against a synthetic feed (passing).
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
  correctly filtered alert data end-to-end.

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

## Known limitations / next steps if you want to extend this

- Alert text comes straight from MTA's feed — no rewriting or
  summarizing happens server-side; Claude does that in conversation.
- No caching — every tool call hits MTA fresh. Fine at personal-use
  volume; add a short in-memory cache if you start calling it a lot.
- If MTA ever moves the alerts feed URL, update `ALERTS_FEED_URL` in
  `mta_feeds.py`.
