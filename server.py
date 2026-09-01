"""
server.py — MTA Subway Alerts MCP server (v1)

Exposes one tool to Claude:
  - get_subway_alerts(lines): current service alerts (delays, planned
    work, reroutes, "no service" notices) for the NYC subway, optionally
    filtered to specific lines.

Run locally (stdio, for testing with `mcp dev` or Claude Desktop's
local config):
    python server.py

Run as an HTTP service (what you actually need for the Claude mobile
app / claude.ai, since those only reach remote MCP servers):
    python server.py --http
which starts a Streamable HTTP server on 0.0.0.0:$PORT.
"""

import os
import sys
import urllib.parse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mta_feeds import KNOWN_LINES, fetch_alerts, fetch_next_trains, search_stations

_MAPS_TRAVEL_MODES = {"transit", "walking", "driving", "bicycling"}

# Cap how many distinct real-world stations a fuzzy get_next_trains()
# query is allowed to resolve to before we bail and ask for something
# more specific — several genuinely different stations share names like
# "103 St", and fetching live feeds for a dozen of them isn't useful.
_MAX_STATION_MATCHES = 6

# FastMCP's DNS-rebinding protection defaults to only trusting
# localhost/127.0.0.1 Host headers, which 421s every request once this
# is deployed behind a real public hostname (Render, Fly, etc.) — so we
# add the deployed host explicitly. Render sets RENDER_EXTERNAL_HOSTNAME
# automatically; other hosts would need their own equivalent env var.
_allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_allowed_origins = ["http://127.0.0.1:*", "http://localhost:*"]
_external_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _external_host:
    _allowed_hosts.append(_external_host)
    _allowed_origins.append(f"https://{_external_host}")

