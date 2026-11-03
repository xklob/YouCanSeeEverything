import json

import pytest

from biography import agents, journeys, synthesis, trip_synthesis
from biography.db import connect
from biography.locations import reconcile_locations
from test_retrieval import add, catalog
from test_synthesis import fake_model, state


@pytest.fixture
def trip(catalog):
    add(catalog, "stop-a", when="2024-05-01T00:00:00Z", title="Recorded park", category="place_visit")
    add(catalog, "stop-b", when="2024-05-01T05:00:00Z", title="Recorded coast", category="place_visit")
    with connect(catalog) as conn:
        for identity, end, longitude in (("stop-a", "2024-05-01T01:00:00Z", 0), ("stop-b", "2024-05-01T06:00:00Z", 1)):
            conn.execute("UPDATE events SET end_at=?,metadata=? WHERE id=?", (end, json.dumps({"location": {"latitude": 0, "longitude": longitude}}), identity))
    reconcile_locations(catalog, event_ids=["stop-a", "stop-b"])
    add(catalog, "coincident", source="reddit", when="2024-05-01T12:00:00Z", title="An unrelated discussion", body="A conversation recorded on the same day.")
    add(catalog, "outside", source="reddit", when="2024-05-02T12:00:00Z", title="Outside window")
    return journeys.list_journeys(catalog)["items"][0]["id"]


def test_trip_uses_authoritative_window_and_separates_context_from_anchors(catalog, state, trip, fake_model):
    result = trip_synthesis.summarize_trip(trip, state, catalog, source_ids=["reddit"])
    assert result["kind"] == "trip_summary" and result["period"] is None
    context = result["trip_context"]
    assert context["window_start"] == context["window_end"] == "2024-05-01"
    assert context["departure_at"] == "2024-05-01T01:00:00Z"
    assert context["anchor_event_ids"] == ["stop-a", "stop-b"]
    assert context["coincident_event_ids"] == ["coincident"]
    assert result["selection"]["source_ids"] == ["reddit"]
    assert set(result["evidence_ids"]) == {"stop-a", "stop-b", "coincident"}
    assert result["coverage"]["total_events_in_scope"] == 3
    assert result["cache"]["generated_at"]
    assert not context["route_known"] and not context["mode_known"]
    assert "not confirmed presence" in fake_model[0]["prompt"]
    assert "dates mentioned retrospectively" in fake_model[0]["prompt"]
    assert result["cache"]["status"] == "fresh"
    assert {item["relation"] for item in fake_model[0]["evidence"]} == {"recorded_trip_stop", "coincident_record"}
    citation_schema = fake_model[0]["schema"]["properties"]["sections"]["items"]["properties"]["sentences"]["items"]["properties"]["citations"]
    assert set(citation_schema["items"]["enum"]) == set(result["evidence_ids"])
    assert citation_schema["minItems"] == 1


def test_trip_saved_browse_never_resolves_journey_or_calls_model(catalog, state, trip, fake_model, monkeypatch):
    result = trip_synthesis.summarize_trip(trip, state, catalog)
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved browsing must not read the catalog")
    monkeypatch.setattr(journeys, "journey_detail", forbidden)
    monkeypatch.setattr(agents, "_run", forbidden)
    monkeypatch.setattr(trip_synthesis.retrieval, "_connection", forbidden)
    status = trip_synthesis.trip_summary_status(trip, state, catalog)
    assert status["status"] == "saved" and status["result"]["cache"]["status"] == "not_checked"
    assert status["result"]["citations"] == result["citations"]
    assert synthesis.list_saved(state)["items"][0]["trip_id"] == trip


def test_missing_journey_and_missing_anchor_evidence_do_not_call_model(catalog, state, trip, fake_model):
    with pytest.raises(ValueError, match="no longer exists"):
        trip_synthesis.summarize_trip("0" * 64, state, catalog)
    with connect(catalog) as conn:
        conn.execute("DELETE FROM evidence WHERE event_id IN ('stop-a','stop-b')")
    with pytest.raises(ValueError, match="no retained"):
        trip_synthesis.summarize_trip(trip, state, catalog)
    assert not fake_model


def test_trip_fresh_cache_survives_unrelated_import_without_another_model(catalog, state, trip, fake_model):
    first = trip_synthesis.summarize_trip(trip, state, catalog, source_ids=["reddit"])
    add(catalog, "later-health", when="2024-06-01", category="sensor")
    second = trip_synthesis.summarize_trip(trip, state, catalog, source_ids=["reddit"])
    assert second["answer"] == first["answer"]
    assert second["cache"]["status"] == "fresh" and len(fake_model) == 1


def test_trip_model_cannot_cite_the_synthetic_journey_id(catalog, state, trip, fake_model, monkeypatch):
    artifact = state / "invalid-trip-job"
    artifact.mkdir(parents=True)
    monkeypatch.setattr(agents, "_run", lambda *a, **k: ({"insufficient_evidence": False, "sections": [{"heading": "Recorded stops", "sentences": [{"text": "A claimed visit.", "citations": [trip]}]}]}, artifact))
    with pytest.raises(agents.AgentError, match="supplied evidence"):
        trip_synthesis.summarize_trip(trip, state, catalog)
    assert trip_synthesis.trip_summary_status(trip, state)["status"] == "missing"


def test_invalid_trip_id_is_rejected_without_catalog_access(state, monkeypatch):
    for identity in ("../private", "not-a-journey", "A" * 64):
        with pytest.raises(ValueError):
            trip_synthesis.trip_summary_status(identity, state)


def test_anchor_sampling_preserves_bounds_and_later_stops(monkeypatch):
    stops = [{"event_id": f"stop-{index}"} for index in range(30)]
    detail = {"journey": {"source_id": "timeline", "source": "Timeline", "departure_at": "2024-05-01T00:00:00Z", "arrival_at": "2024-05-02T00:00:00Z", "first_stop": stops[0], "last_stop": stops[-1]}, "items": [{"from": stops[index], "to": stops[index + 1]} for index in range(29)], "total": 29, "complete": True, "coverage_complete": True}
    monkeypatch.setattr(journeys, "journey_detail", lambda *a, **k: detail)
    context, ids = trip_synthesis._journey("a" * 64, None)
    assert len(ids) == trip_synthesis.MAX_ANCHORS
    assert {"stop-0", "stop-29"}.issubset(ids)
    assert any(f"stop-{index}" in ids for index in range(16, 29))
    assert not context["anchors_complete"]
