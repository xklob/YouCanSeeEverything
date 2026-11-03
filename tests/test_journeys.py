"""Synthetic journey groups exercise pagination beyond the map leg limit."""
from datetime import datetime, timedelta, timezone
import sqlite3

from biography.db import init_db
from biography.locations import ensure_schema
from biography.journeys import journey_detail, list_journeys


def seed(path, rows):
    init_db(path)
    with sqlite3.connect(path) as conn:
        ensure_schema(conn)
        for source in sorted({row[1] for row in rows}):
            conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                         (source, source, "2024-01-01", "2024-01-01"))
        for identity, source, start, end, longitude in rows:
            conn.execute("INSERT INTO events(id,source_id,title,occurred_at,end_at,category,time_precision,created_at) "
                         "VALUES(?,?,?,?,?,'place_visit','second',?)",
                         (identity, source, "Recorded " + identity, start, end, "2024-01-01"))
            conn.execute("INSERT INTO event_locations VALUES(?,?,0,0,?,?,'place','$.location','{}')",
                         ("point-" + identity, identity, longitude, start))


def test_list_groups_filters_whole_journeys_without_relinking(tmp_path):
    db = tmp_path / "catalog.sqlite3"
    seed(db, [
        ("a", "alpha", "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", 0),
        ("b", "alpha", "2024-01-01T04:00:00Z", "2024-01-01T05:00:00Z", .1),
        ("c", "alpha", "2024-01-08T00:00:00Z", "2024-01-08T01:00:00Z", 0),
        ("d", "alpha", "2024-01-08T04:00:00Z", "2024-01-08T05:00:00Z", 2),
        ("e", "beta", "2024-01-02T00:00:00Z", "2024-01-02T01:00:00Z", 0),
        ("f", "beta", "2024-01-02T04:00:00Z", "2024-01-02T05:00:00Z", 3),
    ])
    result = list_journeys(db, limit=1)
    assert result["total"] == 3 and result["truncated"]
    assert result["items"][0]["first_stop"]["event_id"] == "c"
    assert list_journeys(db, source="alpha", kind="local_movement")["total"] == 1
    assert list_journeys(db, source="alpha", kind="long_distance")["total"] == 1
    assert list_journeys(db, start="2024-01-03", end="2024-01-07")["total"] == 0
    selected = list_journeys(db, start="2024-01-08T02:00:00Z", end="2024-01-08T03:00:00Z")
    assert selected["total"] == 1  # Select the whole established group, never invent stops.
    detail = journey_detail(selected["items"][0]["id"], db)
    assert detail["complete"] and detail["total"] == 1
    assert detail["items"][0]["from"]["event_id"] == "c"
    assert detail["items"][0]["to"]["event_id"] == "d"
    assert "provenance" in detail["items"][0]["from"]
    assert journey_detail("missing", db) is None


def test_group_summary_and_detail_do_not_truncate_at_ten_thousand_legs(tmp_path):
    db = tmp_path / "catalog.sqlite3"
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    def stamp(value):
        return value.isoformat().replace("+00:00", "Z")
    rows = [(f"stop-{i:05}", "alpha", stamp(base + timedelta(hours=i * 2)),
             stamp(base + timedelta(hours=i * 2, minutes=30)), i % 2) for i in range(10_005)]
    seed(db, rows)
    result = list_journeys(db)
    assert result["total"] == 1
    group = result["items"][0]
    assert group["legs"] == 10_004
    assert group["represented_stops"] == 10_005 and group["summary_complete"]
    assert group["last_stop"]["event_id"] == "stop-10004"
    detail = journey_detail(group["id"], db, offset=10_000, limit=3)
    assert detail["total"] == 10_004 and not detail["complete"] and detail["truncated"]
    assert detail["items"][0]["from"]["event_id"] == "stop-10000"
    assert detail["items"][-1]["to"]["event_id"] == "stop-10003"
    beyond = journey_detail(group["id"], db, offset=20_000)
    assert beyond["journey"]["id"] == group["id"] and beyond["items"] == []
    assert not beyond["complete"]


def test_invalid_and_imprecise_stops_preserve_barriers(tmp_path):
    db = tmp_path / "catalog.sqlite3"
    seed(db, [
        ("a", "alpha", "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", 0),
        ("b", "alpha", "2024-01-01T04:00:00Z", None, 1),
        ("c", "alpha", "2024-01-01T08:00:00Z", "2024-01-01T09:00:00Z", 2),
    ])
    assert list_journeys(db)["total"] == 0


def test_long_recorded_dwell_breaks_groups_even_when_next_short_hop_is_omitted(tmp_path):
    db = tmp_path / "catalog.sqlite3"
    seed(db, [
        ("a", "alpha", "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", 0),
        ("b", "alpha", "2024-01-01T04:00:00Z", "2024-01-03T04:00:00Z", 1),
        ("c", "alpha", "2024-01-03T05:00:00Z", "2024-01-03T06:00:00Z", 1.001),
        ("d", "alpha", "2024-01-03T09:00:00Z", "2024-01-03T10:00:00Z", 2),
    ])
    result = list_journeys(db)
    assert result["total"] == 2
    assert {(row["first_stop"]["event_id"], row["last_stop"]["event_id"]) for row in result["items"]} == {("a", "b"), ("c", "d")}


def test_journey_filters_reject_invalid_ranges_and_kinds(tmp_path):
    import pytest
    db = tmp_path / "catalog.sqlite3"
    for options in ({"kind": "anything"}, {"start": "bad"},
                    {"start": "2024-02-01", "end": "2024-01-01"}):
        with pytest.raises(ValueError):
            list_journeys(db, **options)
