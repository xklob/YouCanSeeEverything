"""Fictional cache and journey data for isolated summary browser acceptance."""
from copy import deepcopy
import hashlib
from urllib.parse import parse_qs, urlsplit

from check_dashboard import fixture_api

PERIOD_KEY = "1" * 64
TRIP_KEY = "2" * 64
GENERATED_KEY = "3" * 64
TRIP_ID = "qa-journey"


def trip_summary_result(*, focus="", model="gpt-6.1-sol", source_ids=None, key=TRIP_KEY):
    sources = source_ids or []
    records = fixture_api("/api/events?limit=40")["items"]
    anchors = ["qa-cedar", "qa-alder"]
    coincident = [row["id"] for row in records if row["id"] not in anchors and
                  (not sources or row["source_id"] in sources)]
    result = deepcopy(fixture_api("/api/summaries?period=2024-04")["result"])
    result.update(kind="trip_summary", title="April 4–5 record summary", period=None, focus=focus, model=model,
        citations=anchors+coincident,
        answer="Two locations were recorded on April 4 and 5. Other records share that date window; their locations are unspecified.",
        sections=[dict(heading="Recorded stops", sentences=[dict(text="Two locations were recorded on April 4 and 5.", citations=anchors)]),
                  dict(heading="Other records in the date window", sentences=[dict(text="Other records share that date window; their locations are unspecified.", citations=coincident)])],
        trip_context=dict(trip_id=TRIP_ID, source_id="qa-one", source="QA journal",
            departure_at="2024-04-04T12:00:00Z", arrival_at="2024-04-05T12:00:00Z",
            window_start="2024-04-04", window_end="2024-04-05", window_end_exclusive="2024-04-06",
            time_basis="source_recorded_dates", inferred=True, route_known=False, mode_known=False,
            anchor_event_ids=anchors, coincident_event_ids=coincident, anchors_complete=True,
            journey_coverage_complete=True,
            notice="Recorded stops define an inferred connection. The route and mode of travel are unknown.",
            coincident_notice="Other records fall in the same date window. This does not establish that they occurred at the recorded stops."),
        selection=dict(period=None, start="2024-04-04", end_exclusive="2024-04-06", source_ids=sources,
                       warnings=[], time_basis="source_recorded_dates", focus=focus),
        evidence_ids=anchors+coincident, inference_performed=True, insufficient_evidence=False)
    result["evidence_fingerprint"] = hashlib.sha256(",".join(result["evidence_ids"]).encode()).hexdigest()
    result["coverage"].update(total_events_in_scope=len(result["citations"]), selected_events=len(result["citations"]),
                              candidate_events=len(result["citations"]))
    result["cache"].update(key=key)
    return result


def unchecked_saved(result):
    result["cache"].update(status="not_checked", current_watermark=None)
    return result


def journey_fixture():
    leg = fixture_api("/api/trips")["items"][0]
    return dict(id=TRIP_ID, source_id="qa-one", source="QA journal", departure_at=leg["departure_at"],
        arrival_at=leg["arrival_at"], legs=1, distance_km=leg["distance_km"], max_gap_hours=leg["gap_hours"],
        long_distance=False, represented_stops=2, first_stop=leg["from"], last_stop=leg["to"], summary_complete=True)


