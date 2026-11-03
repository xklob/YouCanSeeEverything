import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from biography import adaptive
from biography.agents import AgentError
from biography.parsers import ParseResult
from biography.parsers.common import event


@pytest.fixture
def state(tmp_path):
    return tmp_path / ".local/adaptive"


@pytest.fixture
def baseline():
    result = ParseResult(parser="original", status="needs_fallback", notes=["Original extraction note"])
    result.events.append(event("Original", "Original retained text", "text:chunk:0"))
    return result


def suggestion(*, format="json", path="export.entries", **fields):
    return {"kind": "mapping", "format": format, "record_path": path, "fields": {"id": "", "title": "heading", "body": "words", "occurred_at": "when", "end_at": "", "category": "", **fields}, "events": [], "notes": "Proposed mapping", "model": "gpt-6.1-sol"}


def quoted(body, *, when="", title=None):
    return {"kind": "events", "format": "text", "record_path": "", "fields": {k: "" for k in ("id", "title", "body", "occurred_at", "end_at", "category")}, "events": [{"title": title or body[:30], "body": body, "occurred_at": when, "end_at": "", "category": "record", "locator": "model:untrusted"}], "notes": "", "model": "gpt-6.1-sol"}


def write_json(path, records):
    path.write_text(json.dumps({"export": {"entries": records}}))


def fail_if_called(*args, **kwargs):
    raise AssertionError("No model invocation was expected")


def test_json_mapping_applies_all_records_and_cache_uses_schema_not_content(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "first.json"
    records = [{"heading": f"Entry {i}", "words": f"Source body {i}", "when": "2020-01-02"} for i in range(30)]
    write_json(path, records)
    calls = []
    def propose(profile, work):
        calls.append(profile)
        assert len(profile["sample"]["export"]["entries"]) == 3
        assert profile["record_count"] == 30
        return suggestion()
    monkeypatch.setattr(adaptive, "suggest_adapter", propose)
    result = adaptive.adapt_file(path, "exports/first.json", "Synthetic", baseline, state)
    assert result.status == "parsed"
    assert len(result.events) == 30
    assert result.events[-1]["body"] == "Source body 29"
    assert result.events[-1]["locator"] == "$.export.entries[29]"
    assert result.events[0]["occurred_at"] == "2020-01-02T00:00:00Z"
    assert not result.events[0]["metadata"]["adaptive"]["cached_mapping"]
    records[0]["words"] = "Different content with identical schema"
    write_json(path, records)
    again = adaptive.adapt_file(path, "exports/second.json", "Synthetic", baseline, state)
    assert len(calls) == 1
    assert again.events[0]["body"] == records[0]["words"]
    assert again.events[0]["metadata"]["adaptive"]["cached_mapping"]


def test_cache_is_source_specific_and_corrupt_cache_is_revalidated(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "source.json"
    write_json(path, [{"heading": "Entry", "words": "Text", "when": "2020-01-01"}])
    calls = []
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: (calls.append(a) or suggestion()))
    adaptive.adapt_file(path, "source.json", "One", baseline, state)
    adaptive.adapt_file(path, "source.json", "Two", baseline, state)
    assert len(calls) == 2
    for cache in (state / "adapter-cache").glob("*.json"):
        cache.write_text('{"kind":"mapping","fields":"malicious"}')
    result = adaptive.adapt_file(path, "source.json", "One", baseline, state)
    assert result.status == "parsed"
    assert len(calls) == 3


def test_csv_maps_literal_column_labels_and_preserves_unknown_timezone(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "table.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Heading", "Private notes", "Local date/time"])
        writer.writerow(["First entry", "Exact note", "2020-01-01T10:30:00"])
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: suggestion(format="csv", path="", title="Heading", body="Private notes", occurred_at="Local date/time"))
    result = adaptive.adapt_file(path, "table.csv", "Synthetic", baseline, state)
    assert result.status == "parsed"
    assert result.events[0]["body"] == "Exact note"
    assert result.events[0]["occurred_at"] == "2020-01-01"
    assert result.events[0]["time_precision"] == "day"
    assert result.events[0]["metadata"]["original_datetime"] == "2020-01-01T10:30:00"
    assert result.events[0]["metadata"]["timestamp_role"] == "local_calendar_date_not_utc_instant"


