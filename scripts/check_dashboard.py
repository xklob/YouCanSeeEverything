#!/usr/bin/env python3
"""Read-only browser acceptance for a built dashboard, never the user's browser.

Run with .venv/bin/python scripts/check_dashboard.py --dist web/dist --fixture
or add --api-url http://127.0.0.1:8000 to inspect actual local archive records.
Live acceptance also runs the synthetic map/media checks under output/fixture.
Artifacts contain private rendered records and must stay out of Git.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, ProxyHandler


FIXTURE_IMAGE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOioAAAAASUVORK5CYII=")
# A generated 16x16 VP8 test clip, embedded so acceptance needs no codec tools or network.
FIXTURE_VIDEO = base64.b64decode(
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwH/////////EU2bdKtNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHLTbuMU6uEElTDZ1OsggEY7AEAAAAAAABoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmpSrXsYMPQkBNgIxMYXZmNjIuMy4xMDBXQYxMYXZmNjIuMy4xMDAWVK5ryK4BAAAAAAAAP9eBAXPFiI2wH6Z7pQoznIEAIrWcg3VuZIiBAIaFVl9WUDiDgQEj44OEAmJaAOCQsIEQuoEQmoECVbCEVbmBARJUw2fXc3OfY8CAZ8iZRaOHRU5DT0RFUkSHjExhdmY2Mi4zLjEwMHNzsmPAi2PFiI2wH6Z7pQozZ8ihRaOHRU5DT0RFUkSHlExhdmM2Mi4xMS4xMDAgbGlidnB4H0O2dUDL54EAo7yBAACAsAIAnQEqEAAQAABHCIWFiIWEiAICAnWqA/gCDP0oAP7/TRL//FhX8WFfxYV/8WFf/PzO7cX85gCjlYEAKACxAQAFEKwAGAAYWC/0AAgAAKOVgQBQALEBAAUQrAAYABhYL/QACAAAo5WBAHgAsQEABRCsABgAGFgv9AAIAACjlYEAoACxAQAFEKwAGAAYWC/0AAgAAKOVgQDIALEBAAUQrAAYABhYL/QACAAAo5WBAPAAsQEABRCsABgAGFgv9AAIAAA=")
MOCK_TILE = b'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf2f7"/><path d="M0 80H256M0 180H256M60 0V256M180 0V256" stroke="#d7e2ef"/></svg>'
SAFE_MEDIA = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
              "video/mp4", "video/webm", "video/ogg"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/", "/api", "/api/"}):
        raise ValueError("--api-url must be an HTTP loopback API origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def fixture_api(path: str) -> dict:
    """Deterministic, entirely synthetic records for candidate revision checks."""
    parsed = urlsplit(path)
    params = parse_qs(parsed.query)
    records = [
        {"id": "qa-alder", "title": "Alder acceptance visit", "body": "Synthetic explicitly located evidence.",
         "occurred_at": "2024-04-05T12:00:00Z", "source_id": "qa-one", "source": "QA journal",
         "category": "location", "time_precision": "second"},
        {"id": "qa-birch", "title": "Birch practice session", "body": "Synthetic same-day record without coordinates.",
         "occurred_at": "2024-04-05T11:00:00Z", "source_id": "qa-two", "source": "QA calendar",
         "category": "post", "time_precision": "second", "media": [
             {"file_id": "qa-image", "url": "/api/files/qa-image/media", "mime": "image/png", "kind": "image", "caption": "Synthetic attached image"},
             {"file_id": "qa-video", "url": "/api/files/qa-video/media", "mime": "video/webm", "kind": "video", "caption": "Synthetic attached video"},
             {"file_id": "qa-missing", "url": "/api/files/qa-missing/media", "mime": "image/png", "kind": "image", "caption": "Synthetic unavailable image"}]},
    ]
    sources = [dict(id=f"qa-{suffix}", name=name, status="complete", file_count=1,
                    event_count=1, bytes=128, first_event=record["occurred_at"],
                    last_event=record["occurred_at"], notes=[])
               for suffix, name, record in zip(("one", "two"), ("QA journal", "QA calendar"), records)]
    if parsed.path == "/api/overview":
        return dict(total_events=2, sources_count=2, files_count=2, stored_bytes=256,
                    date_range={"start": records[1]["occurred_at"], "end": records[0]["occurred_at"]},
                    activity=[{"month": r["occurred_at"][:7], "count": 1} for r in records],
                    categories=[{"name": r["category"], "count": 1} for r in records], undated_count=0)
    if parsed.path == "/api/sources":
        return {"items": sources}
    if parsed.path in {"/api/imports", "/api/jobs"}:
        return {"items": []}
    if parsed.path in {"/api/events", "/api/map"}:
        selected = records
        for key, field in (("source", "source_id"), ("category", "category")):
            if params.get(key, [""])[0]:
                selected = [r for r in selected if r[field] == params[key][0]]
        if params.get("q", [""])[0]:
            selected = [r for r in selected if params["q"][0].casefold() in
                        (r["title"] + " " + r["body"]).casefold()]
        if params.get("undated") == ["true"]:
            selected = []
        for key, relation in (("from", lambda date, value: date >= value),
                              ("to", lambda date, value: date[:10] <= value)):
            if params.get(key, [""])[0]:
                selected = [r for r in selected if relation(r["occurred_at"], params[key][0])]
        if parsed.path == "/api/map":
            points = [dict(id="qa-point", event_id=r["id"], latitude=37.54, longitude=-77.44,
                           occurred_at=r["occurred_at"], title=r["title"], source=r["source"],
                           kind="explicit_coordinates", time_precision="second")
                      for r in selected if r["id"] == "qa-alder"]
            return {"items": points, "total": len(points)}
        selected = sorted(selected, key=lambda r: r["occurred_at"],
                          reverse=params.get("sort", ["desc"])[0] != "asc")
        offset, limit = int(params.get("offset", [0])[0]), int(params.get("limit", [40])[0])
        return {"items": selected[offset:offset + limit], "total": len(selected),
                "limit": limit, "offset": offset}
    for record in records:
        if parsed.path == "/api/events/" + record["id"]:
            return {**record, "provenance": [{"file_id": "qa-file-" + record["id"],
                    "path": "synthetic/example.json", "sha256": "a" * 64, "locator": "record 1"}]}
    raise ValueError("Unsupported fixture endpoint")


class CandidateServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, dist: Path, upstream: str | None):
        self.dist, self.upstream = dist, upstream
        self.violations: list[dict] = []
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        super().__init__(("127.0.0.1", 0), CandidateHandler)

    def api(self, path: str) -> dict:
        # Do not expose a session token or download private file objects to candidate code.
        if not re.fullmatch(r"/api/(overview|sources|imports|jobs|events|map)(/[^/?]+)?", urlsplit(path).path):
            raise ValueError("Endpoint is outside the read-only acceptance API")
        if self.upstream is None:
            return fixture_api(path)
        with self.opener.open(Request(self.upstream + path, method="GET"), timeout=15) as response:
            if response.status != 200:
                raise ValueError("Upstream API did not return HTTP 200")
            payload = response.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024:
            raise ValueError("API response exceeds acceptance limit")
        return json.loads(payload)

    def media(self, path: str, requested_range: str | None):
        """Read approved inert local media, with a bounded range for videos."""
        if not re.fullmatch(r"/api/files/[^/?]+/media", urlsplit(path).path):
            raise ValueError("Invalid media endpoint")
        if self.upstream is None:
            fixture = {"/api/files/qa-image/media": (FIXTURE_IMAGE, "image/png"),
                       "/api/files/qa-video/media": (FIXTURE_VIDEO, "video/webm")}.get(path)
            if fixture is None:
                return 404, b'{"detail":"Media not found"}', "application/json", {}
            payload, mime = fixture
            if requested_range:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested_range)
                if not match:
                    raise ValueError("Unsupported fixture media range")
                start = int(match[1]); end = min(int(match[2]) if match[2] else len(payload) - 1, len(payload) - 1)
                if start > end:
                    return 416, b"", mime, {"Content-Range": f"bytes */{len(payload)}"}
                return 206, payload[start:end + 1], mime, {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{len(payload)}"}
            return 200, payload, mime, {"Accept-Ranges": "bytes"}
        headers = {}
        if requested_range:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested_range)
            if not match:
                raise ValueError("Unsupported media range")
            start = int(match[1]); end = min(int(match[2]) if match[2] else start + 16 * 1024**2 - 1,
                                             start + 16 * 1024**2 - 1)
            headers["Range"] = f"bytes={start}-{end}"
        with self.opener.open(Request(self.upstream + path, headers=headers, method="GET"), timeout=20) as response:
            mime = response.headers.get_content_type()
            if mime not in SAFE_MEDIA:
                raise ValueError("Upstream media is not an approved inert browser format")
            payload = response.read(16 * 1024**2 + 1)
            if len(payload) > 16 * 1024**2:
                raise ValueError("Media response exceeds acceptance limit")
            response_headers = {key: response.headers[key] for key in ("Content-Range", "Accept-Ranges") if key in response.headers}
            return response.status, payload, mime, response_headers


class CandidateHandler(BaseHTTPRequestHandler):
    server: CandidateServer

    def log_message(self, *_):
        pass

    def respond(self, status: int, payload: bytes, content_type="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://tile.openstreetmap.org; "
                         "font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; object-src 'none'; "
                         "base-uri 'self'; form-action 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reject(self):
        self.server.violations.append({"method": self.command, "path": urlsplit(self.path).path})
        self.respond(405, b'{"detail":"Acceptance server allows GET only"}')

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_CONNECT = do_TRACE = reject

    def do_GET(self):
        if self.path.startswith("/api/"):
            try:
                if re.fullmatch(r"/api/files/[^/?]+/media", self.path):
                    self.respond(*self.server.media(self.path, self.headers.get("Range")))
                else:
                    self.respond(200, json.dumps(self.server.api(self.path)).encode())
            except (ValueError, HTTPError, OSError) as error:
                self.respond(502, json.dumps({"detail": type(error).__name__}).encode())
            return
        target = (self.server.dist / unquote(urlsplit(self.path).path).lstrip("/")).resolve()
        if not target.is_relative_to(self.server.dist):
            self.respond(403, b"{}"); return
        if target.is_dir():
            target /= "index.html"
        if not target.is_file():
            if not target.suffix:
                target = self.server.dist / "index.html"
            else:
                self.respond(404, b"{}"); return
        self.respond(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")


def run(args: argparse.Namespace, report: dict) -> None:
    from playwright.sync_api import sync_playwright, expect

    expect.set_options(timeout=args.timeout * 1000)

    dist = args.dist.resolve()
    if not (dist / "index.html").is_file():
        raise ValueError("Candidate dist must contain index.html")
    server = CandidateServer(dist, None if args.fixture else api_origin(args.api_url))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    report.update(mode="fixture" if args.fixture else "live_read_only", dist_path=str(dist), checks=[],
                  page_errors=[], browser_console_errors=[], expected_console_errors=[],
                  blocked_requests=[], mocked_tile_requests=0, screenshots=[])
    checks = report["checks"]

    def passed(name: str, **details):
        checks.append({"name": name, "passed": True, **details})

    try:
        initial = server.api("/api/events?limit=40&offset=0&sort=desc")
        if not initial.get("items"):
            raise ValueError("Acceptance requires at least one known API record; use --fixture for an empty archive")
        known = initial["items"][0]
        detail = server.api("/api/events/" + quote(known["id"], safe=""))
        if not detail.get("provenance"):
            raise ValueError("Known API record must have original-file provenance")
        sources = server.api("/api/sources")["items"]
        points = server.api("/api/map?limit=2000")
        report["record_fingerprint"] = hashlib.sha256(known["id"].encode()).hexdigest()
        report["initial_total"] = initial["total"]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-background-networking"])
            context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                          service_workers="block", accept_downloads=False,
                                          reduced_motion="reduce")

            def guard(route):
                request = route.request
                parsed = urlsplit(request.url)
                if (request.method == "GET" and parsed.scheme == "https" and
                        parsed.hostname == "tile.openstreetmap.org"):
                    # OSM forbids automated tile fetching. This never contacts the tile service.
                    report["mocked_tile_requests"] += 1
                    route.fulfill(status=200, content_type="image/svg+xml", body=MOCK_TILE)
                    return
                allowed = request.method == "GET" and f"{parsed.scheme}://{parsed.netloc}" == origin
                if allowed:
                    route.continue_()
                else:
                    report["blocked_requests"].append({"method": request.method, "origin": parsed.netloc,
                                                        "path": parsed.path})
                    route.abort("blockedbyclient")

            context.route("**/*", guard)
            if hasattr(context, "route_web_socket"):
                def reject_socket(socket):
                    report["blocked_requests"].append({"method": "WEBSOCKET"})
                    socket.close()
                context.route_web_socket("**/*", reject_socket)
            page = context.new_page()
            page.set_default_timeout(args.timeout * 1000)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            expected_map_failure = False

            def console_error(message):
                if message.type != "error":
                    return
                path = urlsplit(message.location.get("url", "")).path
                expected = args.fixture and ((path == "/api/files/qa-missing/media" and "404" in message.text)
                            or (expected_map_failure and path == "/api/map" and "503" in message.text))
                report["expected_console_errors" if expected else "browser_console_errors"].append(message.text)

            page.on("console", console_error)

            def screenshot(name: str):
                destination = args.output / name
                page.screenshot(path=str(destination), full_page=True, animations="disabled")
                os.chmod(destination, 0o600)
                report["screenshots"].append(name)

            def event_response(response):
                return urlsplit(response.url).path == "/api/events" and response.request.method == "GET"

            def change(action, key: str, value: str):
                with page.expect_response(lambda response: event_response(response) and
                        parse_qs(urlsplit(response.url).query).get(key, [""])[0] == value) as pending:
                    action()
                response = pending.value
                if response.status != 200:
                    raise AssertionError("Timeline filter API request failed")
                payload = response.json()
                expect(page.get_by_role("region", name="Timeline results")).to_have_attribute("aria-busy", "false")
                return payload

            page.goto(origin, wait_until="networkidle")
            workspace = page.get_by_role("region", name="Map and same-day timeline", exact=True)
            expect(workspace).to_be_visible()
            expect(workspace.get_by_role("heading", name="Places in your history", exact=True)).to_be_visible()
            expect(workspace.get_by_text("their location is not inferred", exact=False)).to_be_visible()
            if points["items"]:
                expect(page.get_by_role("region", name=re.compile("^Map of explicitly recorded locations"))).to_be_visible()
            else:
                expect(workspace.get_by_role("heading", name="No explicit locations in this selection")).to_be_visible()
            passed("default_map_renders_api_state", recorded_points=points["total"])

            if args.fixture:
                location_article = workspace.get_by_role("article").filter(has=page.get_by_role("heading", name="Alder acceptance visit", exact=True))
                context_article = workspace.get_by_role("article").filter(has=page.get_by_role("heading", name="Birch practice session", exact=True))
                expect(location_article.get_by_text("Selected location record", exact=True)).to_be_visible()
                expect(context_article.get_by_text("Selected location record", exact=True)).to_have_count(0)
                expect(context_article.get_by_text("Open original context", exact=True)).to_be_visible()
                expect(workspace.get_by_text("This does not establish that a post was written", exact=False)).to_be_visible()
                expect(location_article.get_by_role("region", name="Attached images and videos")).to_have_count(0)
                passed("same_day_context_does_not_invent_location_or_media_associations")

                image = context_article.get_by_alt_text("Synthetic attached image")
                image.scroll_into_view_if_needed()
                expect(image).to_be_visible()
                expect(image).to_have_js_property("naturalWidth", 1)
                video = context_article.get_by_label("Synthetic attached video")
                expect(video).to_be_visible()
                expect(video).to_have_attribute("preload", "none")
                if video.get_attribute("controls") is None or video.get_attribute("autoplay") is not None:
                    raise AssertionError("Video controls/autoplay boundary changed")
                video.evaluate("element => { element.preload = 'metadata'; element.load(); }")
                expect(video).to_have_js_property("videoWidth", 16)
                passed("explicit_local_image_and_video_decode_without_autoplay")

                context_article.get_by_role("button", name=re.compile("^Show .* more attachments")).click()
                expect(context_article.get_by_role("link", name=re.compile("Preview unavailable"))).to_be_visible()
                passed("unavailable_media_keeps_original_link")
                expect(page.locator(".leaflet-control-attribution")).to_contain_text("OpenStreetMap")
                if report["mocked_tile_requests"] == 0:
                    raise AssertionError("Basemap never requested locally fulfilled tiles")
                screenshot("map.png")
                page.get_by_role("checkbox", name="OpenStreetMap basemap").uncheck()
                expect(workspace.get_by_text("Basemap hidden.", exact=False)).to_be_visible()
                passed("optional_basemap_and_attribution_use_only_mock_tiles")

                page.get_by_role("button", name="View location evidence", exact=True).click()
                expect(page.get_by_role("dialog").get_by_role("link", name=re.compile("Open original file"))).to_have_attribute("href", "/api/files/qa-file-qa-alder/download")
                page.keyboard.press("Escape")
                passed("map_evidence_opens_original_selected_record")

                filters = page.get_by_role("button", name=re.compile(r"^filters", re.I))
                if filters.get_attribute("aria-expanded") != "true":
                    filters.click()
                type_filter = page.get_by_role("combobox", name="Filter by type")
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/map" and
                                          parse_qs(urlsplit(response.url).query).get("category") == ["post"]):
                    type_filter.select_option("post")
                expect(workspace.get_by_role("heading", name="No explicit locations in this selection")).to_be_visible()
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/map" and
                                          not parse_qs(urlsplit(response.url).query).get("category")):
                    type_filter.select_option("")
                expect(page.get_by_role("button", name="View location evidence")).to_be_visible()
                passed("map_filters_preserve_unlocated_records_for_timeline")

                # One controlled API failure must expose a retry path and then recover.
                expected_map_failure = True
                failure_pending = True

                def one_map_failure(route):
                    nonlocal failure_pending
                    if failure_pending:
                        failure_pending = False
                        route.fulfill(status=503, content_type="application/json", body='{"detail":"Synthetic map retry check"}')
                    else:
                        route.fallback()

                page.route(origin + "/api/map?*", one_map_failure)
                page.get_by_role("button", name="Refresh archive", exact=True).click()
                expect(workspace.get_by_role("alert")).to_be_visible()
                workspace.get_by_role("button", name="Try again", exact=True).click()
                expect(page.get_by_role("button", name="View location evidence")).to_be_visible()
                page.unroute(origin + "/api/map?*", one_map_failure)
                passed("map_api_failure_is_visible_and_retry_recovers")

                page.set_viewport_size({"width": 390, "height": 844})
                expect(workspace.get_by_role("heading", name="Places in your history")).to_be_visible()
                if not page.evaluate("document.documentElement.scrollWidth <= innerWidth"):
                    raise AssertionError("Map overflows mobile viewport")
                screenshot("map-mobile.png")
                page.set_viewport_size({"width": 1440, "height": 1000})
                passed("mobile_map_has_no_horizontal_overflow")
            else:
                screenshot("map.png")

            page.get_by_role("button", name="Timeline", exact=True).click()
            results = page.get_by_role("region", name="Timeline results")
            expect(results).to_have_attribute("aria-busy", "false")
            title = results.get_by_role("heading", name=known["title"], exact=True).first
            expect(title).to_be_visible()
            passed("known_api_record_visible", api_total=initial["total"])
            screenshot("timeline.png")

            if args.fixture:
                inline = results.get_by_role("article").filter(has=page.get_by_role("heading", name="Birch practice session", exact=True)).first
                inline.scroll_into_view_if_needed()
                expect(inline.get_by_alt_text("Synthetic attached image")).to_have_js_property("naturalWidth", 1)
                inline.get_by_role("heading", name="Birch practice session", exact=True).click()
                media_drawer = page.get_by_role("dialog")
                expect(media_drawer.get_by_alt_text("Synthetic attached image")).to_have_js_property("naturalWidth", 1)
                expect(media_drawer.get_by_label("Synthetic attached video")).to_be_visible()
                expect(media_drawer.get_by_role("link", name=re.compile("Preview unavailable"))).to_be_visible()
                screenshot("media.png")
                page.keyboard.press("Escape")
                passed("inline_and_drawer_media_preserve_explicit_record_attachment")

            search = page.get_by_role("searchbox", name=re.compile("search.*timeline", re.I))
            # A deliberately absent query proves results are filtered, rather than merely loading a card.
            missing = "qa_missing_" + hashlib.sha256(os.urandom(24)).hexdigest()
            filtered = change(lambda: search.fill(missing), "q", missing)
            assert filtered["total"] == 0, "Absent query unexpectedly matched API records"
            expect(results.get_by_role("button").filter(has=page.get_by_role("heading"))).to_have_count(0)
            passed("search_removes_nonmatching_records")

            # Find a title token confirmed by API before typing it, including Unicode titles.
            tokens = re.findall(r"[^\W_]+", known["title"], re.UNICODE)
            query = next((token for token in tokens if len(token) >= 3), tokens[0] if tokens else known["title"])
            expected = server.api("/api/events?" + urlencode({"q": query, "limit": 40, "sort": "desc"}))
            if not any(item["id"] == known["id"] for item in expected["items"]):
                raise AssertionError("Known record not returned by its title search token")
            filtered = change(lambda: search.fill(query), "q", query)
            assert filtered["total"] == expected["total"], "Browser search result differs from API"
            expect(results.get_by_role("button").filter(has=page.get_by_role("heading"))).to_have_count(len(filtered["items"]))
            expect(results.get_by_role("heading", name=known["title"], exact=True).first).to_be_visible()
            passed("search_restores_known_api_record", matching_total=filtered["total"])

            filters = page.get_by_role("button", name=re.compile(r"^filters", re.I))
            if filters.get_attribute("aria-expanded") != "true":
                filters.click()
            filtered = change(lambda: page.get_by_role("combobox", name="Filter by source").select_option(known["source_id"]), "source", known["source_id"])
            assert filtered["items"] and all(item["source_id"] == known["source_id"] for item in filtered["items"])
            expect(results.get_by_role("button").filter(has=page.get_by_role("heading"))).to_have_count(len(filtered["items"]))
            passed("source_filter_matches_api", matching_total=filtered["total"])
            filtered = change(lambda: page.get_by_role("combobox", name="Filter by type").select_option(known["category"]), "category", known["category"])
            assert filtered["items"] and all(item["category"] == known["category"] for item in filtered["items"])
            expect(results.get_by_role("button").filter(has=page.get_by_role("heading"))).to_have_count(len(filtered["items"]))
            passed("category_filter_matches_api", matching_total=filtered["total"])

            results.get_by_role("button").filter(has=page.get_by_role("heading", name=known["title"], exact=True)).first.click()
            drawer = page.get_by_role("dialog")
            expect(drawer).to_be_visible()
            expect(drawer.get_by_role("heading", name=known["title"], exact=True)).to_be_visible()
            original = drawer.get_by_role("link", name=re.compile("open original file", re.I)).first
            expected_href = "/api/files/" + quote(detail["provenance"][0]["file_id"], safe="") + "/download"
            expect(original).to_have_attribute("href", expected_href)
            passed("evidence_drawer_has_matching_original_link")
            screenshot("evidence.png")
            page.keyboard.press("Escape")
            expect(drawer).to_have_count(0)
            passed("evidence_drawer_closes_with_escape")

            page.get_by_role("button", name=re.compile(r"^source library", re.I)).click()
            for source in sources[:3]:
                expect(page.get_by_role("heading", name=source["name"], exact=True).first).to_be_visible()
            expect(page.get_by_role("button", name=re.compile("browse timeline", re.I)).first).to_be_visible()
            passed("source_library_renders_api_sources", source_total=len(sources))
            screenshot("sources.png")
            assert not report["page_errors"], "Browser page errors occurred"
            assert not report["browser_console_errors"], "Browser console errors occurred"
            assert not report["blocked_requests"] and not server.violations, "Candidate attempted prohibited network access"
            passed("no_runtime_errors_or_prohibited_requests")
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        report["server_violations"] = server.violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("web/dist"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fixture", action="store_true", help="Use only synthetic acceptance records")
    mode.add_argument("--api-url", default="http://127.0.0.1:8000", help="Existing loopback API, GET only")
    parser.add_argument("--output", type=Path, default=Path(".local/browser-acceptance"), help="Private artifact directory")
    parser.add_argument("--timeout", type=int, default=20, help="Browser assertion timeout in seconds")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.output, 0o700)
    report = {"passed": False, "started_at": time.time()}
    started = time.monotonic()
    try:
        run(args, report)
        if not args.fixture:
            # Actual imports can legitimately contain no map/media yet. Always exercise those
            # branches against safe synthetic records before accepting an automatic revision.
            fixture_args = argparse.Namespace(**vars(args))
            fixture_args.fixture = True
            fixture_args.output = args.output / "fixture"
            fixture_args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
            fixture_report = {"passed": False, "started_at": time.time()}
            report["fixture_acceptance"] = fixture_report
            fixture_started = time.monotonic()
            try:
                run(fixture_args, fixture_report)
                fixture_report["passed"] = True
            except Exception as error:
                fixture_report["error"] = {"type": type(error).__name__, "detail": str(error)}
                raise
            finally:
                fixture_report["elapsed_seconds"] = round(time.monotonic() - fixture_started, 3)
                (fixture_args.output / "result.json").write_text(json.dumps(fixture_report, indent=2) + "\n")
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "detail": str(error)}
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result = args.output / "result.json"
    result.write_text(json.dumps(report, indent=2) + "\n")
    os.chmod(result, 0o600)
    print(json.dumps({"passed": report["passed"], "checks": len(report.get("checks", [])),
                      "result": str(result), "elapsed_seconds": report["elapsed_seconds"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