def summary_fixture_api(path):
    parsed, params = urlsplit(path), parse_qs(urlsplit(path).query)
    if parsed.path == "/api/journeys":
        return dict(items=[journey_fixture()], total=1, limit=20, offset=0, coverage_complete=True,
                    sources=[dict(id="qa-one", name="QA journal")])
    if parsed.path == "/api/journeys/" + TRIP_ID:
        return dict(journey=journey_fixture(), items=fixture_api("/api/trips")["items"], total=1,
                    limit=50, offset=0, truncated=False, complete=True, coverage_complete=True)
    if parsed.path == "/api/summaries/library":
        items = [dict(key=PERIOD_KEY, kind="period_summary", title="Saved synthetic monthly note", period="2024-04",
                      trip_id=None, focus="", model="gpt-6.1-sol", source_ids=[], generated_at="2024-04-06T12:00:00Z",
                      citation_count=1, evidence_count=4, freshness="not_checked"),
                 dict(key=TRIP_KEY, kind="trip_summary", title="April 4–5 record summary", period=None,
                      trip_id=TRIP_ID, focus="", model="gpt-6.1-sol", source_ids=[], generated_at="2024-04-06T11:00:00Z",
                      citation_count=4, evidence_count=4, freshness="not_checked")]
        offset, limit = int(params.get("offset", [0])[0]), int(params.get("limit", [20])[0])
        return dict(items=items[offset:offset+limit], offset=offset, limit=limit, has_more=offset+limit < len(items))
    if parsed.path.startswith("/api/summaries/saved/"):
        key = parsed.path.rsplit("/", 1)[-1]
        if key == PERIOD_KEY:
            result = fixture_api("/api/summaries?period=2024-04")["result"]
            result["cache"].update(key=key)
        elif key == TRIP_KEY:
            result = trip_summary_result()
        else:
            raise ValueError("Unknown synthetic saved summary")
        return dict(status="saved", freshness="not_checked", result=unchecked_saved(result))
    if parsed.path == "/api/summaries/trip":
        if params.get("trip_id") != [TRIP_ID]:
            raise ValueError("Unknown synthetic trip")
        focus, model = params.get("focus", [""])[0], params.get("model", ["gpt-6.1-sol"])[0]
        sources = params.get("source", [])
        missing = bool(focus.strip())
        return dict(status="missing" if missing else "saved", trip_id=TRIP_ID,
                    key=GENERATED_KEY if missing else TRIP_KEY, model=model,
                    result=None if missing else unchecked_saved(trip_summary_result(model=model, source_ids=sources)))
    return fixture_api(path)


