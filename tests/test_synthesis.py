import json
import os
from pathlib import Path

import pytest

from biography import agents, synthesis
from biography.db import connect
from test_retrieval import add, catalog


@pytest.fixture
def state(tmp_path):
    return tmp_path / "private/.local/summaries"


@pytest.fixture
def fake_model(monkeypatch, state):
    calls = []
    def run(prompt, schema, work_dir, **kwargs):
        evidence = json.loads(prompt.split("UNTRUSTED SOURCE EVIDENCE JSON:\n", 1)[1])
        calls.append({"evidence": evidence, "kwargs": kwargs, "prompt": prompt, "schema": schema})
        artifact = state / "fake-model-job"
        artifact.mkdir(parents=True, exist_ok=True)
        return {"insufficient_evidence": False, "sections": [{"heading": "Life and activities", "sentences": [{"text": "The records describe a park visit with friends.", "citations": [evidence[0]["id"]]}]}]}, artifact
    monkeypatch.setattr(agents, "_run", run)
    return calls


def test_month_summary_has_sentence_citations_and_plain_prose(catalog, state, fake_model):
    add(catalog, "ev-park")
    result = synthesis.summarize("2024-05", state, catalog)
    assert result["kind"] == "period_summary"
    assert result["citations"] == ["ev-park"]
    assert result["sections"][0]["sentences"][0]["citations"] == ["ev-park"]
    assert "[" not in result["answer"] and "ev-park" not in result["answer"]
    assert result["cache"]["status"] == "fresh"
    assert result["coverage"]["selected_events"] == 1
    assert any("not a complete biography" in value for value in result["limitations"])
    assert fake_model[0]["kwargs"]["model"] == "gpt-6.1-sol"
    citations = fake_model[0]["schema"]["properties"]["sections"]["items"]["properties"]["sentences"]["items"]["properties"]["citations"]
    assert citations["items"]["enum"] == ["ev-park"]
    assert citations["minItems"] == 1


def test_cache_status_and_fresh_hits_never_invoke_model(catalog, state, fake_model):
    add(catalog, "ev-park")
    assert synthesis.summary_status("2024-05", state, catalog)["status"] == "missing"
    assert not fake_model
    first = synthesis.summarize("2024-05", state, catalog)
    second = synthesis.summarize("2024-05", state, catalog)
    assert len(fake_model) == 1
    assert second["answer"] == first["answer"]
    status = synthesis.summary_status("2024-05", state, catalog)
    assert status["status"] == "fresh"
    assert status["fixed_watermark"] == status["current_watermark"]
    assert len(fake_model) == 1


def test_new_import_marks_cache_stale_and_explicit_regenerate_updates_it(catalog, state, fake_model):
    add(catalog, "ev-park")
    first = synthesis.summarize("2024-05", state, catalog)
    add(catalog, "ev-new", when="2024-05-15", body="A second documented observation.")
    status = synthesis.summary_status("2024-05", state, catalog)
    assert status["status"] == "stale"
    assert status["result"]["answer"] == first["answer"]
    assert status["current_coverage"]["total_events_in_scope"] == 2
    assert status["fixed_watermark"] != status["current_watermark"]
    assert len(fake_model) == 1
    regenerated = synthesis.summarize("2024-05", state, catalog)
    assert regenerated["cache"]["status"] == "fresh"
    assert len(fake_model) == 2


def test_selected_record_in_place_edit_also_invalidates_cache(catalog, state, fake_model):
    add(catalog, "ev-park")
    synthesis.summarize("2024-05", state, catalog)
    with connect(catalog) as conn:
        conn.execute("UPDATE events SET body='A changed source observation.' WHERE id='ev-park'")
    status = synthesis.summary_status("2024-05", state, catalog)
    assert status["status"] == "stale"
    assert status["fixed_watermark"] != status["current_watermark"]


def test_period_source_focus_and_model_have_separate_cache_keys(catalog, state, fake_model):
    add(catalog, "ev-park")
    synthesis.summarize("2024-05", state, catalog)
    for kwargs in ({"source_ids": ["journal"]}, {"focus": "People"}, {"model": "gpt-6-astra"}):
        assert synthesis.summary_status("2024-05", state, catalog, **kwargs)["status"] == "missing"
    assert synthesis.summary_status("2024-05-01", state, catalog)["status"] == "missing"
    assert len(fake_model) == 1


