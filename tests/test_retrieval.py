import hashlib
import json
from pathlib import Path
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from biography import retrieval
from biography.db import connect, init_db


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    init_db(path)
    with connect(path) as conn:
        for sid, name in (("journal", "Journal"), ("reddit", "Reddit"), ("music", "Spotify")):
            conn.execute("INSERT INTO sources(id,name,status,created_at,updated_at) VALUES(?,?,?,?,?)", (sid, name, "ready", "2024-01-01", "2024-01-01"))
            digest = hashlib.sha256(sid.encode()).hexdigest()
            conn.execute("INSERT INTO objects VALUES(?,?,?,?)", (digest, 1, str(tmp_path / sid), "2024-01-01"))
            conn.execute("INSERT INTO files(id,source_id,original_path,object_hash,bytes,parse_status,created_at) VALUES(?,?,?,?,?,?,?)", ("file-" + sid, sid, sid + ".json", digest, 1, "parsed", "2024-01-01"))
    return path


def add(catalog, identity, *, source="journal", when="2024-05-01", title="Park visit", body="I visited the park and met friends.", category="note", metadata=None, provenance=True):
    with connect(catalog) as conn:
        conn.execute("INSERT INTO events(id,source_id,title,body,occurred_at,category,time_precision,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (identity, source, title, body, when, category, "day" if when and len(when) == 10 else "second", json.dumps(metadata or {}), "2024-01-01"))
        if provenance:
            conn.execute("INSERT INTO evidence VALUES(?,?,?,?)", (identity, "file-" + source, "$[0]", "fixture"))


@pytest.mark.parametrize("prompt,period,first,last", [
    ("What happened in May 2024?", "2024-05", "2024-05-01", "2024-06-01"),
    ("What happened on May 5, 2024?", "2024-05-05", "2024-05-05", "2024-05-06"),
    ("What happened on 2024-02-29?", "2024-02-29", "2024-02-29", "2024-03-01"),
    ("Describe 2016", "2016", "2016-01-01", "2017-01-01"),
])
def test_reliable_written_periods(prompt, period, first, last):
    chosen = retrieval.selection(prompt)
    assert (chosen["period"], chosen["start"], chosen["end_exclusive"]) == (period, first, last)


def test_explicit_selection_wins_and_ambiguous_dates_do_not_silently_filter():
    assert retrieval.selection("What happened in 2020?", period="2024-05")["start"] == "2024-05-01"
    for text in ("Compare 2023-01-01 with 2024-01-01", "Compare May 5, 2024 and June 2024", "Compare my work in 2020 with 2024", "What happened last month?"):
        chosen = retrieval.selection(text)
        assert chosen["start"] is None
        assert chosen["warnings"]
    chosen = retrieval.selection("What happened from 2024-05-01 through 2024-05-03?")
    assert chosen["start"] == "2024-05-01" and chosen["end_exclusive"] == "2024-05-04"
    with pytest.raises(ValueError):
        retrieval.selection(period="2024-13")


def test_period_and_source_filters_are_strict_and_undated_remains_unknown(catalog):
    add(catalog, "ev-may", when="2024-05-01")
    add(catalog, "ev-june", when="2024-06-01")
    add(catalog, "ev-other", source="reddit", when="2024-05-01")
    add(catalog, "ev-undated", when=None)
    result = retrieval.retrieve("", catalog, period="2024-05", source_ids=["journal"])
    assert [event["id"] for event in result["evidence"]] == ["ev-may"]
    assert result["coverage"]["total_events_in_scope"] == 1
    assert result["coverage"]["undated_events_excluded"] == 1
    assert result["coverage"]["sources"][0]["source_id"] == "journal"
    with pytest.raises(ValueError, match="not present"):
        retrieval.retrieve("", catalog, source_ids=["missing-source"])


def test_empty_date_scope_does_not_search_global_fts(catalog, monkeypatch):
    add(catalog, "ev-recent", when="2024-05-01", body="A trip to New York.")
    queries = []
    original = retrieval._connection

    @contextmanager
    def traced(path=None):
        with original(path) as conn:
            conn.set_trace_callback(queries.append)
            yield conn

    monkeypatch.setattr(retrieval, "_connection", traced)
    result = retrieval.retrieve("New York on 1900-01-01", catalog)
    assert result["selection"]["period"] == "1900-01-01"
    assert result["coverage"]["total_events_in_scope"] == 0
    assert result["evidence"] == []
    assert not any("events_fts" in query for query in queries)


def test_scope_metadata_uses_covering_ranges_without_global_counts(catalog, monkeypatch):
    add(catalog, "ev-selected", when="2024-05-01")
    add(catalog, "ev-other", source="reddit", when="2010-01-01")
    queries, plans = [], []
    original = retrieval._connection

    @contextmanager
    def traced(path=None):
        with original(path) as conn:
            def trace(query):
                queries.append(query)
                if query.startswith("SELECT count(1) total_events"):
                    plans.extend(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query))
            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(retrieval, "_connection", traced)
    result = retrieval.catalog_state(catalog, period="2024-05", source_ids=["journal"])
    assert result["coverage"]["total_events_in_scope"] == 1
    assert result["watermark"]["catalog"]["events"] is None
    assert plans and all("COVERING INDEX events_source" in plan for plan in plans)
    assert not any(query.casefold().startswith("select count(*) from ") for query in queries)


