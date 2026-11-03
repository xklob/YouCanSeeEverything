import json
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
        calls.append({"evidence": evidence, "kwargs": kwargs, "prompt": prompt})
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
