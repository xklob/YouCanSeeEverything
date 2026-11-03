import json
from pathlib import Path
import tempfile
import unittest

from biography.db import connect, init_db
from biography.locations import extract_points, map_points, reconcile_locations


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "catalog.sqlite3"
        init_db(self.database)
        with connect(self.database) as conn:
            for source in ("source-a", "source-b"):
                conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                             (source, source, "2024-01-01", "2024-01-01"))

    def tearDown(self):
        self.temp.cleanup()

    def event(self, identity, metadata, *, source="source-a", category="place_visit",
              when="2024-02-03T12:00:00Z", title="Synthetic visit", precision="second"):
        with connect(self.database) as conn:
            conn.execute("""INSERT INTO events(id,source_id,title,body,occurred_at,category,
                metadata,time_precision,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (identity,source,title,"fixture searchable body",when,category,json.dumps(metadata),precision,"2024-01-01"))

    def test_legacy_and_semantic_points_keep_explicit_source_coordinates(self):
        self.event("legacy", {"location": {"latitudeE7": 125000000, "longitudeE7": 452500000}})
        self.event("semantic", {"visit": {"topCandidate": {"placeLocation": {"latLng": "geo:-20.25,130.5"}}}})
        result = reconcile_locations(self.database)
        self.assertEqual(result, {"events": 2, "points": 2})
        response = map_points(self.database)
        self.assertEqual({(p["latitude"],p["longitude"]) for p in response["items"]},
                         {(12.5,45.25),(-20.25,130.5)})
        self.assertTrue(all(p["metadata"]["coordinate_basis"] == "explicit_source_coordinate" for p in response["items"]))

    def test_explicit_path_times_are_not_interpolated_or_fabricated(self):
        points = extract_points({"timelinePath": [
            {"point": "geo:1,2", "durationMinutesOffsetFromStartTime": "5"},
            {"point": "geo:3,4", "time": "2024-02-03T12:20:00Z"}]},
            "2024-02-03T12:00:00Z")
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["occurred_at"], "2024-02-03T12:05:00Z")
        self.assertEqual(points[1]["occurred_at"], "2024-02-03T12:20:00Z")
        offset_point = extract_points({"timelinePath":[{"point":"geo:1,2","time":"2024-02-03T14:00:00+02:00"}]})
        self.assertEqual(offset_point[0]["occurred_at"],"2024-02-03T12:00:00Z")
        self.assertEqual(extract_points({"address": "A plausible street", "distance": 123}), [])

    def test_invalid_coordinates_and_unselected_candidate_metadata_are_ignored(self):
        self.assertEqual(extract_points({"latitude": float("nan"), "longitude": 5}), [])
        self.assertEqual(extract_points({"latitude": 91, "longitude": 5}), [])
        self.assertEqual(extract_points({"latitude": True, "longitude": 5}), [])
        self.assertEqual(extract_points({"candidates": [{"latitude": 1,"longitude": 2}]}), [])

    def test_map_filters_search_dates_source_category_and_pagination(self):
        self.event("one", {"latitude":1,"longitude":2}, title="Special visit")
        self.event("two", {"latitude":3,"longitude":4}, source="source-b", when="2024-03-01T01:00:00Z")
        self.event("three", {"latitude":5,"longitude":6}, category="travel", when=None, precision="unknown")
        reconcile_locations(self.database)
        self.assertEqual(map_points(self.database,q="Special",source="source-a",start="2024-02-03",end="2024-02-03")["total"],1)
        self.assertEqual(map_points(self.database,category="travel",undated=True)["items"][0]["event_id"],"three")
        page = map_points(self.database,limit=1,offset=1)
        self.assertEqual(page["total"],3)
        self.assertEqual(len(page["items"]),1)
        self.assertEqual(map_points(self.database,bounds=(0,0,3,2))["total"],1)

    def test_incremental_index_and_explicit_refresh_do_not_duplicate_points(self):
        self.event("one", {"latitude":1,"longitude":2})
        reconcile_locations(self.database)
        self.assertEqual(reconcile_locations(self.database), {"events":0,"points":0})
        with connect(self.database) as conn:
            conn.execute("UPDATE events SET metadata=? WHERE id='one'",(json.dumps({"latitude":7,"longitude":8}),))
        reconcile_locations(self.database,event_ids=["one"])
        result = map_points(self.database)
        self.assertEqual(result["total"],1)
        self.assertEqual(result["items"][0]["latitude"],7)

    def test_coarse_dates_keep_precision_and_deleting_event_cascades_index(self):
        self.event("one", {"latitude":1,"longitude":2}, when="2024-02", precision="month")
        reconcile_locations(self.database)
        self.assertEqual(map_points(self.database)["items"][0]["time_precision"],"month")
        with connect(self.database) as conn:
            conn.execute("DELETE FROM events WHERE id='one'")
        self.assertEqual(map_points(self.database)["total"],0)

    def test_other_event_coordinates_and_reviewed_enrichment_keep_provenance(self):
        self.event("booking",{"enrichment":{"location":{"latitude":10,"longitude":20},
            "provenance":{"url":"https://events.example/fixture","method":"explicit_source_coordinates"}}},category="event_booking")
        self.event("post",{"coordinates":{"type":"Point","coordinates":[30,40]}},category="post")
        self.event("unlocated",{"address":"Some city"},category="post")
        reconcile_locations(self.database)
        result = map_points(self.database)
        self.assertEqual(result["total"],2)
        booking = next(point for point in result["items"] if point["event_id"] == "booking")
        self.assertEqual(booking["metadata"]["enrichment_provenance"]["url"],"https://events.example/fixture")
        post = next(point for point in result["items"] if point["event_id"] == "post")
        self.assertEqual((post["latitude"],post["longitude"]),(40,30))

    def test_activity_end_uses_explicit_event_end_when_point_has_no_timestamp(self):
        points = extract_points({"startLocation":{"latitude":1,"longitude":2},
            "endLocation":{"latitude":3,"longitude":4}},"2024-02-03T12:00:00Z",end_at="2024-02-03T14:00:00Z")
        self.assertEqual(points[1]["occurred_at"],"2024-02-03T14:00:00Z")
        self.assertEqual(points[1]["metadata"]["time_basis"],"parent_event_end")


if __name__ == "__main__":
    unittest.main()