@pytest.mark.parametrize("name", [".env", "export/cookies.json", "export/private_key.json", "export/excluded-folder/notes.txt"])
def test_existing_source_exclusions_never_reach_model(tmp_path, state, baseline, monkeypatch, name):
    path = tmp_path / "opaque-object"
    path.write_text("credentials must stay local")
    monkeypatch.setattr(adaptive, "suggest_adapter", fail_if_called)
    assert adaptive.adapt_file(path, name, "Synthetic", baseline, state, excluded_directory_names=("excluded-folder",)) is baseline


def test_configured_excluded_directory_names_respected(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "opaque-object"
    path.write_text("Excluded content")
    monkeypatch.setattr(adaptive, "settings", SimpleNamespace(excluded_directory_names=("private-subfolder",)))
    monkeypatch.setattr(adaptive, "suggest_adapter", fail_if_called)
    assert adaptive.adapt_file(path, "export/PRIVATE-SUBFOLDER/notes.txt", "Synthetic", baseline, state) is baseline


def test_structural_profile_redacts_credentials_and_mapping_cannot_select_them(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "source.json"
    write_json(path, [{"heading": "Entry", "words": "Text", "when": "2020-01-01", "password": "private-never-send", "nested": {"api_key": "private-never-send"}}])
    def propose(profile, work):
        assert "private-never-send" not in json.dumps(profile)
        return suggestion(body="password")
    monkeypatch.setattr(adaptive, "suggest_adapter", propose)
    result = adaptive.adapt_file(path, "source.json", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert result.events is baseline.events
    assert "Credential fields" in result.notes[-1]
    assert not list((state / "adapter-cache").glob("*.json"))


def test_mapped_object_bodies_use_existing_recursive_secret_redaction(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "source.json"
    write_json(path, [{"heading": "Entry", "words": {"note": "Evidence", "password": "never-index"}, "when": "2020-01-01"}])
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: suggestion())
    result = adaptive.adapt_file(path, "source.json", "Synthetic", baseline, state)
    assert "Evidence" in result.events[0]["body"]
    assert "never-index" not in result.events[0]["body"]
    assert "REDACTED" in result.events[0]["body"]


def test_text_events_have_verified_quote_provenance_and_grounded_dates(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "notes.txt"
    quote = "2020-01-02: I moved to a new apartment."
    path.write_text("Heading\n" + quote + "\nEnd")
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: quoted(quote, when="2020-01-02", title="I moved"))
    result = adaptive.adapt_file(path, "notes.txt", "Synthetic", baseline, state)
    value = result.events[0]
    assert result.status == "parsed"
    assert value["body"] == quote
    assert value["occurred_at"] == "2020-01-02T00:00:00Z"
    assert value["metadata"]["ai_inferred"] is True
    assert value["metadata"]["adaptive"]["evidence_is_verbatim"] is True
    assert value["locator"] == f"ai:sample:chars:8-{8+len(quote)}"
    assert "AI-inferred" in result.notes[-1]


def test_model_suggested_date_and_title_need_support_in_same_quote(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "notes.txt"
    quote = "I moved to a new apartment sometime that year."
    path.write_text(quote)
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: quoted(quote, when="1999-01-01", title="Won a Nobel Prize"))
    result = adaptive.adapt_file(path, "notes.txt", "Synthetic", baseline, state)
    assert result.events[0]["occurred_at"] is None
    assert "Nobel" not in result.events[0]["title"]
    assert "Discarded 1" in result.notes[-1]


def test_ungrounded_text_event_keeps_baseline(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("The real source says I went for a walk.")
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: quoted("I became an astronaut."))
    result = adaptive.adapt_file(path, "notes.txt", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert result.events is baseline.events
    assert "verbatim" in result.notes[-1]


def test_text_and_malformed_json_headers_redacted_before_model(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text('A real note.\nCookie: sessionid=private-cookie\nAuthorization: Bearer private-auth\n{"access_token": "private-json-token", unfinished')
    def propose(profile, work):
        serialized = json.dumps(profile)
        for secret in ("private-cookie", "private-auth", "private-json-token"):
            assert secret not in serialized
        return quoted("A real note.")
    monkeypatch.setattr(adaptive, "suggest_adapter", propose)
    result = adaptive.adapt_file(path, "notes.txt", "Synthetic", baseline, state)
    assert result.status == "parsed"


def test_credential_collection_never_reaches_model(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "browser-export.json"
    path.write_text('{"cookies":[{"name":"sessionid","value":"private-cookie"}]}')
    monkeypatch.setattr(adaptive, "suggest_adapter", fail_if_called)
    result = adaptive.adapt_file(path, "browser-export.json", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert "Credential collections" in result.notes[-1]


@pytest.mark.parametrize("length", [adaptive.TEXT_SAMPLE_LIMIT + 1, adaptive.TEXT_FILE_LIMIT + 1])
def test_capped_text_is_retained_without_model_call(tmp_path, state, baseline, monkeypatch, length):
    path = tmp_path / "notes.txt"
    path.write_text("x" * length)
    monkeypatch.setattr(adaptive, "suggest_adapter", fail_if_called)
    result = adaptive.adapt_file(path, "notes.txt", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert result.events is baseline.events


def test_malformed_small_json_can_be_inert_quoted_text(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "broken.json"
    quote = '2020-01-01: retained source evidence'
    path.write_text('{"unfinished": ' + quote)
    def propose(profile, work):
        assert profile["format"] == "text"
        return quoted(quote, when="2020-01-01")
    monkeypatch.setattr(adaptive, "suggest_adapter", propose)
    result = adaptive.adapt_file(path, "broken.json", "Synthetic", baseline, state)
    assert result.parser == "codex.quoted-events.v1"
    assert result.events[0]["body"] == quote


def test_malformed_large_json_validated_before_any_model_call(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text('[{"words":"a record"},' + " " * (adaptive.TEXT_FILE_LIMIT + 1) + "invalid]")
    monkeypatch.setattr(adaptive, "suggest_adapter", fail_if_called)
    result = adaptive.adapt_file(path, "broken.json", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert result.events is baseline.events


def test_large_valid_json_streams_every_record_without_whole_file_read(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "large.json"
    write_json(path, [{"heading": f"Entry {i}", "words": "x" * 4_000, "when": "2020-01-01"} for i in range(180)])
    assert path.stat().st_size > adaptive.TEXT_FILE_LIMIT
    original_read_bytes = Path.read_bytes
    def no_source_read_bytes(self):
        assert self != path, "Structured source must be streamed"
        return original_read_bytes(self)
    monkeypatch.setattr(Path, "read_bytes", no_source_read_bytes)
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: suggestion())
    result = adaptive.adapt_file(path, "large.json", "Synthetic", baseline, state)
    assert result.status == "parsed"
    assert len(result.events) == 180
    assert len(result.events[-1]["body"]) == 4_000


def test_oversized_json_scalar_does_not_materialize_or_reach_model(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "huge-scalar.json"
    write_json(path, [{"heading": "Entry", "words": "x" * (adaptive.RECORD_BYTE_LIMIT * 2)}])
    monkeypatch.setattr(adaptive, "suggest_adapter", fail_if_called)
    result = adaptive.adapt_file(path, "source.json", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert "bounded record size" in result.notes[-1]


def test_mapping_that_misses_later_record_is_all_or_baseline(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "mixed.json"
    write_json(path, [{"heading": "First", "words": "Text", "when": "2020-01-01"}, {"different": "unmapped"}])
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: suggestion())
    result = adaptive.adapt_file(path, "source.json", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert result.events is baseline.events
    assert not list((state / "adapter-cache").glob("*.json"))


@pytest.mark.parametrize("field", ["__import__('os').system('bad')", "words[0]", "../words"])
def test_field_expressions_never_execute(tmp_path, state, baseline, monkeypatch, field):
    path = tmp_path / "source.json"
    write_json(path, [{"heading": "First", "words": "Text", "when": "2020-01-01"}])
    monkeypatch.setattr(adaptive, "suggest_adapter", lambda *a: suggestion(body=field))
    result = adaptive.adapt_file(path, "source.json", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert result.events is baseline.events


def test_agent_failure_preserves_baseline_and_explicit_status(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("Actual source text.")
    def unavailable(*args):
        raise AgentError("ChatGPT usage limit reached", "cli_failed")
    monkeypatch.setattr(adaptive, "suggest_adapter", unavailable)
    result = adaptive.adapt_file(path, "notes.txt", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    assert result.events is baseline.events
    assert "usage limit" in result.notes[-1]


def test_binary_or_source_symlink_never_reaches_model(tmp_path, state, baseline, monkeypatch):
    path = tmp_path / "file.pdf"
    path.write_bytes(b"%PDF-a-binary-source")
    monkeypatch.setattr(adaptive, "suggest_adapter", fail_if_called)
    result = adaptive.adapt_file(path, "file.pdf", "Synthetic", baseline, state)
    assert result.status == "needs_fallback"
    linked = tmp_path / "link"
    linked.symlink_to(path)
    assert adaptive.adapt_file(linked, "notes.txt", "Synthetic", baseline, state) is baseline