def test_fts_is_driven_by_selected_rowids_instead_of_global_rank(catalog, monkeypatch):
    for index in range(60):
        add(catalog, f"ev-noise-{index}", source="reddit", when="2010-01-01", body="A park trip with friends.")
    add(catalog, "ev-relevant", when="2024-05-01", body="A park trip with friends.")
    plans = []
    original = retrieval._connection

    @contextmanager
    def traced(path=None):
        with original(path) as conn:
            def trace(query):
                if query.startswith("WITH scoped") and "events_fts MATCH" in query:
                    plans.extend(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query))
            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(retrieval, "_connection", traced)
    result = retrieval.retrieve("park trip in May 2024", catalog, source_ids=["journal"])
    assert [event["id"] for event in result["evidence"]] == ["ev-relevant"]
    access = [plan for plan in plans if "events_fts VIRTUAL TABLE" in plan]
    assert access and all("=" in plan and "M" in plan for plan in access)
    assert result["coverage"]["lexical_search_pool"]["records"] == 1


def test_large_scopes_bound_text_search_and_disclose_sampled_coverage(catalog, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_SEARCH_POOL", 16)
    for index in range(40):
        add(catalog, f"ev-scoped-{index}", when=f"2024-05-{index % 28 + 1:02d}", body=f"Park observation {index}.")
    result = retrieval.retrieve("park in May 2024", catalog, source_ids=["journal"])
    scope = result["coverage"]["lexical_search_pool"]
    assert scope["limited"] and 0 < scope["records"] <= 16
    assert len(result["evidence"]) <= 16
    assert any("only sampled records were text-searched" in note for note in result["coverage"]["limitations"])


def test_background_context_never_reorders_into_full_events_scan(catalog, monkeypatch):
    with connect(catalog) as conn:
        for index in range(300):
            identity = f"ev-context-{index}"
            conn.execute("INSERT INTO events(id,source_id,title,body,occurred_at,category,time_precision,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (identity, "journal", "Park visit", str(index), "2024-05-01", "note", "day", "{}", "2024-01-01"))
            conn.execute("INSERT INTO evidence VALUES(?,?,?,?)", (identity, "file-journal", "$[0]", "fixture"))
    plans = []
    original = retrieval._connection

    @contextmanager
    def traced(path=None):
        with original(path) as conn:
            def trace(query):
                if query.startswith("WITH scoped") and "ranked AS" in query:
                    plans.extend(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query))
            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(retrieval, "_connection", traced)
    result = retrieval.retrieve("", catalog, period="2024-05", source_ids=["journal"])
    assert result["evidence"]
    assert plans and not any(plan.startswith("SCAN e ") for plan in plans)


def test_named_source_constraint_in_question_is_not_an_all_source_search(catalog):
    add(catalog, "ev-journal")
    add(catalog, "ev-reddit", source="reddit")
    result = retrieval.retrieve("What did I post on Reddit in May 2024?", catalog)
    assert result["selection"]["source_ids"] == ["reddit"]
    assert {item["source_id"] for item in result["evidence"]} == {"reddit"}


def test_complete_source_mention_does_not_include_shorter_prefix_source(catalog):
    with connect(catalog) as conn:
        for sid, name in (("google", "Google"), ("timeline", "Google Maps Timeline")):
            conn.execute("INSERT INTO sources(id,name,status,created_at,updated_at) VALUES(?,?,?,?,?)", (sid, name, "ready", "2024-01-01", "2024-01-01"))
    result = retrieval.catalog_state(catalog, prompt="Show records from Google Maps Timeline in May 2024")
    assert result["selection"]["source_ids"] == ["timeline"]


def test_query_centered_excerpt_finds_fact_beyond_the_old_leading_excerpt(catalog):
    body = "introductory material " * 500 + "I relocated to the Alder apartment." + " more context" * 200
    add(catalog, "ev-long", title="Journal entry", body=body)
    result = retrieval.retrieve("Alder apartment relocation in May 2024", catalog)
    assert len(result["evidence"]) == 1
    evidence = result["evidence"][0]
    assert "Alder apartment" in evidence["body"]
    assert evidence["excerpt_strategy"] == "query_centered"
    assert evidence["body_truncated"]


def test_source_category_and_time_diversity_prevents_listening_firehose(catalog):
    for index in range(130):
        add(catalog, f"ev-music-{index}", source="music", when=f"2024-05-{index % 28 + 1:02d}T12:{index % 60:02d}:00Z", title="Music", body=f"Music listening session {index}", category="listening")
    add(catalog, "ev-park", body="I listened to music with friends in the park.")
    add(catalog, "ev-trip", source="reddit", when="2024-05-20", title="Trip", body="A trip to the coast included music.", category="post")
    result = retrieval.retrieve("music in May 2024", catalog)
    assert {"ev-park", "ev-trip"}.issubset({item["id"] for item in result["evidence"]})
    assert len(result["evidence"]) <= retrieval.MAX_EVIDENCE
    assert result["coverage"]["retrieval_limited"]
    assert len({item["source_id"] for item in result["evidence"]}) == 3


def test_broad_context_can_retrieve_historical_sources_instead_of_only_latest(catalog):
    add(catalog, "ev-old", when="2010-01-01", body="An early journal observation.")
    add(catalog, "ev-new", when="2024-05-01", body="A recent journal observation.")
    result = retrieval.retrieve("Tell me about my life", catalog)
    assert {item["id"] for item in result["evidence"]} == {"ev-old", "ev-new"}


def test_raw_sensor_candidates_omitted_and_unprovenanced_rows_not_cited(catalog, monkeypatch):
    monkeypatch.setitem(sys.modules, "biography.classification", SimpleNamespace(classify_event=lambda event, provenance=None: {"is_raw_sample": event["metadata"].get("raw_sensor", False)}))
    add(catalog, "ev-sensor", body="Raw measurement", category="record", metadata={"raw_sensor": True})
    add(catalog, "ev-ordinary", body="I went for a walk.")
    add(catalog, "ev-without-original", body="No original retained evidence", provenance=False)
    result = retrieval.retrieve("", catalog, period="2024-05")
    assert [item["id"] for item in result["evidence"]] == ["ev-ordinary"]
    assert result["coverage"]["raw_sample_candidates_omitted"] == 1
    assert any("shown separately" in note for note in result["coverage"]["limitations"])


def test_large_chat_metadata_preserves_author_role_without_sending_payload(catalog):
    add(catalog, "ev-assistant", category="message", metadata={"role": "assistant", "conversation_title": "Source discussion", "private_blob": "x" * 20_000})
    result = retrieval.retrieve("", catalog, period="2024-05")
    assert result["evidence"][0]["metadata"]["role"] == "assistant"
    assert "private_blob" not in result["evidence"][0]["metadata"]


def test_evidence_context_has_a_hard_character_budget(catalog):
    for index in range(80):
        add(catalog, f"ev-long-{index}", when=f"2024-05-{index % 28 + 1:02d}", body=(f"Distinct observation {index}. " + "detail " * 2_000))
    result = retrieval.retrieve("Distinct observation May 2024", catalog)
    assert sum(len(json.dumps(item, ensure_ascii=False)) for item in result["evidence"]) <= retrieval.MAX_CONTEXT_CHARS
    assert all(len(item["body"]) <= retrieval.MAX_EXCERPT_CHARS for item in result["evidence"])


def test_watermark_ignores_job_writes_and_changes_when_evidence_is_imported(catalog):
    add(catalog, "ev-original")
    initial = retrieval.catalog_state(catalog, period="2024-05")["watermark"]["token"]
    with connect(catalog) as conn:
        conn.execute("INSERT INTO jobs(id,kind,status,created_at,updated_at) VALUES('job','ask','completed','now','now')")
    assert retrieval.catalog_state(catalog, period="2024-05")["watermark"]["token"] == initial
    add(catalog, "ev-later")
    assert retrieval.catalog_state(catalog, period="2024-05")["watermark"]["token"] != initial


def test_fts_punctuation_cannot_turn_into_sql_or_unrestricted_fts_syntax(catalog):
    add(catalog, "ev-original")
    result = retrieval.retrieve('" OR * -park ; DROP TABLE events --', catalog, period="2024-05")
    assert result["coverage"]["total_events_in_scope"] == 1
    with connect(catalog) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
