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
        self.assertEqual(fixture_api("/api/events")["total"], 2)
        self.assertEqual(fixture_api("/api/events?q=alder")["total"], 1)
        self.assertEqual(fixture_api("/api/events?source=qa-one&category=activity")["total"], 0)
        self.assertEqual(fixture_api("/api/events?from=2024-01-01")["total"], 2)
        self.assertEqual(fixture_api("/api/map")["total"], 1)
        self.assertEqual(fixture_api("/api/map?category=post")["total"], 0)
        context = fixture_api("/api/events?from=2024-04-05&to=2024-04-05")["items"]
        self.assertEqual(len(context), 2)
        self.assertTrue(all("latitude" not in event for event in context))
        self.assertNotIn("media", next(event for event in context if event["id"] == "qa-alder"))
        self.assertIn("media", next(event for event in context if event["id"] == "qa-birch"))

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
                    self.assertEqual(json.load(response)["total"], 2)
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
