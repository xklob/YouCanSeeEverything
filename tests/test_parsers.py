import json
import zipfile
from pathlib import Path

import pytest

from biography.parsers import parse_file
from biography.parsers.common import parse_date
from biography.parsers.json_records import stream_array


def export(tmp_path: Path, name: str, value, source="Unknown"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return parse_file(path, name, source)


@pytest.mark.parametrize("stamp,unit", [(1704067200, "s"), (1704067200000, "ms"), (1704067200000000, "us"), (1704067200000000000, "ns")])
def test_epoch_timestamp_units(stamp, unit):
    occurred, _, metadata = parse_date(stamp)
    assert occurred == "2024-01-01T00:00:00Z"
    assert metadata["timestamp_unit"] == unit


def test_date_precision_and_unknown_local_timezone():
    occurred, precision, metadata = parse_date("2024-06-10")
    assert occurred == "2024-06-10T00:00:00Z"
    assert precision == "day"
    assert metadata["date_is_day_bucket"] is True
    local_date, local_precision, local_metadata = parse_date("2024-06-10 19:30:00")
    assert (local_date, local_precision) == ("2024-06-10", "day")
    assert local_metadata["timezone_unknown"] is True
    assert local_metadata["original_datetime"] == "2024-06-10T19:30:00"
    assert local_metadata["timestamp_role"] == "local_calendar_date_not_utc_instant"
    assert parse_date("2024-06-10T19:30:00-04:00")[0] == "2024-06-10T23:30:00Z"
    assert parse_date("June 10")[0] is None


def test_meta_unicode_and_overlapping_exports_preserve_repeated_messages(tmp_path):
    message = {"sender_name": "Jos\u00c3\u00a9", "timestamp_ms": 1704067200123, "content": "Hi \u00f0\u009f\u0098\u0080"}
    value = {"participants": [{"name": "Jos\u00c3\u00a9"}], "thread_path": "inbox/friends_123", "messages": [message, dict(message)]}
    first = export(tmp_path, "archive-a/message_1.json", value, "Meta")
    second = export(tmp_path, "archive-b/message_1.json", value, "Meta")
    assert first.parser == "meta.messages"
    assert len(first.events) == 2
    assert first.events[0]["title"] == "José"
    assert first.events[0]["body"] == "Hi 😀"
    assert first.events[0]["occurred_at"] == "2024-01-01T00:00:00.123000Z"
    fingerprints = [e["metadata"]["record_fingerprint"] for e in first.events]
    assert fingerprints[0] != fingerprints[1]
    assert fingerprints == [e["metadata"]["record_fingerprint"] for e in second.events]
    assert all(e["source_native_id"] is None for e in first.events)


def test_meta_thread_scope_prevents_cross_thread_dedup(tmp_path):
    message = {"sender_name": "Alex", "timestamp_ms": 1704067200000, "content": "hello"}
    first = export(tmp_path, "first.json", {"thread_path": "inbox/one_123", "messages": [message]}, "Meta")
    second = export(tmp_path, "second.json", {"thread_path": "inbox/two_456", "messages": [message]}, "Meta")
    assert first.events[0]["metadata"]["record_fingerprint"] != second.events[0]["metadata"]["record_fingerprint"]


def test_spotify_never_collapses_identical_repeated_plays(tmp_path):
    play = {"endTime": "2024-01-01 00:00", "artistName": "Artist", "trackName": "Track", "msPlayed": 180000}
    parsed = export(tmp_path, "history.json", [play, dict(play)], "Spotify")
    assert len(parsed.events) == 2
    assert parsed.events[0]["locator"] != parsed.events[1]["locator"]
    assert parsed.events[0]["metadata"]["record_fingerprint"] != parsed.events[1]["metadata"]["record_fingerprint"]
    assert all(e["source_native_id"] is None for e in parsed.events)
    assert all(e["occurred_at"] == "2024-01-01T00:00:00Z" for e in parsed.events)
    assert all(e["time_precision"] == "minute" for e in parsed.events)


def test_chatgpt_mapping_uses_message_timestamp_and_keeps_branches(tmp_path):
    value = [{"id": "conversation", "title": "Example", "create_time": 1, "mapping": {
        "root": {"message": None},
        "a": {"parent": "root", "message": {"id": "m-a", "create_time": 1704067200, "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Question"]}}},
        "b": {"parent": "a", "message": {"id": "m-b", "create_time": None, "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["Answer"]}}},
    }}]
    parsed = export(tmp_path, "conversations.json", value)
    assert len(parsed.events) == 2
    assert parsed.events[0]["source_native_id"] == "m-a"
    assert parsed.events[0]["occurred_at"] == "2024-01-01T00:00:00Z"
    assert parsed.events[1]["occurred_at"] is None


def test_reddit_post_and_comment_ids_are_namespaced(tmp_path):
    parsed = export(tmp_path, "reddit.json", [
        {"id": "abc", "created_utc": 1704067200, "title": "Post", "selftext": "Text", "subreddit": "example"},
        {"id": "abc", "created_utc": 1704067201, "body": "Reply", "subreddit": "example"},
    ], "Reddit")
    assert [e["source_native_id"] for e in parsed.events] == ["t3_abc", "t1_abc"]


def test_ics_all_day_timezone_and_recurrence_metadata(tmp_path):
    path = tmp_path / "calendar.ics"
    path.write_text("""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:all-day
DTSTART;VALUE=DATE:20240704
DTEND;VALUE=DATE:20240705
SUMMARY:Holiday
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTART;TZID=America/New_York:20240101T190000
DTEND;TZID=America/New_York:20240101T200000
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Weekly class
DESCRIPTION:Line one\\nLine two
END:VEVENT
END:VCALENDAR
""")
    parsed = parse_file(path, "calendar.ics", "Google")
    assert len(parsed.events) == 2
    day, recurring = parsed.events
    assert day["occurred_at"] == "2024-07-04T00:00:00Z"
    assert day["time_precision"] == "day"
    assert day["metadata"]["end_is_exclusive"] is True
    assert recurring["occurred_at"] == "2024-01-02T00:00:00Z"
    assert recurring["metadata"]["recurrence_expanded"] is False
    assert "rrule" in recurring["metadata"]
    assert recurring["body"] == "Line one\nLine two"


def test_unknown_json_keeps_one_coherent_document_and_redacts_credentials(tmp_path):
    parsed = export(tmp_path, "profile.json", {"profile": {"name": "Example", "api_key": "supersecret"}, "settings": {"theme": "dark"}})
    assert len(parsed.events) == 1
    assert parsed.events[0]["occurred_at"] is None
    assert "Example" in parsed.events[0]["body"]
    assert "supersecret" not in json.dumps(list(parsed.events))


def test_obisidian_note_and_inert_html_never_use_mtime(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# An undated note\nA remembered thought")
    parsed = parse_file(note, "vault/note.md", "Obsidian")
    assert parsed.events[0]["category"] == "note"
    assert parsed.events[0]["occurred_at"] is None
    html = tmp_path / "export.html"
    html.write_text('<script>steal()</script><style>bad</style><p onclick="steal()">Visible &amp; safe</p>')
    text = parse_file(html, "export.html", "Unknown").events[0]["body"]
    assert "Visible & safe" in text
    assert "steal" not in text
    assert "<p" not in text


def test_csv_dates_and_tweet_native_ids(tmp_path):
    path = tmp_path / "tweets.csv"
    path.write_text('Date,Link,Text Content,Media File Names,Is RT\n2024-01-01T12:00:00Z,https://x.com/me/status/123,"hello, world",,False\n')
    parsed = parse_file(path, "tweets.csv", "X (Twitter)")
    assert parsed.events[0]["source_native_id"] == "123"
    assert parsed.events[0]["body"] == "hello, world"
    assert parsed.events[0]["occurred_at"] == "2024-01-01T12:00:00Z"


def test_stream_array_records_cross_chunk_boundaries(tmp_path):
    path = tmp_path / "large.json"
    records = [{"body": "a" * 70000}, {"value": 2}]
    path.write_text(json.dumps(records))
    assert [item for _, item in stream_array(path)] == records


def test_content_addressed_objects_dispatch_using_original_name(tmp_path):
    path = tmp_path / ("a" * 64)
    path.write_text(json.dumps([{"title": "A record", "timestamp": 1704067200000}]))
    result = parse_file(path, "export/records.json", "Unknown")
    assert result.status == "parsed"
    assert result.events[0]["title"] == "A record"
    assert result.events[0]["occurred_at"] == "2024-01-01T00:00:00Z"
    path.write_text("A note")
    result = parse_file(path, "vault/Example.md", "Obsidian")
    assert result.events[0]["title"] == "Example"
    assert result.events[0]["category"] == "note"


def test_event_collection_spills_without_losing_sequence_behavior():
    from biography.parsers.events import EventCollection
    from biography.parsers.common import event

    events = EventCollection()
    try:
        events.extend(event(f"Record {i}", "a" * 200000, str(i)) for i in range(50))
        assert events._data._rolled is True
        assert len(events) == 50
        assert events[0]["title"] == "Record 0"
        assert events[-1]["locator"] == "49"
        assert sum(1 for _ in events) == 50
    finally:
        events.close()


def test_invalid_streamed_json_becomes_explicit_partial_or_fallback(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('[{"title":"one"}, {broken]')
    result = parse_file(path, "bad.json", "Unknown")
    assert result.status in {"partial", "needs_fallback"}
    assert result.notes


def test_invalid_jsonl_reports_partial_coverage(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"title":"first"}\nnot json\n{"title":"third"}\n')
    result = parse_file(path, "rows.jsonl", "Unknown")
    assert result.status == "partial"
    assert len(result.events) == 2
    assert "2" in result.notes[0]


def test_credentials_excluded_and_opaque_media_retained(tmp_path):
    result = export(tmp_path, "saved_credit_cards.json", [{"Name on card": "Name"}], "Etsy")
    assert result.status == "excluded"
    assert not result.events
    media = tmp_path / "image.jpg"
    media.write_bytes(b"not a real image")
    result = parse_file(media, "image.jpg", "Unknown")
    assert result.status == "metadata_only"
    assert result.events[0]["metadata"]["mime_type"] == "image/jpeg"


def test_parser_directory_exclusions_are_explicit_configuration(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("A note")
    original = "export/Excluded-Backup/notes.txt"
    assert parse_file(path, original, "Unknown").status == "parsed"
    result = parse_file(path, original, "Unknown", excluded_directory_names=["excluded-backup"])
    assert result.status == "excluded"


def test_chatgpt_retains_structured_media_references_without_duplicating_text(tmp_path):
    value = [{"id": "conversation", "mapping": {"node": {"message": {"id": "message", "author": {"role": "user"}, "content": {"content_type": "multimodal_text", "parts": ["Visible text", {"content_type": "image_asset_pointer", "asset_pointer": "file-service://asset-123", "size_bytes": 1000}]}}}}}]
    result = export(tmp_path, "conversations.json", value)
    parts = result.events[0]["metadata"]["content_parts"]
    assert len(parts) == 1
    assert parts[0]["asset_pointer"] == "file-service://asset-123"
    assert all(not isinstance(part, str) for part in parts)


def test_google_legacy_activity_presentation_preserves_source_metadata(tmp_path):
    activity = {"activityType": "IN_PASSENGER_VEHICLE", "distance": 1500, "duration": {"startTimestamp": "2024-01-01T12:00:00Z", "endTimestamp": "2024-01-01T13:05:00Z"}, "startLocation": {"latitudeE7": 10000000, "longitudeE7": 20000000}, "endLocation": {"latitudeE7": 20000000, "longitudeE7": 30000000}, "confidence": "HIGH"}
    result = export(tmp_path, "timeline.json", {"timelineObjects": [{"activitySegment": activity}]}, "Google")
    record = result.events[0]
    assert record["title"] == "In a vehicle"
    assert record["body"] == "In a vehicle\nDistance: 1.5 km\nDuration: 1 h 5 min"
    assert record["metadata"]["activityType"] == activity["activityType"]
    assert record["metadata"]["startLocation"] == activity["startLocation"]
    assert record["locator"] == "$.timelineObjects[0].activitySegment"


def test_google_place_name_and_address_are_readable_without_json(tmp_path):
    place = {"location": {"name": "Example Cafe", "address": "123 Example Street", "placeId": "ChIJ-opaque-source-id"}, "duration": {"startTimestampMs": "1704110400000", "endTimestampMs": "1704112200000"}}
    record = export(tmp_path, "timeline.json", [{"placeVisit": place}], "Google").events[0]
    assert record["title"] == "Example Cafe"
    assert record["body"] == "Example Cafe\nAddress: 123 Example Street\nDuration: 30 min"
    assert "placeId" not in record["body"]
    assert record["metadata"]["location"]["placeId"] == "ChIJ-opaque-source-id"


def test_google_semantic_place_id_is_not_a_human_place_name(tmp_path):
    value = {"semanticSegments": [{"startTime": "2024-01-01T12:00:00Z", "endTime": "2024-01-01T12:10:00Z", "visit": {"topCandidate": {"placeID": "ChIJ-opaque-source-id", "placeLocation": {"latLng": "geo:1,2"}}}}]}
    record = export(tmp_path, "timeline.json", value, "Google").events[0]
    assert record["title"] == "Place visit"
    assert record["body"] == "Place visit\nDuration: 10 min"
    assert record["metadata"]["visit"]["topCandidate"]["placeID"] == "ChIJ-opaque-source-id"


def test_google_semantic_activity_uses_top_candidate_and_explicit_metrics(tmp_path):
    value = {"startTime": "2024-01-01T12:00:00Z", "activity": {"topCandidate": {"type": "ON_BICYCLE"}, "distanceMeters": 2400, "durationSeconds": 125}}
    record = export(tmp_path, "timeline.json", value, "Google").events[0]
    assert record["title"] == "Cycling"
    assert record["body"] == "Cycling\nDistance: 2.4 km\nDuration: 2 min 5 s"


def test_google_activity_does_not_infer_distance_from_coordinates_or_local_timezones(tmp_path):
    value = {"startTime": "2024-01-01T12:00:00", "endTime": "2024-01-01T13:00:00", "activity": {"type": "WALKING", "start": {"latLng": "geo:1,2"}, "end": {"latLng": "geo:2,3"}}}
    record = export(tmp_path, "timeline.json", value, "Google").events[0]
    assert record["title"] == "Walking"
    assert record["body"] == "Walking"
    assert record["metadata"]["activity"] == value["activity"]


def test_google_legacy_preserves_all_waypoints_and_complete_metadata_fields(tmp_path):
    from biography.locations import extract_points

    points = [{"latitudeE7": 10000000 + i, "longitudeE7": 20000000 + i} for i in range(85)]
    activity = {"activityType": "WALKING", "simplifiedRawPath": {"points": points}, "custom": {f"field_{i}": i for i in range(120)}, "description": "a" * 3500, "deep": {"a": {"b": {"c": {"d": {"e": "original"}}}}}, "access_token": "private-value"}
    record = export(tmp_path, "timeline.json", [{"activitySegment": activity}], "Google").events[0]
    metadata = record["metadata"]
    assert metadata["simplifiedRawPath"]["points"] == points
    assert metadata["custom"] == activity["custom"]
    assert metadata["description"] == activity["description"]
    assert metadata["deep"] == activity["deep"]
    assert metadata["access_token"] == "[REDACTED CREDENTIAL]"
    extracted = extract_points(metadata, category="travel")
    assert len(extracted) == 85
    assert [(p["latitude"], p["longitude"]) for p in extracted] == [(p["latitudeE7"]/1e7, p["longitudeE7"]/1e7) for p in points]


def test_google_semantic_preserves_every_explicit_path_point_and_no_extra_points(tmp_path):
    from biography.locations import extract_points

    points = [{"point": f"geo:1.{i:03},2.{i:03}", "durationMinutesOffsetFromStartTime": str(i)} for i in range(75)]
    value = {"startTime": "2024-01-01T12:00:00Z", "timelinePath": points}
    record = export(tmp_path, "timeline.json", value, "Google").events[0]
    assert record["metadata"]["timelinePath"] == points
    extracted = extract_points(record["metadata"], record["occurred_at"], category="travel")
    assert len(extracted) == 75
    assert [point["locator"] for point in extracted] == [f"$.timelinePath[{i}]" for i in range(75)]


def test_google_candidate_arrays_remain_metadata_without_becoming_visits(tmp_path):
    from biography.locations import extract_points

    value = {"location": {"name": "Recorded place", "latitudeE7": 10000000, "longitudeE7": 20000000}, "otherCandidateLocations": [{"latitudeE7": i, "longitudeE7": i} for i in range(70)]}
    record = export(tmp_path, "timeline.json", [{"placeVisit": value}], "Google").events[0]
    assert record["metadata"]["otherCandidateLocations"] == value["otherCandidateLocations"]
    assert len(extract_points(record["metadata"], category="place_visit")) == 1


def test_non_map_metadata_stays_bounded(tmp_path):
    value = {"title": "A generic record", "points": [{"value": i} for i in range(80)], "deep": {"a": {"b": {"c": {"d": {"e": "original"}}}}}}
    record = export(tmp_path, "other.json", value, "Unknown").events[0]
    assert len(record["metadata"]["points"]) == 31
    assert record["metadata"]["points"][-1] == {"additional_items_retained_in_source": 50}


@pytest.mark.parametrize("depth", [0, 1, 2, 4])
def test_auth_query_uuid_redaction_preserves_unrelated_text_and_encoding(depth):
    from urllib.parse import quote, unquote
    from biography.parsers.common import clean_text, redact_auth_query_values

    first = "12345678-1234-1234-1234-123456789abc"
    second = "abcdef12-3456-7890-abcd-ef1234567890"
    ordinary = "fedcba98-7654-3210-abcd-0123456789ab"
    address = "0x" + "1234567890abcdef" * 2 + "12345678"
    url = f"https://example.test/start?token={first}&access_token={second}&token_address={address}&request_id={ordinary}"
    encoded = url
    for _ in range(depth):
        encoded = quote(encoded, safe="")
    text = f"Ordinary UUID {ordinary}. URL: {encoded}."
    redacted = redact_auth_query_values(text)
    assert first not in redacted
    assert second not in redacted
    assert redacted.startswith(f"Ordinary UUID {ordinary}. URL: ")
    decoded = redacted
    for _ in range(depth):
        decoded = unquote(decoded)
    assert f"token_address={address}" in decoded
    assert f"request_id={ordinary}" in decoded
    assert decoded.count("[REDACTED CREDENTIAL]") == 2
    assert redact_auth_query_values(redacted) == redacted
    assert clean_text(text, len(text) + 1000) == redacted


def test_nested_percent_encoded_auth_keys_and_values_are_redacted_without_truncation():
    from urllib.parse import quote, unquote
    from biography.parsers.common import redact_auth_query_values

    opaque = "12345678-1234-1234-1234-123456789abc"
    url = "https://example.test/?%74%6f%6b%65%6e=" + "".join(f"%{ord(c):02x}" for c in opaque)
    text = "Long public introduction. " * 15000 + quote(url, safe="") + " END"
    cleaned = redact_auth_query_values(text)
    assert cleaned.endswith(" END")
    assert cleaned.startswith("Long public introduction. " * 15000)
    decoded = unquote(unquote(cleaned))
    assert opaque not in decoded
    assert "[REDACTED CREDENTIAL]" in decoded


def test_public_blockchain_token_query_addresses_remain_visible():
    from biography.parsers.common import clean_text, redact_auth_query_values

    address = "0x" + "1234567890abcdef" * 2 + "12345678"
    for key in ("token", "access_token", "refresh_token", "tokenAddress"):
        text = f"https://example.test/swap?{key}={address}"
        assert redact_auth_query_values(text) == text
        assert clean_text(text) == text


def test_google_fit_data_points_stream_and_preserve_nanosecond_intervals(tmp_path, monkeypatch):
    pytest.importorskip("ijson")
    point = {"fitValue": [{"value": {"intVal": 7}}], "originDataSourceId": "raw:example:stream-a", "dataTypeName": "com.google.step_count.delta", "startTimeNanos": 1704067200123456789, "endTimeNanos": 1704067201123456789, "modifiedTimeMillis": 1793491200000, "rawTimestampNanos": 0}
    value = {"Data Source": "derived:example:stream-a", "Data Points": [point, dict(point)]}
    path = tmp_path / "fitness.json"
    path.write_text(json.dumps(value))
    def no_whole_file_json_load(*args, **kwargs):
        raise AssertionError("Fit point arrays must use the streaming reader")
    monkeypatch.setattr(json, "load", no_whole_file_json_load)
    result = parse_file(path, "All Data/fitness.json", "Google")
    assert result.status == "parsed" and result.parser == "google.fit_points"
    assert len(result.events) == 2
    first, repeated = result.events
    assert first["occurred_at"] == "2024-01-01T00:00:00.123456Z"
    assert first["end_at"] == "2024-01-01T00:00:01.123456Z"
    assert first["metadata"]["startTimeNanos"] == point["startTimeNanos"]
    assert first["metadata"]["endTimeNanos"] == point["endTimeNanos"]
    assert first["metadata"]["Data Source"] == value["Data Source"]
    assert first["metadata"]["data_source_id"] == point["originDataSourceId"]
    assert first["category"] == "measurement"
    assert first["source_native_id"] is repeated["source_native_id"] is None
    assert first["locator"] != repeated["locator"]


def test_google_fit_missing_start_never_uses_modified_or_raw_capture_time(tmp_path):
    point = {"fitValue": [], "dataTypeName": "com.google.heart_rate.bpm", "endTimeNanos": 1704067200000000000, "modifiedTimeMillis": 1793491200000, "rawTimestampNanos": 1793491200000000000}
    result = export(tmp_path, "fitness.json", {"Data Points": [point]}, "Google")
    assert result.events[0]["occurred_at"] is None
    assert result.events[0]["end_at"] == "2024-01-01T00:00:00Z"


def test_fit_context_uses_top_level_source_not_a_nested_lookalike(tmp_path):
    point = {"fitValue": [{"value": {"intVal": 7}, "Data Source": "nested-lookalike"}], "dataTypeName": "com.google.step_count.delta", "startTimeNanos": 1704067200000000000}
    result = export(tmp_path, "fitness.json", {"Data Points": [point], "Data Source": "actual-top-level-stream"}, "Google")
    assert result.events[0]["metadata"]["export_context"]["data_source"] == "actual-top-level-stream"
    assert result.events[0]["metadata"]["export_context"]["data_source_status"] == "available"


def test_late_fit_source_outside_bounded_prefix_is_explicitly_unavailable(tmp_path):
    pytest.importorskip("ijson")
    point = {"fitValue": [{"value": {"intVal": 7}, "Data Source": "nested-lookalike"}], "dataTypeName": "com.google.step_count.delta", "startTimeNanos": 1704067200000000000}
    path = tmp_path / "fitness.json"
    path.write_text(json.dumps({"Data Points": [point] * 500, "Data Source": "late-top-level-stream"}))
    result = parse_file(path, path.name, "Google")
    assert len(result.events) == 500
    context = result.events[0]["metadata"]["export_context"]
    assert context["data_source"] is None
    assert context["data_source_status"] == "unavailable_in_bounded_header"
    assert "data_source_id" not in result.events[0]["metadata"]


def test_nanosecond_conversion_does_not_round_into_a_false_microsecond():
    assert parse_date(1704067200123456789, unit="ns")[0] == "2024-01-01T00:00:00.123456Z"
    assert parse_date(-1, unit="ns")[0] == "1969-12-31T23:59:59.999999Z"


def test_data_points_named_collection_requires_actual_fit_point_fields(tmp_path):
    result = export(tmp_path, "ordinary.json", {"Data Points": [{"name": "A generic record"}]})
    assert result.events[0]["category"] == "document"
    assert result.events[0]["occurred_at"] is None


def test_activity_probability_interval_uses_explicit_times_not_utc_offset_twice(tmp_path):
    path = tmp_path / "UserActivityProbabilities_2099-01-01.csv"
    path.write_text('activity_probabilities_start,activity_probabilities_end,utc_offset,activity_probabilities\n2024-01-01T12:00:00Z,2024-01-01T12:00:30Z,-05:00,"still: 0.3; walking: 0.7"\n')
    record = parse_file(path, path.name, "Google").events[0]
    assert record["occurred_at"] == "2024-01-01T12:00:00Z"
    assert record["end_at"] == "2024-01-01T12:00:30Z"
    assert record["metadata"]["activity_probabilities_start"] == "2024-01-01T12:00:00Z"
    assert record["metadata"]["utc_offset"] == "-05:00"
    assert record["category"] == "measurement"
    assert record["title"] == "Activity probability sample"
    assert "confirmed activities" in record["body"]


@pytest.mark.parametrize("start,expected", [("2024-01-01T12:00:00", "2024-01-01"), ("12:00:00", None), ("", None)])
def test_activity_probability_missing_timezone_or_date_stays_honest(tmp_path, start, expected):
    path = tmp_path / "UserActivityProbabilities_2099-01-01.csv"
    path.write_text(f'activity_probabilities_start,activity_probabilities_end,utc_offset,activity_probabilities\n{start},,-05:00,unknown\n')
    record = parse_file(path, path.name, "Google").events[0]
    assert record["occurred_at"] == expected
    if expected:
        assert record["metadata"]["timezone_unknown"] is True
        assert record["time_precision"] == "day"


def test_ppk_policy_does_not_read_key_contents(tmp_path, monkeypatch):
    def forbidden_open(*args, **kwargs):
        raise AssertionError("Credential content must not be opened")
    monkeypatch.setattr(Path, "open", forbidden_open)
    result = parse_file(tmp_path / "opaque-object", "attachments/private.PPK", "Meta")
    assert result.status == "excluded"
    assert len(result.events) == 0


def test_eventbrite_orders_and_etsy_purchases_use_meaningful_dates(tmp_path):
    order = export(tmp_path, "orders.json", [{"event_data": {"event_name": "Concert", "start_date": "2024-05-01 19:00:00", "end_date": "2024-05-01 22:00:00", "venue": "Venue"}}], "Eventbrite").events[0]
    assert order["title"] == "Concert"
    assert order["time_precision"] == "day"
    assert order["occurred_at"] == "2024-05-01"
    assert order["metadata"]["original_datetime"] == "2024-05-01T19:00:00"
    assert order["metadata"]["timestamp_role"] == "local_calendar_date_not_utc_instant"
    purchase = export(tmp_path, "purchases.json", [{"Title": "Handmade thing", "Description": "Details", "Paid date": "2024-05-02"}], "Etsy").events[0]
    assert purchase["time_precision"] == "day"
    assert purchase["occurred_at"] == "2024-05-02T00:00:00Z"


def test_tcx_summarizes_activity_and_retains_each_lap_start(tmp_path):
    path = tmp_path / "activity.tcx"
    path.write_text('''<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"><Activities><Activity Sport="Running"><Id>2024-01-01T12:00:00Z</Id><Lap StartTime="2024-01-01T12:00:00Z"><TotalTimeSeconds>300</TotalTimeSeconds><DistanceMeters>1000</DistanceMeters><Track><Trackpoint><Time>2024-01-01T12:00:00Z</Time></Trackpoint><Trackpoint><Time>2024-01-01T12:05:00Z</Time></Trackpoint></Track></Lap><Lap StartTime="2024-01-01T12:05:00Z"><TotalTimeSeconds>300</TotalTimeSeconds><DistanceMeters>1000</DistanceMeters></Lap></Activity></Activities></TrainingCenterDatabase>''')
    result = parse_file(path, "activity.tcx", "Google")
    assert result.status == "parsed"
    assert len(result.events) == 1
    activity = result.events[0]
    assert activity["occurred_at"] == "2024-01-01T12:00:00Z"
    assert activity["metadata"]["trackpoint_count"] == 2
    assert len(activity["metadata"]["laps"]) == 2
    assert activity["metadata"]["laps"][1]["start_time"] == "2024-01-01T12:05:00Z"


def test_gpx_summarizes_track_instead_of_point_events(tmp_path):
    path = tmp_path / "track.gpx"
    path.write_text('''<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><name>A walk</name><trkseg><trkpt lat="37" lon="-77"><time>2024-01-01T12:00:00Z</time></trkpt><trkpt lat="37.001" lon="-77"><time>2024-01-01T12:05:00Z</time></trkpt></trkseg></trk></gpx>''')
    result = parse_file(path, "track.gpx", "Google")
    assert len(result.events) == 1
    activity = result.events[0]
    assert activity["title"] == "A walk"
    assert activity["metadata"]["trackpoint_count"] == 2
    assert 100 < activity["metadata"]["distance_meters_computed"] < 120
    assert activity["end_at"] == "2024-01-01T12:05:00Z"


@pytest.mark.parametrize("extension,member,content", [
    ("docx", "word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t></w:r></w:p></w:body></w:document>'),
    ("odt", "content.xml", '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><text:p>Hello world</text:p></office:document>'),
])
def test_office_documents_extract_text_without_inventing_occurrence_dates(tmp_path, extension, member, content):
    path = tmp_path / ("note." + extension)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, content)
    result = parse_file(path, path.name, "Google")
    assert result.status == "parsed"
    assert "Hello world" in result.events[0]["body"]
    assert result.events[0]["occurred_at"] is None


def test_rtf_extracts_unicode_and_ignores_embedded_objects(tmp_path):
    path = tmp_path / "note.rtf"
    path.write_text(r"{\rtf1\ansi Hello \u233?\par {\pict embeddedpayload}world}")
    result = parse_file(path, path.name, "Unknown")
    assert result.status == "parsed"
    assert "Hello é\nworld" in result.events[0]["body"]
    assert "embeddedpayload" not in result.events[0]["body"]


def test_normalized_git_uses_author_date_and_labels_other_authors_as_context(tmp_path):
    base = {"kind": "git_commit", "stable_id": "git:abc123", "commit_hash": "abc123", "author": {"name": "Another Author", "email": "other@example.com", "date": "2024-01-01T12:00:00-05:00"}, "committer": {"name": "Maintainer", "email": "maintainer@example.com", "date": "2024-01-02T00:00:00Z"}, "subject": "Implement feature", "body": "Implement feature\n", "personal_attribution": False}
    parsed = export(tmp_path, "other-commit.json", base, "GitHub & local Git").events[0]
    assert parsed["category"] == "project_context"
    assert parsed["title"].startswith("Project context · Another Author:")
    assert parsed["metadata"]["attribution"] == "repository context; not attributed to the profile owner"
    assert parsed["occurred_at"] == "2024-01-01T17:00:00Z"
    assert parsed["metadata"]["committer"]["date"] == "2024-01-02T00:00:00Z"
    personal = export(tmp_path, "personal-commit.json", {**base, "personal_attribution": True}, "GitHub & local Git").events[0]
    assert personal["category"] == "git_commit"
    assert personal["title"] == "Implement feature"


def test_normalized_git_identity_payload_matches_across_local_and_github_provenance(tmp_path):
    common = {"kind": "git_commit", "stable_id": "git:abc123", "commit_hash": "abc123", "author": {"name": "Example Contributor", "email": "me@example.com", "date": "2024-01-01T12:00:00Z"}, "committer": {"name": "Example Contributor", "email": "me@example.com", "date": "2024-01-01T12:00:00Z"}, "subject": "A commit", "body": "A commit", "personal_attribution": True}
    local = {**common, "author": {**common["author"], "epoch": 1704110400, "timezone_offset": "+0000"}, "body": "A commit\n", "refs": ["refs/heads/main"], "repositories": [{"path": "/example/worktree", "repository": "example-owner/repo", "source_type": "local_git"}], "attribution_reason": "configured_author_email"}
    remote = {**common, "repositories": [{"repository": "example-owner/repo", "source_type": "github_default_branch", "source_page": "raw/page.json"}], "github_author_login": "example-owner", "url": "https://github.com/example-owner/repo/commit/abc123", "attribution_reason": "github_author_account"}
    first = export(tmp_path, "local.json", local).events[0]
    second = export(tmp_path, "github.json", remote).events[0]
    assert first == second
    assert first["source_native_id"] == "git:abc123"


def test_ethereum_exact_wei_conversion_direction_and_receipt_failure(tmp_path):
    record = {"kind": "ethereum_transaction", "stable_id": "1:tx:0xabc", "chain_id": 1, "address": "0xACCOUNT", "from": {"hash": "0xaccount"}, "to": {"hash": "0xother"}, "hash": "0xabc", "value": "990000000000000001", "timestamp": "2024-01-01T00:00:00Z", "status": "ok", "is_error": True, "captured_at": "2026-01-01T00:00:00Z"}
    parsed = export(tmp_path, "transaction.json", record).events[0]
    assert parsed["source_native_id"] == "1:tx:0xabc"
    assert parsed["occurred_at"] == "2024-01-01T00:00:00Z"
    assert parsed["metadata"]["amount"] == "0.990000000000000001"
    assert parsed["metadata"]["direction"] == "sent"
    assert parsed["metadata"]["receipt_status"] == "failed"
    assert parsed["title"].startswith("Failed attempt")


def test_ethereum_token_decimal_precision_and_log_identity(tmp_path):
    record = {"kind": "ethereum_token_transfer", "chain_id": 1, "address": "0xaccount", "from": {"hash": "0xother"}, "to": {"hash": "0xACCOUNT"}, "transaction_hash": "0xabc", "log_index": 0, "total": {"value": "987654321012345678901234", "decimals": "18"}, "token": {"symbol": "TEST", "decimals": "18", "address_hash": "0xtoken", "type": "ERC-20"}, "timestamp": 1704067200000}
    parsed = export(tmp_path, "transfer.json", record).events[0]
    assert parsed["source_native_id"] == "1:log:0xabc:0"
    assert parsed["metadata"]["amount"] == "987654.321012345678901234"
    assert parsed["metadata"]["direction"] == "received"
    assert parsed["metadata"]["receipt_status"] == "unknown"
    assert parsed["occurred_at"] == "2024-01-01T00:00:00Z"


def test_ethereum_missing_trace_timestamp_stays_undated_despite_capture_time(tmp_path):
    record = {"kind": "ethereum_internal_transaction", "stable_id": "1:trace:0xabc:0", "chain_id": 1, "address": "0xaccount", "from": {"hash": "0xaccount"}, "to": {"hash": "0xaccount"}, "transaction_hash": "0xabc", "index": 0, "value": "0", "captured_at": "2026-01-01T00:00:00Z"}
    parsed = export(tmp_path, "trace.json", record).events[0]
    assert parsed["source_native_id"] == "1:trace:0xabc:0"
    assert parsed["occurred_at"] is None
    assert parsed["time_precision"] == "unknown"
    assert parsed["metadata"]["trace_ordinal"] == 0
    assert parsed["metadata"]["direction"] == "self_transfer"
    assert parsed["metadata"]["receipt_status"] == "unknown"

def test_spotify_prompt_malformed_source_epoch_is_retained_as_unknown(tmp_path):
    result=export(tmp_path,'UserPrompts.json',{'created_timestamp':'1970-01-21T15:54:39.829Z','message':'A synthetic prompt'},'Spotify').events[0]
    assert result['occurred_at'] is None
    assert result['time_precision']=='unknown'
    assert result['metadata']['original_timestamp']=='1970-01-21T15:54:39.829Z'
    assert result['metadata']['timestamp_status']=='invalid_source_timestamp'
    historical=export(tmp_path,'historic-reference.json',{'date':'1970-01-21','title':'Historical reference'},'Notes').events[0]
    assert historical['occurred_at'] is not None
