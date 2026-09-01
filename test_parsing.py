"""
Smoke test for mta_feeds._parse_alert_entities.

Builds a fake FeedMessage in memory (no network call) with two alerts —
one matching lines we'll filter for, one not — and checks the parser
extracts them correctly. This validates the parsing logic independent
of whether the live MTA feed is reachable from wherever this runs.
"""

from google.transit import gtfs_realtime_pb2
from mta_feeds import _parse_alert_entities


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

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    main()
