import json
from pathlib import Path
import tempfile
import unittest

from biography.db import connect,init_db
from biography.locations import reconcile_locations
from biography.trips import distance_km,trip_connections


class TripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name)/"catalog.sqlite3"
        init_db(self.database)
        with connect(self.database) as conn:
            for identity,account in (("source-a","account-a"),("source-b","account-b")):
                conn.execute("INSERT INTO sources(id,name,account_key,created_at,updated_at) VALUES(?,?,?,?,?)",
                             (identity,identity,account,"2024-01-01","2024-01-01"))

    def tearDown(self):
        self.temp.cleanup()

    def stop(self,identity,start,end,longitude,*,latitude=0,source="source-a",title=None,
             precision="second",category="place_visit",metadata=None):
        if metadata is None:
            metadata = {"location":{"latitude":latitude,"longitude":longitude}}
        with connect(self.database) as conn:
            conn.execute("""INSERT INTO events(id,source_id,title,body,occurred_at,end_at,category,time_precision,metadata,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",(identity,source,title or identity,"fixture",start,end,category,precision,json.dumps(metadata),"2024-01-01"))
        reconcile_locations(self.database,event_ids=[identity])

    def pair(self):
        self.stop("a","2024-01-01T00:00:00Z","2024-01-01T01:00:00Z",0)
        self.stop("b","2024-01-01T05:00:00Z","2024-01-01T06:00:00Z",1)

    def test_leg_uses_previous_end_to_next_start_and_explicit_endpoint_contract(self):
        self.pair()
        result = trip_connections(self.database)
        self.assertEqual(result["total"],1)
        leg = result["items"][0]
        self.assertEqual(leg["gap_hours"],4)
        self.assertEqual(leg["departure_at"],"2024-01-01T01:00:00Z")
        self.assertEqual(leg["from"]["event_id"],"a")
        self.assertEqual(leg["to"]["latitude"],0)
        self.assertEqual(leg["to"]["longitude"],1)
        self.assertTrue(leg["inferred"])
        self.assertEqual(leg["geometry_type"],"straight_line")
        self.assertEqual(leg["trip_kind"],"long_distance")
        self.assertIn("provenance",leg["from"])

    def test_search_filters_and_limit_never_create_new_adjacency(self):
        self.stop("a","2024-01-01T00:00:00Z","2024-01-01T01:00:00Z",0,title="matching a")
        self.stop("b","2024-01-01T05:00:00Z","2024-01-01T06:00:00Z",1,title="hidden b")
        self.stop("c","2024-01-01T10:00:00Z","2024-01-01T11:00:00Z",2,title="matching c")
        self.assertEqual(trip_connections(self.database,q="matching")["total"],0)
        first = trip_connections(self.database,limit=1)
        self.assertEqual(first["total"],2)
        self.assertTrue(first["truncated"])
        second = trip_connections(self.database,limit=1,offset=1)
        self.assertEqual(second["items"][0]["from"]["event_id"],"b")
        self.assertEqual(second["items"][0]["to"]["event_id"],"c")

    def test_date_source_category_and_undated_filters(self):
        self.pair()
        self.stop("other","2024-01-01T02:00:00Z","2024-01-01T03:00:00Z",50,source="source-b")
        self.assertEqual(trip_connections(self.database,source="source-b")["total"],0)
        self.assertEqual(trip_connections(self.database,start="2024-01-01",end="2024-01-01")["total"],1)
        self.assertEqual(trip_connections(self.database,start="2024-01-02")["total"],0)
        self.assertEqual(trip_connections(self.database,category="event_booking")["total"],0)
        self.assertEqual(trip_connections(self.database,undated=True)["total"],0)
        with self.assertRaises(ValueError):
            trip_connections(self.database,start="not-a-date")
        with self.assertRaises(ValueError):
            trip_connections(self.database,start="2024-02-01",end="2024-01-01")

    def test_recording_hole_is_visible_inside_an_empty_selected_window(self):
        self.stop("a","2020-03-01T00:00:00Z","2020-03-01T01:00:00Z",0)
        self.stop("b","2021-01-01T00:00:00Z","2021-01-01T01:00:00Z",30)
        result = trip_connections(self.database,start="2020-05-01",end="2020-09-30")
        self.assertEqual(result["total"],0)
        self.assertEqual(result["gaps_total"],1)
        self.assertEqual(result["gaps"][0]["reason"],"recording_gap_exceeds_bound")
        self.assertGreater(result["gaps"][0]["gap_hours"],72)

    def test_invalid_or_missing_end_and_unlocated_stops_are_barriers(self):
        for reason,kwargs in (("missing_visit_end",{"end":None}),
                              ("reversed_visit_interval",{"end":"2023-12-31T00:00:00Z"}),
                              ("missing_explicit_coordinates",{"metadata":{"name":"unlocated"}})):
            with self.subTest(reason=reason):
                self.stop("a","2024-01-01T00:00:00Z","2024-01-01T01:00:00Z",0)
                self.stop("bad","2024-01-01T05:00:00Z",kwargs.get("end","2024-01-01T06:00:00Z"),1,
                          metadata=kwargs.get("metadata"))
                self.stop("c","2024-01-01T10:00:00Z","2024-01-01T11:00:00Z",2)
                result = trip_connections(self.database)
                self.assertEqual(result["total"],0)
                self.assertIn(reason,{gap["reason"] for gap in result["gaps"]})
                with connect(self.database) as conn:
                    conn.execute("DELETE FROM events")

    def test_overlap_cluster_cannot_bridge_via_its_shorter_visit(self):
        self.stop("a","2024-01-01T00:00:00Z","2024-01-01T10:00:00Z",0)
        self.stop("b","2024-01-01T02:00:00Z","2024-01-01T03:00:00Z",1)
        self.stop("c","2024-01-01T05:00:00Z","2024-01-01T06:00:00Z",2)
        self.stop("d","2024-01-01T11:00:00Z","2024-01-01T12:00:00Z",3)
        self.stop("e","2024-01-01T15:00:00Z","2024-01-01T16:00:00Z",4)
        result = trip_connections(self.database)
        self.assertEqual(result["total"],1)
        self.assertEqual((result["items"][0]["from"]["event_id"],result["items"][0]["to"]["event_id"]),("d","e"))

    def test_invalid_known_stay_blocks_connections_inside_its_full_interval(self):
        self.stop("a","2024-01-01T00:00:00Z","2024-01-01T01:00:00Z",0)
        self.stop("bad","2024-01-01T02:00:00Z","2024-01-01T12:00:00Z",1,metadata={"name":"unlocated stay"})
        self.stop("c","2024-01-01T05:00:00Z","2024-01-01T06:00:00Z",2)
        self.stop("d","2024-01-01T07:00:00Z","2024-01-01T08:00:00Z",3)
        self.assertEqual(trip_connections(self.database)["total"],0)

    def test_impossible_speed_and_zero_window_do_not_connect(self):
        self.stop("a","2024-01-01T00:00:00Z","2024-01-01T01:00:00Z",0)
        self.stop("b","2024-01-01T01:00:01Z","2024-01-01T02:00:00Z",90)
        result = trip_connections(self.database)
        self.assertEqual(result["total"],0)
        self.assertEqual(result["gaps"][0]["reason"],"implausible_minimum_speed")
        with connect(self.database) as conn:
            conn.execute("UPDATE events SET occurred_at='2024-01-01T01:00:00Z' WHERE id='b'")
        self.assertEqual(trip_connections(self.database)["gaps"][0]["reason"],"zero_travel_window")

    def test_exact_duplicate_visits_merge_and_preserve_original_event_ids(self):
        self.pair()
        self.stop("duplicate-a","2024-01-01T00:00:00Z","2024-01-01T01:00:00Z",0)
        result = trip_connections(self.database)
        self.assertEqual(result["total"],1)
        self.assertEqual(set(result["items"][0]["from"]["event_ids"]),{"a","duplicate-a"})
        self.assertEqual(result["coverage"][0]["duplicate_stops"],1)

    def test_coarse_and_unplaceable_times_are_explicit_barriers(self):
        self.stop("coarse","2024-01",None,0,precision="month")
        self.stop("a","2024-01-02T00:00:00Z","2024-01-02T01:00:00Z",1)
        self.stop("b","2024-01-03T00:00:00Z","2024-01-03T01:00:00Z",2)
        self.assertEqual(trip_connections(self.database)["total"],0)
        self.stop("unplaced",None,None,3,precision="unknown")
        result = trip_connections(self.database)
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["coverage"][0]["status"],"unplaced_time_barrier")

    def test_declared_month_precision_masks_month_even_if_stored_as_iso_datetime(self):
        self.stop("coarse","2024-01-01T00:00:00Z",None,0,precision="month")
        self.stop("a","2024-01-15T00:00:00Z","2024-01-15T01:00:00Z",1)
        self.stop("b","2024-01-16T00:00:00Z","2024-01-16T01:00:00Z",2)
        self.assertEqual(trip_connections(self.database)["total"],0)

    def test_dateline_uses_short_distance_and_unwrapped_display_geometry(self):
        self.stop("a","2024-01-01T00:00:00Z","2024-01-01T01:00:00Z",179)
        self.stop("b","2024-01-01T05:00:00Z","2024-01-01T06:00:00Z",-179)
        result = trip_connections(self.database)
        leg = result["items"][0]
        self.assertLess(leg["distance_km"],225)
        self.assertTrue(leg["crosses_dateline"])
        self.assertEqual(leg["to"]["longitude"],-179)
        self.assertEqual(leg["display_path"][-1],[0.0,181.0])

    def test_observation_fallback_requires_accuracy_and_ignores_unrelated_venues(self):
        self.stop("obs-a","2024-01-01T00:00:00Z",None,0,category="location",metadata={"latitude":0,"longitude":0,"accuracy":10})
        self.stop("obs-b","2024-01-01T05:00:00Z",None,1,category="location",metadata={"latitude":0,"longitude":1,"accuracy":10})
        self.stop("venue","2024-01-01T02:00:00Z","2024-01-01T03:00:00Z",80,category="event_booking")
        result = trip_connections(self.database)
        self.assertEqual(result["total"],1)
        self.assertEqual(result["coverage"][0]["mode"],"location")
        with connect(self.database) as conn:
            conn.execute("UPDATE event_locations SET metadata=? WHERE event_id='obs-b'",(json.dumps({"accuracy":1000}),))
        self.assertEqual(trip_connections(self.database)["total"],0)

    def test_readonly_query_works_during_a_wal_writer_without_creating_schema(self):
        empty = trip_connections(self.database)
        self.assertFalse(empty["coverage_complete"])
        with connect(self.database) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='event_locations'").fetchone())
        self.pair()
        writer = connect(self.database)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE events SET title='uncommitted title' WHERE id='a'")
            result = trip_connections(self.database)
            self.assertEqual(result["total"],1)
            self.assertEqual(result["items"][0]["from"]["title"],"a")
        finally:
            writer.rollback()
            writer.close()


if __name__ == "__main__":
    unittest.main()
