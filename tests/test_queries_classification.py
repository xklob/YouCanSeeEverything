import json

from biography import classification, queries
from biography.db import connect, init_db


def catalog(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    init_db(path)
    with connect(path) as conn:
        for identity, name in (("unknown-source", "Unknown"), ("health-source", "Google")):
            conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)", (identity, name, "now", "now"))
        conn.execute("INSERT INTO objects(sha256,bytes,path,verified_at) VALUES(?,?,?,?)", ("a" * 64, 1, str(tmp_path / "object-a"), "now"))
        conn.execute("INSERT INTO objects(sha256,bytes,path,verified_at) VALUES(?,?,?,?)", ("b" * 64, 1, str(tmp_path / "object-b"), "now"))
        conn.execute("INSERT INTO files(id,source_id,original_path,object_hash,bytes,parser,created_at) VALUES(?,?,?,?,?,?,?)", ("chat-file", "unknown-source", "Export/conversations-000.json", "a" * 64, 1, "chatgpt.messages", "now"))
        conn.execute("INSERT INTO files(id,source_id,original_path,object_hash,bytes,parser,created_at) VALUES(?,?,?,?,?,?,?)", ("health-file", "health-source", "Takeout/Google Health & Fit/Heart Rate/samples.csv", "b" * 64, 1, "csv.records", "now"))
    return path


def insert(path, identity, source, category, metadata, file_id, parser, *, when="2024-06-01T12:00:00Z"):
    with connect(path) as conn:
        conn.execute("INSERT INTO events(id,source_id,title,body,category,occurred_at,time_precision,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (identity, source, "Original title", "Original source text", category, when, "second", json.dumps(metadata), "now"))
        conn.execute("INSERT INTO evidence(event_id,file_id,locator,parser) VALUES(?,?,?,?)", (identity, file_id, "record:" + identity, parser))


def test_chatgpt_display_label_does_not_rewrite_source_provenance_or_filters(tmp_path):
    path = catalog(tmp_path)
    insert(path, "chat", "unknown-source", "message", {}, "chat-file", "chatgpt.messages")
    listed = queries.list_events(path, source="unknown-source", category="message")
    item = listed["items"][0]
    assert item["source"] == "Unknown" and item["source_id"] == "unknown-source"
    assert item["source_label"] == "ChatGPT"
    assert item["category"] == item["display_category"] == "message"
    assert item["classification"]["display_class"] == "conversation"
    assert queries.list_events(path, source="ChatGPT")["total"] == 0
    detail = queries.event_detail("chat", path)
    assert detail["source_label"] == "ChatGPT" and detail["source"] == "Unknown"
    assert detail["provenance"][0]["path"] == "Export/conversations-000.json"
    assert detail["body"] == "Original source text"
    with connect(path) as conn:
        row = conn.execute("SELECT category,body FROM events WHERE id='chat'").fetchone()
        assert dict(row) == {"category": "message", "body": "Original source text"}


def test_health_presentation_adds_display_category_without_changing_raw_category(tmp_path):
    path = catalog(tmp_path)
    metadata = {"timestamp": "2024-06-01T12:00:00Z", "beats per minute": "72", "data source": "sensor-stream-a"}
    insert(path, "sample", "health-source", "record", metadata, "health-file", "csv.records")
    item = queries.list_events(path, category="record")["items"][0]
    assert item["category"] == "record"
    assert item["display_category"] == "health_sample"
    assert item["source_label"] == "Google Health & Fit"
    assert item["is_raw_sample"] is True
    assert item["metric"]["unit"] == "bpm" and item["metric"]["value"] == 72
    assert item["metadata"] == metadata
    assert queries.list_events(path, category="health_sample")["total"] == 0
    assert queries.event_detail("sample", path)["display_category"] == "health_sample"


def test_only_bounded_page_rows_are_classified(tmp_path, monkeypatch):
    path = catalog(tmp_path)
    for index in range(12):
        insert(path, f"chat-{index:02}", "unknown-source", "message", {}, "chat-file", "chatgpt.messages")
    original = classification.classify_event
    seen = []
    def wrapped(event, provenance=None):
        seen.append(event["id"])
        return original(event, provenance)
    monkeypatch.setattr(classification, "classify_event", wrapped)
    page = queries.list_events(path, limit=2, offset=3)
    assert page["total"] == 12
    assert seen == [item["id"] for item in page["items"]]
    assert len(seen) == 2
    seen.clear()
    assert queries.event_detail("missing", path) is None
    assert seen == []
