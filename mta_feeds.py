"""
mta_feeds.py

Talks to the MTA's public GTFS-realtime feeds and turns the protobuf
responses into plain Python dicts. No API key needed for subway feeds
(MTA dropped that requirement in 2024) — this just does anonymous
GET requests.

Two feed families matter for v1:
  1. The service alerts feed — planned work, disruptions, "no service"
     notices. This is the one that answers "is anything broken right now."
  2. (Not used in v1) the per-line trip-update feeds, which would be
     needed for live arrival countdowns — skipped per the v1 scope.
"""

from __future__ import annotations

import httpx

# The single feed that carries ALL subway service alerts (delays, planned
# work, station closures, reroutes, "no service" notices, etc.), across
# every line. Verified live (200, real protobuf data) as of Sept 2026.
# Note the resource path segment is "camsys/subway-alerts" but the "/" must
# stay percent-encoded as %2F — API Gateway treats it as one opaque path
# param, and an unencoded "/" 403s with "Missing Authentication Token".
# If requests start failing, check https://api.mta.info for the current link.
ALERTS_FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts"

# Maps every subway line to the "route_id" the GTFS feed uses to tag it.
# For almost all lines this is just the line name itself. A few have
# quirks (Franklin Ave Shuttle = "FS", Rockaway Park Shuttle = "H", etc).
KNOWN_LINES = {
    "1", "2", "3", "4", "5", "6", "7",
    "A", "B", "C", "D", "E", "F", "G",
    "J", "Z", "L", "M", "N", "Q", "R", "W",
    "SI",  # Staten Island Railway
    "GS",  # 42 St Shuttle
    "FS",  # Franklin Ave Shuttle
    "H",   # Rockaway Park Shuttle
}

# Human-readable labels for the GTFS-realtime "Effect" enum, so the tool
# doesn't hand Claude a bare integer.
EFFECT_LABELS = {
    1: "no_service",
    2: "reduced_service",
    3: "significant_delays",
    4: "detour",
    5: "additional_service",
    6: "modified_service",
    7: "other_effect",
    8: "unknown_effect",
    9: "stop_moved",
    10: "no_effect",
    11: "accessibility_issue",
}


def _first_translation(translated_string) -> str:
    """GTFS-RT text fields carry a list of {text, language} translations.
    We just want the first one (MTA feeds are English-only in practice)."""
    if not translated_string or not translated_string.translation:
        return ""
    return translated_string.translation[0].text


def fetch_alerts(lines: list[str] | None = None, timeout: float = 10.0) -> list[dict]:
    """
    Fetch and parse the current subway service alerts feed.

    Args:
        lines: optional list of line letters/numbers to filter to
               (e.g. ["7", "E", "M", "G"]). If None, returns alerts for
               every line system-wide.
        timeout: HTTP timeout in seconds.

    Returns:
        A list of alert dicts, each shaped like:
        {
            "routes": ["E", "M"],
            "effect": "reduced_service",
            "header": "E and M trains running with delays",
            "description": "Because of a signal problem at ...",
        }
    """
    wanted = {l.upper() for l in lines} if lines else None

    resp = httpx.get(ALERTS_FEED_URL, timeout=timeout)
    resp.raise_for_status()

    # Imported here (not at module top) so this file can be unit-tested
    # against a hand-built FeedMessage without needing network access.
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    return _parse_alert_entities(feed, wanted)


def _parse_alert_entities(feed, wanted: set[str] | None) -> list[dict]:
    """Pull Alert entities out of a parsed FeedMessage. Split out from
    fetch_alerts() so tests can feed it a synthetic FeedMessage directly."""
    results = []

    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert

        routes = sorted({
            ie.route_id
            for ie in alert.informed_entity
            if ie.route_id
        })
        if not routes:
            continue  # station/system alerts with no route tag — skip for v1

        if wanted is not None and not (wanted & set(routes)):
            continue  # doesn't touch any line we were asked about

        results.append({
            "routes": routes,
            "effect": EFFECT_LABELS.get(alert.effect, f"unknown({alert.effect})"),
            "header": _first_translation(alert.header_text),
            "description": _first_translation(alert.description_text),
        })

    return results
