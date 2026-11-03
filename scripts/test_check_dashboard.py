"""Acceptance harness boundaries, without launching a browser or reading archives."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from check_dashboard import CandidateServer, api_origin, fixture_api


class AcceptanceBoundaryTests(unittest.TestCase):
    def test_upstream_is_loopback_without_userinfo_or_extra_path(self):
        self.assertEqual(api_origin("http://127.0.0.1:8000/api"), "http://127.0.0.1:8000")
        for value in ("https://example.com", "http://example.com", "http://127.0.0.1@evil.test",
                      "http://user:pass@localhost", "file:///tmp/data", "http://localhost/secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api_origin(value)

    def test_synthetic_search_and_filters_reduce_records(self):
        self.assertEqual(fixture_api("/api/events")["total"], 4)
        self.assertEqual(fixture_api("/api/events?q=alder")["total"], 1)
        self.assertEqual(fixture_api("/api/events?source=qa-one&category=activity")["total"], 0)
        self.assertEqual(fixture_api("/api/events?from=2024-01-01")["total"], 4)
        self.assertEqual(fixture_api("/api/map")["total"], 2)
        self.assertEqual(fixture_api("/api/map?category=post")["total"], 0)
        context = fixture_api("/api/events?from=2024-04-05&to=2024-04-05")["items"]
        self.assertEqual(len(context), 3)
        self.assertTrue(all("latitude" not in event for event in context))
        self.assertNotIn("media", next(event for event in context if event["id"] == "qa-alder"))
        self.assertIn("media", next(event for event in context if event["id"] == "qa-birch"))
        trips = fixture_api("/api/trips")
        self.assertEqual(trips["total"], 1)
        self.assertTrue(trips["items"][0]["inferred"])
        paged_ids = {point["event_id"] for point in fixture_api("/api/map")["items"]}
        self.assertNotIn(trips["items"][0]["from"]["event_id"], paged_ids)
        self.assertEqual(fixture_api("/api/trips?category=post")["total"], 0)

    def test_live_map_configuration_is_projected_without_fetching_a_key(self):
        with tempfile.TemporaryDirectory() as folder:
            server = CandidateServer(Path(folder), "http://127.0.0.1:9")
            try:
                # A closed upstream port proves this response never fetches live configuration.
                self.assertEqual(server.api("/api/map-config"), {
                    "provider": "openstreetmap", "google_maps_configured": False,
                })
            finally:
                server.server_close()

    def test_coverage_and_daily_fixtures_keep_evidence_and_progress_distinct(self):
        coverage = fixture_api("/api/coverage")
        self.assertFalse(coverage["continuous_coverage_asserted"])
        self.assertEqual(coverage["totals"]["events"], 4)
        progress = coverage["active_imports"][0]
        self.assertEqual(progress["files"]["fraction"], .25)
        self.assertEqual(progress["bytes"]["fraction"], .75)
        self.assertNotEqual(progress["events_reported"], coverage["totals"]["events"])
        self.assertEqual(coverage["backup"]["status"], "not_supplied")
        self.assertEqual(fixture_api("/api/days")["total"], 2)
        day = fixture_api("/api/days/2024-04-05?source=qa-two")
        self.assertEqual([record["id"] for record in day["items"]], ["qa-birch"])
        self.assertEqual(fixture_api("/api/days/2024-04-03")["total"], 0)
        saved = fixture_api("/api/summaries?period=2024-04")
        self.assertEqual(saved["result"]["citations"], ["qa-alder"])
        self.assertEqual(fixture_api("/api/events/qa-alder")["id"], saved["result"]["citations"][0])
        focused = fixture_api("/api/summaries?period=2024-04&focus=fictional")
        self.assertEqual(focused["status"], "missing")
        self.assertIsNone(focused["result"])
        self.assertNotEqual(focused["key"], saved["key"])
        raw_types = {row["name"] for row in fixture_api("/api/overview")["categories"]}
        derived = fixture_api("/api/daily/categories")
        self.assertIn("health_sample", {row["name"] for row in derived["items"]})
        self.assertNotIn("health_sample", raw_types)
        health = fixture_api("/api/days/2024-04-05?category=health_sample")
        self.assertEqual(health["summary"]["raw_samples"], 1)
        self.assertEqual(health["items"], [])
        self.assertEqual(health["rollups"][0]["metric"], "heart_rate")
        samples = fixture_api("/api/rollups/qa-heart-rate")
        self.assertEqual([record["id"] for record in samples["items"]], ["qa-pulse"])

    def test_candidate_cannot_mutate_get_tokens_download_files_or_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dist = root / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<h1>synthetic</h1>")
            (root / "outside.txt").write_text("must not be served")
            (dist / "escape.txt").symlink_to(root / "outside.txt")
            server = CandidateServer(dist, None)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(origin + "/api/events") as response:
                    self.assertEqual(json.load(response)["total"], 4)
                for path in ("/api/session", "/api/files/id/download"):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                        urlopen(origin + path)
                    self.assertEqual(error.exception.code, 502)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(origin + "/api/assistant", data=b"{}", method="POST"))
                self.assertEqual(error.exception.code, 405)
                self.assertEqual(server.violations[0]["method"], "POST")
                with self.assertRaises(HTTPError) as error:
                    urlopen(origin + "/escape.txt")
                self.assertEqual(error.exception.code, 403)
                with urlopen(origin + "/api/files/qa-image/media") as response:
                    self.assertEqual(response.headers.get_content_type(), "image/png")
                    self.assertTrue(response.read().startswith(b"\x89PNG"))
                with urlopen(Request(origin + "/api/files/qa-video/media", headers={"Range": "bytes=0-9"})) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(len(response.read()), 10)
                    self.assertTrue(response.headers["Content-Range"].startswith("bytes 0-9/"))
                with self.assertRaises(HTTPError) as error:
                    urlopen(origin + "/api/files/qa-missing/media")
                self.assertEqual(error.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
