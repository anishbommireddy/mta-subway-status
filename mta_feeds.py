"""
mta_feeds.py

Talks to the MTA's public GTFS-realtime feeds and turns the protobuf
responses into plain Python dicts. No API key needed for subway feeds
(MTA dropped that requirement in 2024) — this just does anonymous
GET requests.

Two feed families matter here:
  1. The service alerts feed — planned work, disruptions, "no service"
     notices. This is the one that answers "is anything broken right now."
  2. The per-line trip-update feeds — live predicted arrival times,
     split across 8 feeds by line group. This is what answers "when's
     the next train."

Station name -> stop_id lookup (needed because the live feeds only key
arrivals by GTFS stop_id, not station names) comes from a small bundled
CSV (data/stations.csv), built once from MTA's static GTFS bundle
(stops.txt + trips.txt + stop_times.txt) rather than fetched live —
station names and ids essentially never change.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

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

# The per-line trip-update feeds. MTA splits live arrival predictions
# across 8 feeds by line group (same host/encoding as ALERTS_FEED_URL).
# Each carries TripUpdate entities: one per active trip, with a list of
# upcoming stops and predicted arrival times.
_FEED_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F"
TRIP_UPDATE_FEED_URLS = {
    "1234567S": _FEED_BASE + "gtfs",
    "ace": _FEED_BASE + "gtfs-ace",
    "bdfm": _FEED_BASE + "gtfs-bdfm",
    "g": _FEED_BASE + "gtfs-g",
    "jz": _FEED_BASE + "gtfs-jz",
    "nqrw": _FEED_BASE + "gtfs-nqrw",
    "l": _FEED_BASE + "gtfs-l",
    "si": _FEED_BASE + "gtfs-si",
}

# Which trip-update feed carries a given line's live positions.
LINE_TO_FEED_GROUP = {
    "1": "1234567S", "2": "1234567S", "3": "1234567S", "4": "1234567S",
    "5": "1234567S", "6": "1234567S", "7": "1234567S", "GS": "1234567S",
    "A": "ace", "C": "ace", "E": "ace", "H": "ace", "FS": "ace",
    "B": "bdfm", "D": "bdfm", "F": "bdfm", "M": "bdfm",
    "G": "g",
    "J": "jz", "Z": "jz",
    "N": "nqrw", "Q": "nqrw", "R": "nqrw", "W": "nqrw",
    "L": "l",
    "SI": "si",
}

# GTFS static data tags the 6, 7 and F's express/diamond variants as
# "6X"/"7X"/"FX" — normalize those to the plain letter/number used
# everywhere else (KNOWN_LINES, the alerts feed, station signage).
_ROUTE_ALIASES = {"6X": "6", "7X": "7", "FX": "F"}

_STATIONS_CSV = Path(__file__).parent / "data" / "stations.csv"
_stations_cache: list[dict] | None = None


def _load_stations() -> list[dict]:
    """Load the bundled station name -> stop_id reference, cached after
    the first call. Each row is one GTFS "parent station" (location_type
    1) with the set of routes that actually stop there."""
    global _stations_cache
    if _stations_cache is None:
        with open(_STATIONS_CSV, newline="", encoding="utf-8") as f:
            _stations_cache = [
                {
                    "stop_id": row["stop_id"],
                    "name": row["name"],
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                    "routes": set(row["routes"].split("|")),
                }
                for row in csv.DictReader(f)
            ]
    return _stations_cache


def search_stations(query: str) -> list[dict]:
    """
    Find subway stations by (partial) name.

    Args:
        query: free-text station name, e.g. "times sq", "103", "union sq".

    Returns:
        A list of matching station dicts: {stop_id, name, lat, lon, routes}.
        An exact (case-insensitive) name match returns just that station
        (or stations, since several distinct real-world stations share a
        name like "103 St"); otherwise all stations whose name contains
        the query are returned.
    """
    q = query.strip().lower()
    if not q:
        return []
    stations = _load_stations()
    exact = [s for s in stations if s["name"].lower() == q]
    if exact:
        return exact
    return [s for s in stations if q in s["name"].lower()]


def _parse_trip_update_entities(
    feed, stop_ids: set[str], lines: set[str], now: float
) -> list[dict]:
    """Pull TripUpdate entities out of a parsed FeedMessage and extract
    predicted arrivals at the wanted stop_ids/lines. Split out from
    fetch_next_trains() so tests can feed it a synthetic FeedMessage
    directly, same pattern as _parse_alert_entities()."""
    results = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        route = _ROUTE_ALIASES.get(trip_update.trip.route_id, trip_update.trip.route_id)
        if route not in lines:
            continue

        for stu in trip_update.stop_time_update:
            if stu.stop_id not in stop_ids:
                continue
            if stu.HasField("arrival"):
                arrival_epoch = stu.arrival.time
            elif stu.HasField("departure"):
                arrival_epoch = stu.departure.time
            else:
                continue

            minutes_away = (arrival_epoch - now) / 60
            if minutes_away < -1:
                continue  # stale prediction for a train that's already left

            results.append({
                "route": route,
                "stop_id": stu.stop_id,
                "direction": stu.stop_id[-1] if stu.stop_id[-1] in "NS" else "?",
                "minutes_away": round(max(minutes_away, 0), 1),
                "arrival_epoch": arrival_epoch,
            })

    results.sort(key=lambda r: r["arrival_epoch"])
    return results


def fetch_next_trains(
    stop_ids: set[str], lines: set[str], timeout: float = 10.0
) -> list[dict]:
    """
    Fetch live predicted arrivals at the given stop_ids for the given lines.

    Args:
        stop_ids: GTFS child stop_ids to match against, e.g. {"635N", "635S"}
                   (the "N"/"S" suffix is the platform/direction code).
        lines: route letters/numbers to include, e.g. {"4", "5", "6"} —
               also determines which of the 8 trip-update feeds get hit.
        timeout: HTTP timeout in seconds, per feed request.

    Returns:
        A list of arrival dicts, soonest first:
        {"route": "6", "stop_id": "635N", "direction": "N",
         "minutes_away": 3.2, "arrival_epoch": 1735689600}
    """
    from google.transit import gtfs_realtime_pb2

    feed_groups = {LINE_TO_FEED_GROUP[l] for l in lines if l in LINE_TO_FEED_GROUP}
    now = time.time()
    results = []

    for group in feed_groups:
        resp = httpx.get(TRIP_UPDATE_FEED_URLS[group], timeout=timeout)
        resp.raise_for_status()

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)

        results.extend(_parse_trip_update_entities(feed, stop_ids, lines, now))

    results.sort(key=lambda r: r["arrival_epoch"])
    return results


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