def run_summary_acceptance(page, origin, report, passed):
    """Exercise only synthetic caches; generation is fulfilled inside the browser."""
    import json
    import re
    from playwright.sync_api import expect

    report["workflow"] = "saved_summaries_and_trip_summary"
    report["mocked_job_creations"] = []
    nav = page.get_by_role("complementary", name="Main navigation")
    nav.get_by_role("button", name=re.compile(r"^Ask\b")).click()
    page.get_by_text("Saved summaries", exact=True).click()
    page.get_by_role("button", name=re.compile(r"^Saved synthetic monthly note")).click()
    saved = page.get_by_role("region", name="Saved summary", exact=True)
    expect(saved.get_by_text("Saved · Sources not checked", exact=True)).to_be_visible()
    expect(saved.get_by_role("heading", name="Saved synthetic monthly note", exact=True)).to_be_visible()
    saved.get_by_role("button", name="Open cited record 1", exact=True).first.click()
    drawer = page.get_by_role("dialog")
    expect(drawer.get_by_role("heading", name="Alder acceptance visit", exact=True)).to_be_visible()
    expect(drawer.get_by_role("link", name=re.compile("Open original file"))).to_have_attribute("href", "/api/files/qa-file-qa-alder/download")
    page.keyboard.press("Escape")
    passed("saved_monthly_narrative_opens_cache_and_original_evidence")

    page.get_by_role("button", name=re.compile(r"^April 4–5 record summary")).click()
    window = saved.get_by_role("region", name="Trip summary window", exact=True)
    expect(window.get_by_text("The route and mode of travel are unknown.", exact=False)).to_be_visible()
    expect(window.get_by_text("This does not establish that they occurred at the recorded stops.", exact=False)).to_be_visible()
    window.get_by_text("Trip evidence", exact=True).click()
    anchors = window.locator("p").filter(has_text="Recorded-stop evidence")
    coincident = window.locator("p").filter(has_text="Records associated by time")
    expect(anchors.get_by_role("button")).to_have_count(2)
    expect(coincident.get_by_role("button")).to_have_count(2)
    coincident.get_by_role("button", name="Open cited record 3", exact=True).click()
    expect(drawer.get_by_role("heading", name="Birch practice session", exact=True)).to_be_visible()
    expect(drawer.get_by_alt_text("Synthetic attached image")).to_have_js_property("naturalWidth", 1)
    page.keyboard.press("Escape")
    passed("saved_trip_keeps_recorded_stops_separate_from_temporal_records_and_media")
    expect(page.get_by_role("button", name="Previous saved summaries", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Next saved summaries", exact=True)).to_be_disabled()
    assert not report["blocked_requests"] and not report["mocked_job_creations"], "Browsing attempted a model job"
    passed("summary_library_browsing_is_cache_only_and_does_not_check_freshness")

    nav.get_by_role("button", name="Trips", exact=True).click()
    selected = page.get_by_role("region", name="Selected trip", exact=True)
    selected.get_by_text("Summarize this trip", exact=True).click()
    form = selected.locator("details").filter(has=page.locator("summary", has_text="Summarize this trip"))
    expect(form.get_by_text("Opening does not run a model.", exact=False)).to_be_visible()
    expect(form.get_by_text("Other records in these dates do not establish trip activity or presence.", exact=False)).to_be_visible()
    expect(form.get_by_role("region", name="Trip summary window", exact=True)).to_be_visible()
    form.get_by_role("combobox", name="Trip summary sources", exact=True).select_option("qa-one")
    form.get_by_role("combobox", name="Trip summary model", exact=True).select_option("gpt-6-astra")
    focus = form.get_by_role("textbox", name="Trip summary focus", exact=True)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/summaries/trip" and
            parse_qs(urlsplit(response.url).query).get("focus") == ["Dates and source evidence"]):
        focus.fill("Dates and source evidence")
        focus.press("Tab")
    expect(form.get_by_text("No saved summary for this selection", exact=True)).to_be_visible()
    assert not report["blocked_requests"] and not report["mocked_job_creations"], "Opening trip controls attempted generation"
    passed("trip_cache_miss_preserves_explicit_generation_and_temporal_scope")

    def session(route):
        assert route.request.method == "GET"
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"token": "synthetic-session-only"}))
    def create_job(route):
        request = route.request
        assert request.method == "POST"
        payload = request.post_data_json
        assert payload == dict(mode="summarize_trip", trip_id=TRIP_ID, source_ids=["qa-one"],
                               prompt="Dates and source evidence", model="gpt-6-astra"), "Trip request changed its reviewed scope"
        report["mocked_job_creations"].append({"mode": payload["mode"], "trip_id": payload["trip_id"], "source_ids": payload["source_ids"]})
        route.fulfill(status=202, content_type="application/json", body=json.dumps({"id": "qa-trip-job", "status": "queued"}))
    def completed_job(route):
        assert route.request.method == "GET"
        route.fulfill(status=200, content_type="application/json", body=json.dumps(dict(id="qa-trip-job", status="completed",
            kind="summarize_trip", prompt="Dates and source evidence", created_at="2024-04-06T12:00:00Z",
            result=trip_summary_result(focus="Dates and source evidence", model="gpt-6-astra", source_ids=["qa-one"], key=GENERATED_KEY))))
    # Page routes fulfill these synthetic requests before the global GET-only
    # guard. Nothing reaches CandidateServer, a real session, or a model backend.
    page.route(origin+"/api/session", session)
    page.route(origin+"/api/assistant", create_job)
    page.route(origin+"/api/jobs/qa-trip-job", completed_job)
    form.get_by_role("button", name="Generate trip summary", exact=True).click()
    expect(form.get_by_role("heading", name="April 4–5 record summary", exact=True)).to_be_visible()
    expect(form.get_by_text("gpt-6-astra", exact=False).first).to_be_visible()
    assert len(report["mocked_job_creations"]) == 1, "One explicit action must create exactly one synthetic job"
    passed("explicit_generation_sends_only_trip_id_scope_focus_model_without_client_dates")
    expect(form.get_by_role("region", name="Trip summary window", exact=True)).to_be_visible()
    passed("mocked_completed_trip_job_renders_with_source_citations")