def test_equivalent_source_selection_order_reuses_fresh_cache(catalog, state, fake_model):
    add(catalog, "ev-park")
    add(catalog, "ev-reddit", source="reddit")
    synthesis.summarize("2024-05", state, catalog, source_ids=["journal", "reddit"])
    status = synthesis.summary_status("2024-05", state, catalog, source_ids=["reddit", "journal"])
    assert status["status"] == "fresh"
    assert len(fake_model) == 1


def test_empty_period_has_honest_local_answer_and_uses_no_account_usage(catalog, state, fake_model):
    result = synthesis.summarize("2024-05", state, catalog)
    assert result["insufficient_evidence"]
    assert result["citations"] == []
    assert not result["inference_performed"]
    assert "No dated evidence" in result["answer"]
    assert not fake_model
    assert synthesis.summary_status("2024-05", state, catalog)["status"] == "fresh"


@pytest.mark.parametrize("sentences", [
    [{"text": "An unsupported claim.", "citations": ["invented-event"]}],
    [{"text": "An unsupported claim.", "citations": []}],
    [{"text": "The record says this [ev-park].", "citations": ["ev-park"]}],
])
def test_uncited_fabricated_ids_and_raw_brackets_reject_summary(catalog, state, fake_model, monkeypatch, sentences):
    add(catalog, "ev-park")
    artifact = state / "invalid-job"
    artifact.mkdir(parents=True)
    monkeypatch.setattr(agents, "_run", lambda *a, **k: ({"insufficient_evidence": False, "sections": [{"heading": "Life and activities", "sentences": sentences}]}, artifact))
    with pytest.raises(agents.AgentError):
        synthesis.summarize("2024-05", state, catalog)
    assert synthesis.summary_status("2024-05", state, catalog)["status"] == "missing"
    assert json.loads((artifact / "status.json").read_text())["status"] == "failed"


def test_import_during_model_job_preserves_fixed_watermark_and_returns_stale(catalog, state, fake_model, monkeypatch):
    add(catalog, "ev-park")
    original = agents._run
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        add(catalog, "ev-imported-during-model", body="A later source import.")
        return value
    monkeypatch.setattr(agents, "_run", changed)
    result = synthesis.summarize("2024-05", state, catalog)
    assert result["cache"]["status"] == "stale"
    assert result["coverage"]["total_events_in_scope"] == 1
    assert synthesis.summary_status("2024-05", state, catalog)["current_coverage"]["total_events_in_scope"] == 2


def test_source_named_in_focus_does_not_make_fresh_summary_immediately_stale(catalog, state, fake_model):
    add(catalog, "ev-reddit", source="reddit")
    add(catalog, "ev-journal")
    result = synthesis.summarize("2024-05", state, catalog, focus="Posts from Reddit")
    assert result["selection"]["source_ids"] == ["reddit"]
    assert result["cache"]["status"] == "fresh"
    assert synthesis.summary_status("2024-05", state, catalog, focus="Posts from Reddit")["status"] == "fresh"


def test_summary_cannot_write_artifacts_outside_private_state(catalog, tmp_path, fake_model):
    add(catalog, "ev-park")
    with pytest.raises(ValueError, match="private"):
        synthesis.summarize("2024-05", tmp_path / "public-artifacts", catalog)
    with pytest.raises(ValueError, match="month"):
        synthesis.summarize("2024", tmp_path / ".local", catalog)


def test_unrelated_imports_and_raw_samples_do_not_invalidate_narrative(catalog, state, fake_model):
    add(catalog, "ev-park")
    synthesis.summarize("2024-05", state, catalog)
    add(catalog, "ev-next-month", when="2024-06-01")
    add(catalog, "ev-raw", category="sensor", body="Heart rate measurement")
    with connect(catalog) as conn:
        conn.execute("UPDATE sources SET updated_at='later',status='importing' WHERE id='journal'")
    assert synthesis.summary_status("2024-05", state, catalog)["status"] == "fresh"
    assert len(fake_model) == 1


