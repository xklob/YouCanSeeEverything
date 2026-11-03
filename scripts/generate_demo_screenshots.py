#!/usr/bin/env python3
"""Generate public README screenshots using fictional API data only.

Run from the application checkout after building web/dist:
  .venv/bin/python scripts/generate_demo_screenshots.py

No live API, database, existing browser, profile, photo library or map provider is
used. The post illustration and schematic map tiles are generated locally.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlsplit

from check_dashboard import CandidateServer


ILLUSTRATION = """<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="440" viewBox="0 0 1100 440">
<defs><linearGradient id="sky" x2="0" y2="1"><stop stop-color="#cadce7"/><stop offset="1" stop-color="#f5d6b2"/></linearGradient><linearGradient id="water" x2="0" y2="1"><stop stop-color="#81b4c3"/><stop offset="1" stop-color="#3e798e"/></linearGradient></defs>
<rect width="1100" height="440" fill="url(#sky)"/><circle cx="790" cy="112" r="43" fill="#f7cf8f"/>
<path d="M0 217L95 155 166 183 251 112 350 188 439 146 544 211 643 181 723 215 815 181 940 220 1100 185V310H0Z" fill="#97b0bc"/>
<path d="M0 239L98 198 180 232 281 184 380 236 494 211 591 244 692 222 800 242 921 211 1100 245V315H0Z" fill="#6f94a5"/>
<rect y="267" width="1100" height="173" fill="url(#water)"/>
<path d="M0 303H200M82 332H376M490 301H807M695 342H1041M355 378H664M66 407H431M800 402H1100" stroke="#c5d9d9" stroke-width="2" opacity=".43"/>
<g fill="#527986"><path d="M755 268V217H779V268M785 268V190H812V268M821 268V226H842V268M849 268V207H874V268M885 268V233H914V268M925 268V213H949V268M962 268V241H998V268"/><path d="M788 191L798 172 808 191"/></g>
<path d="M587 343L668 343 650 356 605 356Z" fill="#334e60"/><path d="M627 271V343M622 277L593 332H622Z" stroke="#e8e6d9" stroke-width="3" fill="#e8e6d9"/>
<path d="M0 401L275 361 393 375 0 440Z" fill="#485b60"/><path d="M0 419L285 377M69 398L97 429M152 386L174 415M228 376L250 399" stroke="#708185" stroke-width="2"/>
<g fill="#405e68"><path d="M18 402L55 195 93 402Z"/><path d="M33 326L0 326 46 266 12 266 57 200 101 266 73 266 118 326 82 326Z"/><path d="M1047 440L1081 233 1114 440Z"/><path d="M1065 351L1027 351 1075 278 1118 351 1091 351Z"/></g>
</svg>"""


def demo_records():
    records = [
        dict(id="demo-post-waterfront", title="Saturday concert", body="Met friends at the waterfront venue. Set started at 8.", occurred_at="2025-04-19T20:30:00Z", source_id="demo-journal", source="Journal", category="post", time_precision="second", media=[dict(file_id="demo-illustration", url="/api/files/demo-illustration/media", mime="image/png", kind="image", caption="Waterfront illustration · demo data")]),
        dict(id="demo-calendar-music", title="Lantern Room concert", body="Doors at 7 p.m. Set starts at 8 p.m.", occurred_at="2025-04-19T19:00:00Z", source_id="demo-calendar", source="Calendar", category="event_booking", time_precision="second"),
        dict(id="demo-portland", title="Portland stop", body="Recorded location: Portland, April 19 at 2:30 p.m.", occurred_at="2025-04-19T14:30:00Z", source_id="demo-locations", source="Recorded places", category="location", time_precision="second"),
        dict(id="demo-olympia", title="Olympia stop", body="Recorded location: Olympia, April 18 at 11 a.m.", occurred_at="2025-04-18T11:00:00Z", source_id="demo-locations", source="Recorded places", category="location", time_precision="second"),
        dict(id="demo-seattle-water", title="Seattle waterfront", body="Recorded location: Seattle waterfront, April 17 at 11 a.m.", occurred_at="2025-04-17T11:00:00Z", source_id="demo-locations", source="Recorded places", category="location", time_precision="second"),
        dict(id="demo-seattle", title="Seattle stop", body="Recorded location: Seattle, April 17 at 9 a.m.", occurred_at="2025-04-17T09:00:00Z", source_id="demo-locations", source="Recorded places", category="location", time_precision="second"),
        dict(id="demo-note-synth", title="Synth rehearsal notes", body="Recorded a four-minute arpeggio. Lower the bass level on the next take.", occurred_at="2025-04-15T18:00:00Z", source_id="demo-notes", source="Notes", category="note", time_precision="day"),
    ]
    entries = [
        ("Trail walk", "Walked the west loop. The north entrance was closed."),
        ("Coffee with Sam", "Meet at the cafe on Third Street at 10 a.m."),
        ("Band rehearsal", "Rehearsal booked for 6 p.m. Bring the keyboard stand."),
        ("Grocery list", "Rice, tomatoes, eggs, coffee."),
        ("Library visit", "Returned two books. Check the next due date on May 6."),
        ("Pasta recipe", "200 g pasta, one can of tomatoes, two cloves of garlic."),
        ("Listening notes", "Compare the first and second mixes on headphones."),
        ("Call with Morgan", "Discussed the rehearsal schedule. Follow up on Tuesday."),
        ("Synth patch", "Saved patch 12 with a slower filter envelope."),
        ("Train booking", "Saturday departure at 9:20 a.m. Seat reservation saved."),
        ("Neighborhood meeting", "Meeting starts at 7 p.m. in the library room."),
        ("Project notes", "Replace the broken switch before the next test."),
    ]
    for index, (title, body) in enumerate(entries):
        day = datetime(2025, 4, 12) - timedelta(days=index * 19)
        source_id, source, category = [("demo-journal", "Journal", "post"), ("demo-notes", "Notes", "note"), ("demo-calendar", "Calendar", "event_booking")][index % 3]
        records.append(dict(id=f"demo-memory-{index}", title=title, body=body, occurred_at=day.date().isoformat(), source_id=source_id, source=source, category=category, time_precision="day"))
    return records


class DemoServer(CandidateServer):
    """A fixture-only server whose API cannot delegate to any upstream."""

    def __init__(self, dist, image):
        super().__init__(dist, None)
        self.opener = None
        self.image = image
        self.records = demo_records()
        by_id = {record["id"]: record for record in self.records}
        coordinates = {"demo-seattle": (47.6062, -122.3321), "demo-seattle-water": (47.6055, -122.3425), "demo-olympia": (47.0379, -122.9007), "demo-portland": (45.5152, -122.6784)}
        self.points = [dict(id="point-" + record_id, event_id=record_id, latitude=latitude, longitude=longitude, occurred_at=by_id[record_id]["occurred_at"], title=by_id[record_id]["title"], source="Recorded places", source_id="demo-locations", kind="explicit_coordinates", time_precision="second") for record_id, (latitude, longitude) in coordinates.items()]
        self.trips = []
        for index, (start, end) in enumerate(zip(self.points, self.points[1:])):
            lat1, lat2 = map(math.radians, (start["latitude"], end["latitude"]))
            dlat, dlon = lat2 - lat1, math.radians(end["longitude"] - start["longitude"])
            km = 6371 * 2 * math.asin(math.sqrt(math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2))
            gap = (datetime.fromisoformat(end["occurred_at"].replace("Z", "+00:00")) - datetime.fromisoformat(start["occurred_at"].replace("Z", "+00:00"))).total_seconds() / 3600
            self.trips.append(dict(id=f"demo-connection-{index}", source="Recorded places", source_id="demo-locations", **{"from": start, "to": end}, departure_at=start["occurred_at"], arrival_at=end["occurred_at"], gap_hours=gap, distance_km=round(km, 1), inferred=True, geometry_type="straight_line"))

    def api(self, path):
        parsed = urlsplit(path); params = parse_qs(parsed.query)
        if parsed.path == "/api/map-config":
            return dict(provider="openstreetmap", google_maps_configured=False)
        if parsed.path in {"/api/imports", "/api/jobs"}:
            return {"items": []}
        if parsed.path == "/api/overview":
            dates = sorted(record["occurred_at"] for record in self.records)
            return dict(total_events=len(self.records), sources_count=4, files_count=len(self.records) + 1, stored_bytes=182400, date_range=dict(start=dates[0], end=dates[-1]), activity=[dict(month=month, count=count) for month, count in sorted(Counter(r["occurred_at"][:7] for r in self.records).items())], categories=[dict(name=name, count=count) for name, count in Counter(r["category"] for r in self.records).items()], undated_count=0)
        if parsed.path == "/api/sources":
            items = []
            for source_id, name in [("demo-journal", "Journal"), ("demo-calendar", "Calendar"), ("demo-locations", "Recorded places"), ("demo-notes", "Notes")]:
                own = [record for record in self.records if record["source_id"] == source_id]
                items.append(dict(id=source_id, name=name, status="complete", file_count=len(own), event_count=len(own), bytes=45600, first_event=min(r["occurred_at"] for r in own), last_event=max(r["occurred_at"] for r in own), notes=["Demo data. Fictional source records."]))
            return {"items": items}
        if parsed.path.startswith("/api/events/"):
            record = next((r for r in self.records if r["id"] == parsed.path.rsplit("/", 1)[-1]), None)
            if record is None:
                raise ValueError("Unknown fictional record")
            return {**record, "metadata": {"demo": True, "author": "Robin Hart (fictional)", "original_context": "Demo data"}, "provenance": [dict(file_id="original-" + record["id"], path=f"illustrative-demo/{record['source_id']}/{record['occurred_at'][:10]}.json", locator="Fictional demo record.", sha256="d" * 64)]}
        selected = list(self.records)
        for key, field in (("source", "source_id"), ("category", "category")):
            if params.get(key, [""])[0]:
                selected = [record for record in selected if record[field] == params[key][0]]
        if params.get("q", [""])[0]:
            selected = [record for record in selected if params["q"][0].casefold() in (record["title"] + " " + record["body"]).casefold()]
        for key, comparison in (("from", lambda day, value: day >= value), ("to", lambda day, value: day[:10] <= value)):
            if params.get(key, [""])[0]:
                selected = [record for record in selected if comparison(record["occurred_at"], params[key][0])]
        ids = {record["id"] for record in selected}
        if parsed.path == "/api/map":
            points = [point for point in self.points if point["event_id"] in ids]
            return dict(items=list(reversed(points)), total=len(points))
        if parsed.path == "/api/trips":
            trips = [trip for trip in self.trips if trip["from"]["event_id"] in ids and trip["to"]["event_id"] in ids]
            return dict(items=trips, total=len(trips), truncated=False, gaps=[], gaps_total=0)
        if parsed.path == "/api/events":
            selected.sort(key=lambda record: record["occurred_at"], reverse=params.get("sort", ["desc"])[0] != "asc")
            limit, offset = int(params.get("limit", [40])[0]), int(params.get("offset", [0])[0])
            return dict(items=selected[offset:offset + limit], total=len(selected), limit=limit, offset=offset)
        raise ValueError("Endpoint is outside the fictional demo API")

    def media(self, path, requested_range):
        if path != "/api/files/demo-illustration/media":
            return 404, b"{}", "application/json", {}
        return 200, self.image, "image/png", {}


def schematic_tile(url):
    """Generate an abstract coast and grid, never provider cartography."""
    match = re.search(r"/(\d+)/(\d+)/(\d+)\.png", urlsplit(url).path)
    if not match:
        raise ValueError("Unexpected mock tile path")
    z, x, y = map(int, match.groups()); scale = 2 ** z * 256
    points = []
    for dy in range(0, 257, 8):
        global_y = (y * 256 + dy) / scale
        coast = (0.1572 + 0.0017 * math.sin(global_y * 260) + 0.0006 * math.sin(global_y * 920)) * scale - x * 256
        points.append(f"{coast:.2f},{dy}")
    polygon = " ".join(points) + " 2048,256 2048,0"
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#dcebf1"/><polygon points="{polygon}" fill="#edf1e8"/><path d="M0 64H256M0 128H256M0 192H256M64 0V256M128 0V256M192 0V256" stroke="#ffffff" stroke-opacity=".65" stroke-width="2"/><path d="M0 64H256M0 128H256M0 192H256M64 0V256M128 0V256M192 0V256" stroke="#c9d5d3" stroke-opacity=".25" stroke-width=".7"/></svg>'.encode()


def main():
    from playwright.sync_api import sync_playwright, expect
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("web/dist"))
    parser.add_argument("--output", type=Path, default=Path("docs/screenshots"))
    parser.add_argument("--report", type=Path, default=Path(".local/reports/readme-screenshots.json"))
    args = parser.parse_args(); dist = args.dist.resolve()
    if not (dist / "index.html").is_file():
        raise SystemExit("Build the dashboard before generating demo screenshots")
    args.output.mkdir(parents=True, exist_ok=True)
    report = dict(fictional_data_only=True, live_api_used=False, database_accessed=False, existing_browser_used=False, external_requests_sent=0, basemap="Locally generated schematic, not provider cartography", blocked_requests=[], locally_mocked_tiles=0, api_requests=0, errors=[], artifacts=[])
    server = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-background-networking"])
        context = browser.new_context(viewport=dict(width=1520, height=1120), device_scale_factor=1, reduced_motion="reduce", service_workers="block", accept_downloads=False)
        illustration = context.new_page()
        illustration.route("**/*", lambda route: route.abort())
        illustration.set_content(ILLUSTRATION)
        image = illustration.locator("svg").screenshot(animations="disabled")
        illustration.close()
        server = DemoServer(dist, image)
        assert server.upstream is None and server.opener is None
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"

        def guard(route):
            request = route.request; parsed = urlsplit(request.url)
            if request.method == "GET" and parsed.scheme == "https" and parsed.hostname == "tile.openstreetmap.org":
                report["locally_mocked_tiles"] += 1
                route.fulfill(status=200, content_type="image/svg+xml", body=schematic_tile(request.url)); return
            if request.method == "GET" and f"{parsed.scheme}://{parsed.netloc}" == origin:
                report["api_requests"] += parsed.path.startswith("/api/")
                route.continue_(); return
            report["blocked_requests"].append(dict(method=request.method, origin=parsed.netloc))
            route.abort("blockedbyclient")

        context.route("**/*", guard)
        context.route_web_socket("**/*", lambda socket: socket.close())
        page = context.new_page()
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        page.on("console", lambda message: report["errors"].append(message.text) if message.type == "error" else None)
        page.set_default_timeout(15000)
        try:
            page.goto(origin, wait_until="networkidle")
            expect(page.get_by_role("heading", name=re.compile(r"^(Locations|Places in your history)$"))).to_be_visible()
            expect(page.get_by_text("Browse 3 inferred connections", exact=True)).to_be_visible()
            page.evaluate("""() => { const badge=document.createElement('div'); badge.id='demo-label'; badge.textContent='Demo data'; document.body.append(badge); }""")
            page.add_style_tag(content="#demo-label{position:fixed;right:95px;top:17px;z-index:99999;padding:8px 13px;border:1px solid #bad7ca;background:#edf8f2;color:#426e59;border-radius:7px;font:600 12px/1.2 system-ui;box-shadow:0 2px 8px #17324d08}.topbar-right>span{visibility:hidden}")

            def capture(name, clip=None):
                destination = args.output / name
                page.screenshot(path=str(destination), animations="disabled", clip=clip)
                destination.chmod(0o644)
                report["artifacts"].append(dict(path=str(destination), bytes=destination.stat().st_size, sha256=hashlib.sha256(destination.read_bytes()).hexdigest()))

            page.get_by_role("group", name="Archive view").get_by_role("button", name=re.compile(r"^(Records|Timeline)$")).click()
            image_locator = page.get_by_alt_text("Waterfront illustration · demo data")
            expect(image_locator).to_have_js_property("naturalWidth", 1100)
            page.evaluate("scrollTo(0,0)")
            capture("timeline.png")
            page.get_by_role("group", name="Archive view").get_by_role("button", name=re.compile(r"^(Map|Map & day context)$")).click()
            page.set_viewport_size(dict(width=1520, height=1320))
            clear_location = page.get_by_role("button", name="Clear selected location", exact=True)
            expect(clear_location).to_be_visible(); clear_location.click()
            summary = page.get_by_text("Browse 3 inferred connections", exact=True)
            expect(summary).to_be_visible(); summary.click()
            page.get_by_role("button", name=re.compile("Olympia stop.*Portland stop")).click()
            summary.click()
            expect(page.get_by_role("region", name="Inferred trip connection details", exact=True)).to_be_visible()
            page.wait_for_timeout(250)
            page.get_by_role("button", name="Fit recorded points", exact=True).click()
            page.wait_for_timeout(250)
            page.get_by_role("button", name="Zoom out", exact=True).click()
            page.wait_for_timeout(500)
            page.evaluate("""() => { document.querySelector('.leaflet-control-attribution').textContent='Schematic demo basemap · no map service contacted'; const wrap=document.querySelector('.map-canvas-wrap'); const label=document.createElement('div'); label.className='demo-map-label'; label.textContent='Demo data · schematic basemap'; wrap.append(label); }""")
            page.add_style_tag(content=".demo-map-label{position:absolute;left:48px;top:14px;z-index:800;padding:7px 9px;border-radius:5px;background:#ffffffed;color:#587181;font:600 9px/1.2 system-ui;letter-spacing:.8px}")
            workspace = page.get_by_role("region", name="Map and same-day timeline", exact=True)
            workspace.evaluate("element => window.scrollTo(0, element.getBoundingClientRect().top + scrollY - 80)")
            page.wait_for_timeout(200)
            box = workspace.bounding_box()
            detail_box = page.get_by_role("region", name="Inferred trip connection details", exact=True).bounding_box()
            capture("map.png", dict(x=box["x"], y=box["y"], width=box["width"], height=detail_box["y"] + detail_box["height"] - box["y"]))
            page.set_viewport_size(dict(width=1520, height=1120))
            page.get_by_role("group", name="Archive view").get_by_role("button", name=re.compile(r"^(Records|Timeline)$")).click()
            page.get_by_role("heading", name="Saturday concert", exact=True).click()
            drawer = page.get_by_role("dialog")
            expect(drawer.get_by_role("heading", name=re.compile(r"^(Original evidence|Evidence)$"))).to_be_visible()
            expect(drawer.get_by_alt_text("Waterfront illustration · demo data")).to_have_js_property("naturalWidth", 1100)
            box = drawer.bounding_box()
            capture("evidence.png", dict(x=box["x"] - 80, y=0, width=box["width"] + 80, height=1120))
            assert not report["errors"] and not report["blocked_requests"] and not server.violations
            assert all(record["id"].startswith("demo-") for record in server.records)
            assert set(path.name for path in args.output.glob("*.png")) >= {"timeline.png", "map.png", "evidence.png"}
            report["passed"] = True
        finally:
            context.close(); browser.close(); server.shutdown(); server.server_close(); worker.join(timeout=5)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n"); args.report.chmod(0o600)
    print(json.dumps(dict(passed=report.get("passed", False), screenshots=len(report["artifacts"]), external_requests_sent=0, locally_mocked_tiles=report["locally_mocked_tiles"], report=str(args.report))))


if __name__ == "__main__":
    main()
