import json

import pytest
from fastapi.testclient import TestClient

from biography import agents, api, db, retrieval, synthesis
from biography.config import Settings
from test_retrieval import add, catalog


class DeferredExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))

    def run(self):
        function, args = self.calls.pop(0)
        function(*args)


@pytest.fixture
def summary_client(catalog, tmp_path, monkeypatch):
    local = Settings(state_dir=tmp_path / ".local", data_dir=catalog.parent)
    executor = DeferredExecutor()
    monkeypatch.setattr(api, "settings", local)
    monkeypatch.setattr(db, "settings", local)
    monkeypatch.setattr(api, "EXECUTOR", executor)
    with TestClient(api.app, base_url="http://127.0.0.1:8000") as client:
        client.headers["x-biography-token"] = client.get("/api/session").json()["token"]
        yield client, executor, local


def test_summary_get_is_read_only_and_never_invokes_model(summary_client, monkeypatch):
    client, executor, local = summary_client
    def no_inference(*args, **kwargs):
        raise AssertionError("Cache GET must never invoke a model")
    monkeypatch.setattr(agents, "_run", no_inference)
    response = client.get("/api/summaries", params={"period": "2024-05", "source": "journal"})
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "missing" and value["result"] is None
    assert value["current_coverage"]["sources"][0]["source_id"] == "journal"
    assert executor.calls == []
    with db.connect(local.database) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("payload", [
    {"mode": "summarize", "period": "2024-99"},
    {"mode": "summarize"},
    {"mode": "summarize", "period": "2024"},
    {"mode": "summarize", "period": "2024-05", "model": "some-billed-api-model"},
    {"mode": "summarize", "period": "2024-05", "source_ids": [str(i) for i in range(33)]},
])
def test_invalid_summary_selection_never_queues_inference(summary_client, payload):
    client, executor, _ = summary_client
    assert client.post("/api/assistant", json=payload).status_code == 400
    assert not executor.calls


def test_summary_job_forwards_exact_period_sources_and_explicit_model(summary_client, monkeypatch):
    client, executor, local = summary_client
    observed = {}
    def summarized(period, work_dir, path, **kwargs):
        observed.update(period=period, work_dir=work_dir, path=path, **kwargs)
        return {"kind": "period_summary", "period": period, "answer": "A concise cited summary.", "sections": [], "citations": [], "cache": {"status": "fresh"}}
    monkeypatch.setattr(synthesis, "summarize", summarized)
    queued = client.post("/api/assistant", json={"mode": "summarize", "period": "2024-05", "source_ids": ["journal"], "prompt": "Focus on people", "model": "gpt-6-astra"})
    assert queued.status_code == 202
    identity = queued.json()["id"]
    assert client.get("/api/jobs/" + identity).json()["status"] == "queued"
    assert observed == {}
    executor.run()
    completed = client.get("/api/jobs/" + identity).json()
    assert completed["status"] == "completed"
    assert completed["result"]["kind"] == "period_summary"
    assert not completed["can_undo"]
    assert observed == {"period": "2024-05", "work_dir": local.summaries_dir, "path": local.database, "source_ids": ["journal"], "focus": "Focus on people", "model": "gpt-6-astra"}


def test_summary_worker_failure_is_persisted_without_applying_any_change(summary_client, monkeypatch):
    client, executor, _ = summary_client
    def unavailable(*args, **kwargs):
        raise agents.AgentError("The saved account reached its Codex usage limit.", "cli_failed")
    monkeypatch.setattr(synthesis, "summarize", unavailable)
    identity = client.post("/api/assistant", json={"mode": "summarize", "period": "2024-05"}).json()["id"]
    executor.run()
    failed = client.get("/api/jobs/" + identity).json()
    assert failed["status"] == "failed"
    assert "usage limit" in failed["error"]
    assert not failed["can_undo"]
    assert failed["result"] is None


def test_ask_worker_forwards_bounded_retrieval_context(summary_client, monkeypatch):
    client, executor, local = summary_client
    selected = {"evidence": [{"id": "ev-fixture", "body": "Actual source excerpt"}], "selection": {"period": "2024-05"}, "coverage": {"selected_events": 1, "retrieval_limited": True}, "watermark": {"token": "fixed"}, "policy_version": 1}
    observed = {}
    def retrieve(prompt, path, **kwargs):
        observed["retrieval"] = (prompt, path, kwargs)
        return selected
    def ask(prompt, evidence, work_dir, **kwargs):
        observed["ask"] = (prompt, evidence, work_dir, kwargs)
        return {"answer": "A supported answer.", "citations": ["ev-fixture"]}
    monkeypatch.setattr(retrieval, "retrieve", retrieve)
    monkeypatch.setattr(agents, "ask", ask)
    identity = client.post("/api/assistant", json={"mode": "ask", "prompt": "What happened?", "period": "2024-05", "source_ids": ["journal"]}).json()["id"]
    executor.run()
    result = client.get("/api/jobs/" + identity).json()["result"]
    assert result["coverage"]["selected_events"] == 1
    assert observed["retrieval"] == ("What happened?", local.database, {"period": "2024-05", "source_ids": ["journal"]})
    assert observed["ask"][1] == selected["evidence"]
    assert observed["ask"][3]["retrieval_context"]["coverage"]["retrieval_limited"]


def test_cached_summary_endpoint_reports_staleness_without_new_inference(summary_client, catalog, monkeypatch):
    client, _, local = summary_client
    add(catalog, "ev-park")
    calls = []
    def run(prompt, schema, work_dir, **kwargs):
        evidence = json.loads(prompt.split("UNTRUSTED SOURCE EVIDENCE JSON:\n", 1)[1])
        calls.append(evidence)
        work_dir.mkdir(parents=True, exist_ok=True)
        return {"insufficient_evidence": False, "sections": [{"heading": "Life and activities", "sentences": [{"text": "The records describe a park visit.", "citations": [evidence[0]["id"]]}]}]}, work_dir
    monkeypatch.setattr(agents, "_run", run)
    synthesis.summarize("2024-05", local.summaries_dir, catalog, source_ids=["journal"])
    assert client.get("/api/summaries", params={"period": "2024-05", "source": "journal"}).json()["status"] == "fresh"
    add(catalog, "ev-later", when="2024-05-20", body="A newly imported source observation.")
    response = client.get("/api/summaries", params={"period": "2024-05", "source": "journal"}).json()
    assert response["status"] == "stale"
    assert response["result"]["citations"] == ["ev-park"]
    assert response["current_coverage"]["total_events_in_scope"] == 2
    assert len(calls) == 1
