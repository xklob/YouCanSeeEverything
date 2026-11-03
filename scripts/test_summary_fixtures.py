from summary_fixtures import PERIOD_KEY, TRIP_KEY, TRIP_ID, summary_fixture_api, trip_summary_result


def test_saved_library_paginates_and_never_claims_current_freshness():
    first = summary_fixture_api("/api/summaries/library?limit=1&offset=0")
    second = summary_fixture_api("/api/summaries/library?limit=1&offset=1")
    assert first["items"][0]["key"] == PERIOD_KEY and first["has_more"]
    assert second["items"][0]["key"] == TRIP_KEY and not second["has_more"]
    saved = summary_fixture_api("/api/summaries/saved/"+TRIP_KEY)
    assert saved["freshness"] == "not_checked"
    assert saved["result"]["kind"] == "trip_summary"
    assert saved["result"]["cache"]["status"] == "not_checked"
    assert saved["result"]["cache"]["current_watermark"] is None


def test_trip_fixture_keeps_anchor_and_temporal_evidence_separate():
    result = trip_summary_result()
    context = result["trip_context"]
    assert set(context["anchor_event_ids"]).isdisjoint(context["coincident_event_ids"])
    assert set(result["citations"]) == set(context["anchor_event_ids"]+context["coincident_event_ids"])
    assert context["route_known"] is False and context["mode_known"] is False
    assert result["cache"]["status"] == "fresh"
    assert result["selection"]["start"] == "2024-04-04" and result["selection"]["end_exclusive"] == "2024-04-06"
    assert result["evidence_ids"] == result["citations"] and TRIP_ID not in result["evidence_ids"]
    scoped = trip_summary_result(source_ids=["qa-one"])
    assert scoped["trip_context"]["coincident_event_ids"] == ["qa-pulse"]
    for event_id in result["citations"]:
        assert summary_fixture_api("/api/events/"+event_id)["provenance"]


def test_trip_cache_miss_does_not_generate_a_job():
    missing = summary_fixture_api("/api/summaries/trip?trip_id="+TRIP_ID+"&focus=Dates")
    assert missing["status"] == "missing" and missing["result"] is None
    assert summary_fixture_api("/api/jobs")["items"] == []
    journey = summary_fixture_api("/api/journeys/"+TRIP_ID)
    assert journey["journey"]["id"] == TRIP_ID and len(journey["items"]) == 1


def test_summary_generation_acceptance_rejects_live_api_mode(monkeypatch):
    import sys
    import pytest
    from check_dashboard import main
    monkeypatch.setattr(sys, "argv", ["check_dashboard.py", "--summary-workflows", "--api-url", "http://127.0.0.1:9"])
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 2
