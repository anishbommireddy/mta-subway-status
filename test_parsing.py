"""
Smoke test for mta_feeds._parse_alert_entities.

Builds a fake FeedMessage in memory (no network call) with two alerts —
one matching lines we'll filter for, one not — and checks the parser
extracts them correctly. This validates the parsing logic independent
of whether the live MTA feed is reachable from wherever this runs.
"""

from google.transit import gtfs_realtime_pb2
from mta_feeds import _parse_alert_entities, _parse_trip_update_entities, search_stations


def build_fake_feed():
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "1.0"

    # Alert 1: affects E and M — this is the one we expect to survive
    # a filter for lines=["E", "M", "G"]
    e1 = feed.entity.add()
    e1.id = "alert-1"
    e1.alert.effect = 3  # SIGNIFICANT_DELAYS
    ie1 = e1.alert.informed_entity.add()
    ie1.route_id = "E"
    ie2 = e1.alert.informed_entity.add()
    ie2.route_id = "M"
    e1.alert.header_text.translation.add(text="E and M trains delayed", language="en")
    e1.alert.description_text.translation.add(
        text="Signal problems at Court Sq are causing delays.", language="en"
    )

    # Alert 2: affects only the 1 train — should be filtered OUT when
    # we ask for E/M/G
    e2 = feed.entity.add()
    e2.id = "alert-2"
    e2.alert.effect = 4  # DETOUR
    ie3 = e2.alert.informed_entity.add()
    ie3.route_id = "1"
    e2.alert.header_text.translation.add(text="1 trains rerouted", language="en")
    e2.alert.description_text.translation.add(
        text="Planned track work this weekend.", language="en"
    )

    # Alert 3: a station-only alert with no route_id — should be
    # skipped entirely (matches the "skip for v1" branch)
    e3 = feed.entity.add()
    e3.id = "alert-3"
    e3.alert.effect = 11
    e3.alert.header_text.translation.add(text="Elevator out of service", language="en")

    return feed


def build_fake_trip_update_feed(now: float):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "1.0"

    # Trip 1: a 6 train, 3 minutes out at 635N (Union Sq uptown platform)
    e1 = feed.entity.add()
    e1.id = "trip-1"
    e1.trip_update.trip.route_id = "6"
    stu1 = e1.trip_update.stop_time_update.add()
    stu1.stop_id = "635N"
    stu1.arrival.time = int(now + 180)

    # Trip 2: a 6X (express diamond) train, should normalize to route "6",
    # 5 minutes out at 635S
    e2 = feed.entity.add()
    e2.id = "trip-2"
    e2.trip_update.trip.route_id = "6X"
    stu2 = e2.trip_update.stop_time_update.add()
    stu2.stop_id = "635S"
    stu2.arrival.time = int(now + 300)

    # Trip 3: an E train at a stop we don't care about — should be filtered out
    e3 = feed.entity.add()
    e3.id = "trip-3"
    e3.trip_update.trip.route_id = "E"
    stu3 = e3.trip_update.stop_time_update.add()
    stu3.stop_id = "A18N"
    stu3.arrival.time = int(now + 60)

    # Trip 4: a 4 train that already left (stale prediction) — should be dropped
    e4 = feed.entity.add()
    e4.id = "trip-4"
    e4.trip_update.trip.route_id = "4"
    stu4 = e4.trip_update.stop_time_update.add()
    stu4.stop_id = "635N"
    stu4.arrival.time = int(now - 120)

    return feed


def main():
    feed = build_fake_feed()

    # Unfiltered: should get alert-1 and alert-2, not alert-3
    all_alerts = _parse_alert_entities(feed, wanted=None)
    assert len(all_alerts) == 2, f"expected 2 alerts, got {len(all_alerts)}"
    print("PASS: unfiltered returns 2 alerts (station-only alert correctly skipped)")

    # Filtered to E/M/G: should get only alert-1
    filtered = _parse_alert_entities(feed, wanted={"E", "M", "G"})
    assert len(filtered) == 1, f"expected 1 alert, got {len(filtered)}"
    assert filtered[0]["routes"] == ["E", "M"]
    assert filtered[0]["effect"] == "significant_delays"
    assert "Court Sq" in filtered[0]["description"]
    print("PASS: filtering to E/M/G returns only the E/M alert")

    # Filtered to a line with no active alerts
    none_found = _parse_alert_entities(feed, wanted={"7"})
    assert none_found == []
    print("PASS: line with no alerts returns empty list")

    # --- trip updates (next-train arrivals) ---
    import time
    now = time.time()
    tu_feed = build_fake_trip_update_feed(now)

    arrivals = _parse_trip_update_entities(
        tu_feed, stop_ids={"635N", "635S"}, lines={"6"}, now=now
    )
    assert len(arrivals) == 2, f"expected 2 arrivals, got {len(arrivals)}"
    assert arrivals[0]["route"] == "6" and arrivals[0]["direction"] == "N"
    assert arrivals[0]["minutes_away"] == 3.0
    assert arrivals[1]["route"] == "6" and arrivals[1]["direction"] == "S"
    assert arrivals[1]["minutes_away"] == 5.0
    print("PASS: trip updates filtered to stop_ids/lines, 6X normalized to 6, "
          "wrong-stop and stale entries dropped")

    # --- station search over the bundled reference data ---
    exact = search_stations("Times Sq-42 St")
    assert len(exact) == 4, f"expected 4 stop_ids for the Times Sq complex, got {len(exact)}"
    names = {s["name"] for s in exact}
    assert names == {"Times Sq-42 St"}

    # Union Sq is 3 separate GTFS parent stations sharing one name (like
    # Times Sq) — one per physically distinct platform group.
    substring = search_stations("union sq")
    assert len(substring) == 3, f"expected 3 Union Sq stop_ids, got {len(substring)}"
    routes_by_stop = {s["stop_id"]: s["routes"] for s in substring}
    assert routes_by_stop["635"] == {"4", "5", "6"}
    assert routes_by_stop["L03"] == {"L"}
    assert routes_by_stop["R20"] == {"N", "Q", "R", "W"}

    assert search_stations("not a real station") == []
    print("PASS: search_stations resolves exact and substring matches with correct routes")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    main()
