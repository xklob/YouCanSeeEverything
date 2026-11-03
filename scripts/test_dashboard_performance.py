"""Pending statistics remain unknown while bounded rows stay available."""
from check_dashboard_performance import performance_fixture_api


def test_pending_count_fixture_pages_without_an_exact_total():
    first = performance_fixture_api("/api/events?limit=40&offset=0")
    second = performance_fixture_api("/api/events?limit=40&offset=40")
    assert first["total"] is None and second["total"] is None
    assert first["has_more"] is True and second["has_more"] is False
    assert len(first["items"]) == 40 and len(second["items"]) == 2
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    assert all(r["id"].startswith("qa-") for r in first["items"] + second["items"])


def test_pending_statistics_keep_real_type_names_and_no_invented_zero():
    overview = performance_fixture_api("/api/overview")
    sources = performance_fixture_api("/api/sources")["items"]
    assert overview["total_events"] is None and overview["undated_count"] is None
    assert overview["stats"]["status"] == "pending"
    assert all(row["count"] is None and row["name"] for row in overview["categories"])
    assert all(row["event_count"] is None and row["first_event"] is None for row in sources)
    assert performance_fixture_api("/api/journeys")["items"] == []
