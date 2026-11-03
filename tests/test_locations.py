import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
import time
from unittest.mock import patch

from biography import locations
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
        self.assertEqual({key:result[key] for key in ('events','points')}, {"events": 2, "points": 2})
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
        result=reconcile_locations(self.database)
        self.assertEqual({key:result[key] for key in ('events','points')}, {"events":0,"points":0})
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

    def test_concurrent_writer_after_discovery_is_rechecked_without_snapshot_upgrade(self):
        self.event('changed',{'location':None},category='post')
        original=locations._discover_location_rows
        def commit_after_read(*args,**kwargs):
            rows=original(*args,**kwargs)
            with connect(self.database) as conn:
                conn.execute("UPDATE events SET metadata=? WHERE id='changed'",(json.dumps({'latitude':0,'longitude':12}),))
            return rows
        with patch.object(locations,'_discover_location_rows',side_effect=commit_after_read):result=reconcile_locations(self.database)
        self.assertEqual(result['points'],1);self.assertEqual(result['discovery']['checkpoint_rowid'],1)
        self.assertEqual(map_points(self.database)['items'][0]['latitude'],0)

    def test_failed_batch_rolls_back_points_and_checkpoint_and_resumes(self):
        for identity in ('a','b'):self.event(identity,{'latitude':1,'longitude':2})
        original=locations._write_location_rows
        def fail_after_writes(*args,**kwargs):
            original(*args,**kwargs);raise RuntimeError('synthetic interruption')
        with patch.object(locations,'_write_location_rows',side_effect=fail_after_writes):
            with self.assertRaisesRegex(RuntimeError,'synthetic interruption'):reconcile_locations(self.database)
        self.assertEqual(locations.location_discovery_status(self.database)['checkpoint_rowid'],0)
        self.assertEqual(map_points(self.database)['total'],0)
        self.assertEqual(reconcile_locations(self.database)['points'],2)

    def test_null_false_candidates_are_skipped_and_all_recognized_structures_are_discovered(self):
        invalid=[{'location':None},{'location':{'latitude':None,'longitude':None}},{'lat':False,'lon':1}, {'candidates':[{'latitude':1,'longitude':2}]}]
        valid=[{'latE7':0,'lngE7':10000000},{'latLng':'geo:1,2'},{'startLocation':{'lat':1,'lng':2}},
            {'visit':{'topCandidate':{'placeLocation':{'latLng':'geo:1,2'}}}},
            {'activity':{'end':{'lat':1,'lon':2}}},{'timelinePath':[{'point':'geo:1,2'}]},
            {'waypointPath':{'waypoints':[{'lat':1,'lng':2}]}},{'rawPath':{'points':[{'lat':1,'lng':2}]}},
            {'transitPath':{'transitStops':[{'latitude':1,'longitude':2}]}}]
        for index,value in enumerate(invalid+valid):self.event(str(index),value,category='post')
        result=reconcile_locations(self.database)
        self.assertEqual(result['events'],len(valid));self.assertEqual(result['points'],len(valid))
        with connect(self.database) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM location_index_state').fetchone()[0],len(valid))

    def test_physical_limit_precedes_source_filter_and_fixed_target_resumes(self):
        for identity in ('a','b'):self.event(identity,{},source='source-b',category='record')
        self.event('c',{'latitude':1,'longitude':2})
        first=reconcile_locations(self.database,source_id='source-a',batch_size=2)
        self.assertEqual(first['scanned_in_run'],2);self.assertEqual(first['events'],0);self.assertTrue(first['has_more'])
        self.event('d',{'latitude':3,'longitude':4})
        second=reconcile_locations(self.database,source_id='source-a',batch_size=2)
        self.assertEqual(second['discovery']['watermark_rowid'],3);self.assertEqual(second['points'],1);self.assertTrue(second['has_more'])
        final=reconcile_locations(self.database,source_id='source-a',batch_size=2)
        self.assertFalse(final['has_more']);self.assertEqual(final['points'],1)

    def test_explicit_800_ids_are_all_refreshed_and_removed_coordinates_clear(self):
        with connect(self.database) as conn:
            conn.executemany("INSERT INTO events(id,source_id,title,category,metadata,created_at) VALUES(?,'source-a','Synthetic','post',?,'now')",[(str(i),json.dumps({'lat':1,'lng':2})) for i in range(800)])
        result=reconcile_locations(self.database,event_ids=[str(i) for i in range(800)])
        self.assertEqual(result['points'],800)
        with connect(self.database) as conn:conn.execute("UPDATE events SET metadata='{}' WHERE id='0'")
        reconcile_locations(self.database,event_ids=['0'])
        self.assertEqual(map_points(self.database)['total'],799)

    def test_large_valid_path_is_retained_in_full_without_point_cap(self):
        self.event('path',{'timelinePath':[{'point':'geo:1,2'} for _ in range(10001)]},category='post')
        self.assertEqual(reconcile_locations(self.database)['points'],10001)

    def test_rowid_and_explicit_id_plans_are_bounded_without_source_sort(self):
        with connect(self.database) as conn:
            conn.executemany("INSERT INTO events(id,source_id,title,category,metadata,created_at) VALUES(?,'source-a','Synthetic','document','{}','now')",[(str(i),) for i in range(5000)])
            rows=locations._discover_location_rows(conn,0,5000,5000,8*1024**2)
            self.assertEqual(len(rows),5000)
            sql='SELECT e.rowid,e.id FROM events e NOT INDEXED WHERE e.rowid>? AND e.rowid<=? ORDER BY e.rowid LIMIT ?'
            plan=[row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+sql,(0,5000,5000))]
            self.assertTrue(any('INTEGER PRIMARY KEY' in step for step in plan));self.assertFalse(any('TEMP B-TREE' in step or 'events_source' in step for step in plan))
            plan=[row[3] for row in conn.execute('EXPLAIN QUERY PLAN SELECT id FROM events INDEXED BY sqlite_autoindex_events_1 WHERE id IN ('+','.join('?' for _ in range(500))+')',[str(i) for i in range(500)])]
            self.assertTrue(any('sqlite_autoindex_events_1' in step for step in plan));self.assertFalse(any('events_category' in step for step in plan))
        started=time.monotonic();result=reconcile_locations(self.database,batch_size=5000,max_seconds=10)
        self.assertEqual(result['scanned_in_run'],5000);self.assertEqual(result['events'],0)
        self.assertLess(time.monotonic()-started,5)

    def test_drain_preserves_refresh_until_reset_commit(self):
        replies=[{'events':0,'points':0,'scanned_in_run':0,'paused':'catalog_busy','reset_applied':False,'has_more':True,'discovery':{}},
                 {'events':0,'points':0,'scanned_in_run':1,'paused':None,'reset_applied':True,'has_more':False,'discovery':{}}]
        with patch.object(locations,'reconcile_locations',side_effect=replies) as called,patch.object(locations.time,'sleep'):
            locations.drain_locations(self.database,refresh=True)
        self.assertTrue(all(call.kwargs['refresh'] for call in called.call_args_list))

    def test_busy_reset_does_not_erase_or_advance_committed_checkpoint(self):
        self.event('point',{'latitude':1,'longitude':2})
        reconcile_locations(self.database)
        writer=connect(self.database);writer.execute('BEGIN IMMEDIATE')
        try:paused=reconcile_locations(self.database,refresh=True)
        finally:writer.rollback();writer.close()
        self.assertEqual(paused['paused'],'catalog_busy');self.assertFalse(paused['reset_applied'])
        self.assertEqual(paused['discovery']['checkpoint_rowid'],1)

    def test_reviewed_enrichment_updates_locations_behind_discovery_checkpoint(self):
        from biography.enrichment import _apply
        self.event('old',{},category='post')
        reconcile_locations(self.database)
        payload={'occurred_at':'2024-03-01','end_at':None,'time_precision':'day','location':{'latitude':12,'longitude':34},
            'provenance':[{'url':'https://events.example/synthetic'}],
            'original_values':{'occurred_at':'2024-02-03T12:00:00Z','end_at':None,'time_precision':'second'}}
        with connect(self.database) as conn:self.assertTrue(_apply(conn,'old',payload))
        points=map_points(self.database)['items']
        self.assertEqual(len(points),1);self.assertEqual(points[0]['occurred_at'],'2024-03-01')
        self.assertEqual(points[0]['metadata']['enrichment_provenance'],payload['provenance'])

    def test_postprocessing_failure_preserves_raw_completion_and_resume_clears_error(self):
        from biography import pipeline
        from biography.storage import ImportPaused
        root=Path(self.temp.name)/'export';root.mkdir()
        dataset={'id':'source-a','name':'Synthetic','root':str(root)}
        with patch.object(locations,'drain_locations',side_effect=ImportPaused('synthetic_busy')):
            with self.assertRaises(ImportPaused):pipeline.run_dataset(dataset,db_path=self.database,data_dir=Path(self.temp.name)/'vault')
        with connect(self.database) as conn:
            row=conn.execute('SELECT * FROM imports').fetchone();checkpoint=json.loads(row['checkpoint'])
            self.assertEqual(row['status'],'ready');self.assertTrue(checkpoint['raw_complete']);self.assertFalse(checkpoint['postprocessing_complete'])
            self.assertEqual(conn.execute("SELECT status FROM sources WHERE id='source-a'").fetchone()[0],'ready')
        self.assertTrue(pipeline.mark_postprocessing_complete('source-a',self.database))
        with connect(self.database) as conn:
            row=conn.execute('SELECT * FROM imports').fetchone()
            self.assertIsNone(row['error']);self.assertTrue(json.loads(row['checkpoint'])['postprocessing_complete'])

    def test_raw_ingestion_failure_remains_paused_without_raw_completion(self):
        from biography import pipeline,importer
        from biography.storage import ImportPaused
        root=Path(self.temp.name)/'export';root.mkdir();(root/'source.txt').write_text('Synthetic retained fixture')
        with patch.object(importer,'ingest_path',side_effect=ImportPaused('synthetic_capacity')):
            with self.assertRaises(ImportPaused):pipeline.run_dataset({'id':'source-a','name':'Synthetic','root':str(root)},db_path=self.database,data_dir=Path(self.temp.name)/'vault')
        with connect(self.database) as conn:
            row=conn.execute('SELECT * FROM imports').fetchone()
            self.assertEqual(row['status'],'paused');self.assertFalse(json.loads(row['checkpoint'])['raw_complete'])

    def test_drain_finishes_fixed_cut_without_chasing_unrelated_appends(self):
        reply={'events':0,'points':0,'scanned_in_run':2,'paused':None,'reset_applied':False,'has_more':True,
            'discovery':{'status':'complete','watermark_rowid':2,'checkpoint_rowid':2,'current_rowid':3}}
        with patch.object(locations,'reconcile_locations',return_value=reply) as called:
            result=locations.drain_locations(self.database)
        self.assertEqual(called.call_count,1);self.assertEqual(result['drained_watermark_rowid'],2)


if __name__ == "__main__":
    unittest.main()