def test_changed_or_deleted_unselected_scope_record_marks_stale(catalog, state, fake_model):
    for index in range(55):
        add(catalog, f"ev-{index}", when=f"2024-05-{index % 28 + 1:02d}", body=f"Park observation {index}")
    result = synthesis.summarize("2024-05", state, catalog)
    identity = next(f"ev-{index}" for index in range(55) if f"ev-{index}" not in result["evidence_ids"])
    with connect(catalog) as conn:
        conn.execute("UPDATE events SET body='A different observed activity' WHERE id=?", (identity,))
    assert synthesis.summary_status("2024-05", state, catalog)["status"] == "stale"
    synthesis.summarize("2024-05", state, catalog)
    with connect(catalog) as conn:
        conn.execute("DELETE FROM evidence WHERE event_id=?", (identity,))
        conn.execute("DELETE FROM events WHERE id=?", (identity,))
    assert synthesis.summary_status("2024-05", state, catalog)["status"] == "stale"


def test_incomplete_freshness_check_is_unknown_and_preserves_saved_result(catalog, state, fake_model, monkeypatch):
    add(catalog, "ev-park")
    result = synthesis.summarize("2024-05", state, catalog)
    monkeypatch.setattr(synthesis.summary_freshness, "MAX_ROWS", 1)
    add(catalog, "ev-raw", category="sensor")
    status = synthesis.summary_status("2024-05", state, catalog)
    assert status["status"] == "unknown"
    assert status["result"]["answer"] == result["answer"]
    assert len(fake_model) == 1


def test_saved_library_and_detail_have_no_catalog_or_model_access(catalog, state, fake_model, monkeypatch):
    add(catalog, "ev-park")
    result = synthesis.summarize("2024-05", state, catalog)
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved browsing must use cache files only")
    monkeypatch.setattr(synthesis.retrieval, "_connection", forbidden)
    monkeypatch.setattr(agents, "_run", forbidden)
    listing = synthesis.list_saved(state, limit=1)
    assert len(listing["items"]) == 1 and not listing["has_more"]
    assert listing["items"][0]["freshness"] == "not_checked"
    opened = synthesis.saved_summary(result["cache"]["key"], state)
    assert opened["status"] == "saved" and opened["result"]["cache"]["status"] == "not_checked"
    assert opened["result"]["answer"] == result["answer"]


def test_legacy_files_are_discovered_and_unsafe_files_ignored(catalog, state, fake_model, tmp_path):
    add(catalog, "ev-park")
    result = synthesis.summarize("2024-05", state, catalog)
    (state / "library.sqlite3").unlink()
    (state / ("a" * 64 + ".json")).write_text("{broken")
    (state / ("b" * 64 + ".json")).symlink_to(state / (result["cache"]["key"] + ".json"))
    os.mkfifo(state / ("c" * 64 + ".json"))
    listing = synthesis.list_saved(state)
    assert [entry["key"] for entry in listing["items"]] == [result["cache"]["key"]]
    assert synthesis.saved_summary("b" * 64, state) is None
    assert synthesis.saved_summary("c" * 64, state) is None
    with pytest.raises(ValueError):
        synthesis.saved_summary("../outside", state)


def test_legacy_discovery_is_bounded_and_resumes_without_catalog_access(catalog, state, fake_model, monkeypatch):
    add(catalog, "ev-park")
    synthesis.summarize("2024-05", state, catalog)
    synthesis.summarize("2024-05-01", state, catalog)
    (state / "library.sqlite3").unlink()
    monkeypatch.setattr(synthesis.summary_library, "MAX_IMPORT_FILES", 1)
    first = synthesis.list_saved(state)
    assert first["discovery_limited"] and len(first["items"]) == 1
    second = synthesis.list_saved(state)
    assert len(second["items"]) == 2 and not second["discovery_limited"]


def test_schema_bound_hash_still_rejects_a_mistyped_id_in_post_validation(catalog, state, fake_model, monkeypatch):
    identity = "a" * 64
    add(catalog, identity)
    artifact = state / "mistyped-id"
    artifact.mkdir(parents=True)
    observed = {}
    def wrong(prompt, schema, *args, **kwargs):
        observed["schema"] = schema
        return {"insufficient_evidence": False, "sections": [{"heading": "Life and activities", "sentences": [{"text": "A park visit is recorded.", "citations": [identity + "e"]}]}]}, artifact
    monkeypatch.setattr(agents, "_run", wrong)
    with pytest.raises(agents.AgentError) as error:
        synthesis.summarize("2024-05", state, catalog)
    assert error.value.code == "invalid_citation"
    items = observed["schema"]["properties"]["sections"]["items"]["properties"]["sentences"]["items"]["properties"]["citations"]["items"]
    assert items["enum"] == [identity]
    assert synthesis.list_saved(state)["items"] == []