mcp = FastMCP(
    "mta-subway-status",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


@mcp.tool()
def get_subway_alerts(lines: list[str] | None = None) -> dict:
    """
    Get current NYC subway service alerts: delays, planned repair work,
    reroutes, and lines with no service right now.

    Args:
        lines: Optional list of subway line names to check, e.g.
               ["7", "E", "M", "G"]. If omitted, returns alerts for
               every line system-wide.

    Returns:
        A dict with:
        - "checked_lines": the lines that were filtered for (or "all")
        - "alerts": list of active alerts, each with routes/effect/
          header/description
        - "lines_with_no_reported_issues": lines you asked about that
          have no active alert right now (only present if `lines` was
          given) — absence of an alert generally means normal service,
          but isn't a 100% guarantee.
    """
    if lines:
        unknown = [l for l in lines if l.upper() not in KNOWN_LINES]
        if unknown:
            return {
                "error": f"Unrecognized line(s): {unknown}. "
                         f"Known lines: {sorted(KNOWN_LINES)}"
            }

    alerts = fetch_alerts(lines=lines)

    result = {
        "checked_lines": [l.upper() for l in lines] if lines else "all",
        "alerts": alerts,
    }

    if lines:
        affected = {r for a in alerts for r in a["routes"]}
        clear = sorted(l.upper() for l in lines if l.upper() not in affected)
        result["lines_with_no_reported_issues"] = clear

    return result


@mcp.tool()
def find_stations(query: str) -> dict:
    """
    Look up NYC subway stations by name, to get the stop info needed by
    get_next_trains (or just to check which lines serve a station).

    Args:
        query: free-text station name, e.g. "times sq", "union sq", "103 st".

    Returns:
        A dict with "matches": a list of {stop_id, name, lat, lon, routes}.
        Several distinct real-world stations can share a name (e.g. there
        are four different "103 St" stations) — all of them come back;
        disambiguate using the "routes" each one serves.
    """
    matches = search_stations(query)
    return {
        "query": query,
        "matches": [
            {**m, "routes": sorted(m["routes"])} for m in matches
        ],
    }


@mcp.tool()
def get_next_trains(station: str, lines: list[str] | None = None, limit: int = 6) -> dict:
    """
    Get live predicted arrival times at a subway station.

    Args:
        station: free-text station name, e.g. "Times Sq", "Union Sq", "125 St".
        lines: optional list of route letters/numbers to restrict to, e.g.
               ["4", "5", "6"]. Also disambiguates when the station name
               alone matches more than one real-world station.
        limit: max arrivals to return per matched station (combined across
               both directions, soonest first).

    Returns:
        A dict with "stations": a list of
        {name, stop_id, routes, arrivals: [{route, direction, minutes_away}]}
        — one entry per real-world station the name resolved to, soonest
        arrivals first. "direction" is MTA's platform code: "N" is
        typically uptown/Bronx-bound and "S" downtown/Brooklyn-bound,
        though that convention gets fuzzy on lines that don't run
        north-south (e.g. L, G, 7).
        If the name is ambiguous (matches too many distinct stations) or
        doesn't match anything, returns an "error" instead — narrow with
        `lines` or a more specific name.
    """
    wanted_lines = None
    if lines:
        wanted_lines = {l.upper() for l in lines}
        unknown = [l for l in wanted_lines if l not in KNOWN_LINES]
        if unknown:
            return {
                "error": f"Unrecognized line(s): {unknown}. "
                         f"Known lines: {sorted(KNOWN_LINES)}"
            }

    matches = search_stations(station)
    if wanted_lines:
        matches = [m for m in matches if wanted_lines & m["routes"]]

    if not matches:
        return {"error": f"No station found matching '{station}'."}
    if len(matches) > _MAX_STATION_MATCHES:
        return {
            "error": f"'{station}' matches {len(matches)} different stations — "
                     f"be more specific or pass `lines` to narrow it down.",
            "candidates": sorted({m["name"] for m in matches}),
        }

    stop_ids_needed = {f"{m['stop_id']}{d}" for m in matches for d in ("N", "S")}
    feed_lines = wanted_lines if wanted_lines else {r for m in matches for r in m["routes"]}
    arrivals = fetch_next_trains(stop_ids_needed, feed_lines)

    stations_out = []
    for m in matches:
        station_lines = (wanted_lines & m["routes"]) if wanted_lines else m["routes"]
        station_stop_ids = {f"{m['stop_id']}{d}" for d in ("N", "S")}
        station_arrivals = [
            {
                "route": a["route"],
                "direction": a["direction"],
                "minutes_away": a["minutes_away"],
            }
            for a in arrivals
            if a["stop_id"] in station_stop_ids and a["route"] in station_lines
        ][:limit]
        stations_out.append({
            "name": m["name"],
            "stop_id": m["stop_id"],
            "routes": sorted(station_lines),
            "arrivals": station_arrivals,
        })

    return {"station_query": station, "stations": stations_out}


@mcp.tool()
def get_directions_link(destination: str, origin: str | None = None, mode: str = "transit") -> dict:
    """
    Build a Google Maps directions link — no API key needed, just a URL.

    This server has no way to know your live location (it runs in the
    cloud, not on your phone), so if you omit `origin`, don't expect the
    link to already have a starting point baked in. Instead, opening the
    returned link on a phone lets the Google Maps *app* itself fall back
    to your device's live GPS location as the starting point — that
    resolution happens locally on your phone, not through this server.

    Args:
        destination: address, place name, or landmark to route to, e.g.
                     "Canal St, Chinatown, Manhattan" or "Court Sq-23 St".
        origin: optional starting address/place. Omit to let Google Maps
                use the device's current location when the link is opened.
        mode: one of "transit" (default), "walking", "driving", "bicycling".

    Returns:
        {"url": "..."} and, when `origin` was omitted, a "note" explaining
        that the starting point resolves on the device, not here. Returns
        an "error" if `mode` isn't one of the recognized values.
    """
    if mode not in _MAPS_TRAVEL_MODES:
        return {"error": f"Unknown mode '{mode}'. Use one of: {sorted(_MAPS_TRAVEL_MODES)}"}

    params = {"api": "1", "destination": destination, "travelmode": mode}
    if origin:
        params["origin"] = origin

    result = {"url": "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(params)}
    if not origin:
        result["note"] = (
            "No origin given — opening this link on a phone lets Google Maps "
            "use the device's live location as the starting point."
        )
    return result


if __name__ == "__main__":
    if "--http" in sys.argv:
        port = int(os.environ.get("PORT", 8000))
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
