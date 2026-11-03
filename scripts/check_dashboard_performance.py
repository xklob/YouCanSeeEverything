#!/usr/bin/env python3
"""Focused pending-count/pagination fixture and one bounded live view pass.

Uses an owned, clean headless browser and the GET-only candidate proxy. Google
configuration is projected to no-key OSM; tile requests are fulfilled locally.
No search/count probes, model jobs, user browser, or persistent profile.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from check_dashboard import CandidateServer, MOCK_TILE, api_origin, fixture_api


def performance_fixture_api(path):
    parsed = urlsplit(path)
    params = parse_qs(parsed.query)
    pending = {"status": "pending", "measured_at": None, "watermark": None}
    if parsed.path == "/api/journeys":
        return dict(items=[], total=0, limit=20, offset=0, coverage_complete=False, sources=[])
    value = fixture_api(path)
    if parsed.path == "/api/overview":
        return {**value, "total_events": None, "undated_count": None,
                "date_range": {"start": None, "end": None}, "activity": [],
                "categories": [{**row, "count": None} for row in value["categories"]], "stats": pending}
    if parsed.path == "/api/sources":
        return {"items": [{**row, "event_count": None, "first_event": None, "last_event": None,
                           "stats": pending} for row in value["items"]], "stats": pending}
    if parsed.path == "/api/events" and not any(params.get(key) for key in ("from", "to", "q", "source", "category", "undated")):
        records = fixture_api("/api/events?limit=40")["items"] + [dict(id=f"qa-pending-{index:03}", title=f"Pending record {index:03}",
            body="Synthetic pagination record.", occurred_at="2024-04-03T10:00:00Z", source_id="qa-one",
            source="QA journal", category="note", time_precision="second") for index in range(1, 39)]
        offset, limit = int(params.get("offset", [0])[0]), int(params.get("limit", [40])[0])
        return dict(items=records[offset:offset+limit], total=None, has_more=offset+limit < len(records),
                    total_status="pending", counted_at=None, offset=offset, limit=limit)
    return value


def run(args, report):
    from playwright.sync_api import expect, sync_playwright

    dist = args.dist.resolve()
    if not (dist / "index.html").is_file():
        raise ValueError("Built candidate index is missing")
    server = CandidateServer(dist, None if args.fixture else api_origin(args.api_url), fixture_handler=performance_fixture_api)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    report.update(checks=[], errors=[], blocked_requests=[], mocked_tiles=0,
                  mode="fixture" if args.fixture else "limited_live_read_only",
                  candidate_index_sha256=hashlib.sha256((dist/"index.html").read_bytes()).hexdigest())

    def passed(name, **details):
        report["checks"].append(dict(name=name, passed=True, **details))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-background-networking"])
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce",
                                          service_workers="block", accept_downloads=False)
            def guard(route):
                request, parsed = route.request, urlsplit(route.request.url)
                if request.method == "GET" and parsed.scheme == "https" and parsed.hostname == "tile.openstreetmap.org":
                    report["mocked_tiles"] += 1
                    route.fulfill(status=200, content_type="image/svg+xml", body=MOCK_TILE)
                elif request.method == "GET" and f"{parsed.scheme}://{parsed.netloc}" == origin:
                    route.continue_()
                else:
                    report["blocked_requests"].append({"method": request.method, "path": parsed.path})
                    route.abort("blockedbyclient")
            context.route("**/*", guard)
            if hasattr(context, "route_web_socket"):
                def reject_socket(socket):
                    report["blocked_requests"].append({"method": "WEBSOCKET"})
                    socket.close()
                context.route_web_socket("**/*", reject_socket)
            page = context.new_page()
            page.set_default_timeout(args.timeout*1000)
            expect.set_options(timeout=args.timeout*1000)
            page.on("pageerror", lambda error: report["errors"].append({"kind": "page", "detail": str(error)}))
            def console(message):
                if message.type != "error":
                    return
                path = urlsplit(message.location.get("url", "")).path
                if args.fixture and path == "/api/files/qa-missing/media" and "404" in message.text:
                    return
                report["errors"].append({"kind": "console", "detail": message.text})
            page.on("console", console)
            def endpoint(path):
                return lambda response: urlsplit(response.url).path == path and response.request.method == "GET"
            def count_text_is_clean():
                text = page.locator(".timeline-stats,.results-heading,.pagination,.source-card-count,.map-point-count,.nav-count").all_text_contents()
                assert not any(re.search(r"NaN|undefined|\[object Object\]|^\s*\{", value) for value in text), "Invalid total display"
            def screenshot(name):
                path = args.output / name
                page.screenshot(path=str(path), full_page=True, animations="disabled")
                path.chmod(0o600)

            with page.expect_response(endpoint("/api/overview")) as overview_response:
                page.goto(origin, wait_until="domcontentloaded")
            overview = overview_response.value.json()
            map_view = page.get_by_role("region", name="Map and same-day timeline", exact=True)
            expect(map_view).to_be_visible()
            expect(page.get_by_role("combobox", name="Map provider")).to_have_value("openstreetmap")
            passed("map_view_renders_with_isolated_no_key_provider")
            modes = page.get_by_role("group", name="Archive view")
            with page.expect_response(endpoint("/api/events")) as records_response:
                modes.get_by_role("button", name="Records", exact=True).click()
            records = records_response.value.json()
            results = page.get_by_role("region", name="Timeline results")
            expect(results).to_have_attribute("aria-busy", "false")
            count_text_is_clean()
            passed("records_render_bounded_page", returned=len(records["items"]), total_status=records.get("total_status"))
            if args.fixture:
                assert overview["total_events"] is None and records["total"] is None
                expect(page.get_by_text("Unknown", exact=True).first).to_be_visible()
                expect(results.get_by_text("40 shown", exact=True)).to_be_visible()
                expect(results.get_by_text("Counts pending", exact=True)).to_be_visible()
                expect(results.get_by_text("0 records", exact=True)).to_have_count(0)
                expect(page.get_by_role("combobox", name="Filter by type").get_by_role("option", name="Location", exact=True)).to_have_count(1)
                passed("pending_totals_and_names_only_types_do_not_imply_zero")
                with page.expect_response(lambda response: endpoint("/api/events")(response) and
                        parse_qs(urlsplit(response.url).query).get("offset") == ["40"]) as next_response:
                    results.get_by_role("button", name="Next", exact=True).click()
                assert len(next_response.value.json()["items"]) == 2
                expect(results.get_by_text("41–42 shown", exact=True)).to_be_visible()
                expect(results.get_by_role("button", name="Next", exact=True)).to_be_disabled()
                expect(results.get_by_role("button", name="Previous", exact=True)).to_be_enabled()
                with page.expect_response(lambda response: endpoint("/api/events")(response) and
                        parse_qs(urlsplit(response.url).query).get("offset") == ["0"]):
                    results.get_by_role("button", name="Previous", exact=True).click()
                expect(results.get_by_role("button", name="Next", exact=True)).to_be_enabled()
                passed("unknown_total_pagination_uses_has_more_and_returns_to_first_page")
            screenshot("records.png")
            with page.expect_response(endpoint("/api/days")) as days_response:
                modes.get_by_role("button", name="Days", exact=True).click()
            assert days_response.value.status == 200
            daily = page.get_by_role("region", name="Daily summaries and source evidence", exact=True)
            expect(daily).to_be_visible()
            expect(daily.get_by_role("heading", name="Days", exact=True)).to_be_visible()
            passed("days_view_renders_projected_data")
            modes.get_by_role("button", name="Map", exact=True).click()
            expect(map_view).to_be_visible()
            passed("map_remount_remains_usable")
            with page.expect_response(endpoint("/api/journeys")) as trips_response:
                page.get_by_role("complementary", name="Main navigation").get_by_role("button", name="Trips", exact=True).click()
            assert trips_response.value.status == 200
            trips = page.get_by_role("region", name="Trips", exact=True)
            expect(trips).to_be_visible()
            expect(trips.get_by_text("Loading trips…", exact=True)).to_have_count(0)
            passed("trips_view_renders_bounded_journey_page")
            if args.fixture:
                page.get_by_role("complementary", name="Main navigation").get_by_role("button", name="Sources", exact=True).click()
                expect(page.get_by_role("heading", name="QA journal", exact=True)).to_be_visible()
                expect(page.locator(".source-card-count").first).to_contain_text("Unknown")
                expect(page.get_by_text("Counts pending", exact=True).first).to_be_visible()
                passed("source_pending_counts_remain_explicit")
            count_text_is_clean()
            assert not report["errors"] and not report["blocked_requests"] and not server.violations, "Runtime or network boundary failure"
            passed("no_invalid_count_displays_runtime_errors_or_outbound_requests")
            context.close()
            browser.close()
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("web/dist"))
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path(".local/browser-acceptance/performance"))
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.chmod(0o700)
    report = {"passed": False}
    start = time.monotonic()
    try:
        run(args, report)
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "detail": str(error)}
    report["elapsed_seconds"] = round(time.monotonic()-start, 3)
    destination = args.output / "result.json"
    destination.write_text(json.dumps(report, indent=2)+"\n"); destination.chmod(0o600)
    print(json.dumps({"passed": report["passed"], "checks": len(report.get("checks", [])), "result": str(destination), "elapsed_seconds": report["elapsed_seconds"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
