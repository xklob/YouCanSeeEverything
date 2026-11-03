"""Source-aware JSON adapters and bounded streaming of top-level records."""

from __future__ import annotations

import json
import hashlib
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .common import clean_text, document_text, event, parse_date, pick, safe_value
from .normalized_history import ethereum_record, git_commit


def _fit_export_context(head: str) -> tuple[Any, str]:
    """Inspect only a bounded prefix and only top-level export fields."""
    decoder = json.JSONDecoder()
    cursor = len(head) - len(head.lstrip())
    if cursor >= len(head) or head[cursor] != "{":
        return None, "not_a_top_level_object"
    cursor += 1
    data_source = None
    source_found = False
    try:
        while cursor < len(head):
            while cursor < len(head) and head[cursor].isspace():
                cursor += 1
            if cursor >= len(head) or head[cursor] == "}":
                break
            key, cursor = decoder.raw_decode(head, cursor)
            while cursor < len(head) and head[cursor].isspace():
                cursor += 1
            if cursor >= len(head) or head[cursor] != ":":
                return None, "incomplete_bounded_header"
            cursor += 1
            while cursor < len(head) and head[cursor].isspace():
                cursor += 1
            if key == "Data Points" and source_found:
                return data_source, "available"
            value, cursor = decoder.raw_decode(head, cursor)
            if key == "Data Source":
                data_source, source_found = value, True
            while cursor < len(head) and head[cursor].isspace():
                cursor += 1
            if cursor < len(head) and head[cursor] == ",":
                cursor += 1
            elif cursor < len(head) and head[cursor] == "}":
                break
            else:
                return None, "incomplete_bounded_header"
    except (json.JSONDecodeError, RecursionError):
        return None, "unavailable_in_bounded_header"
    return (data_source, "available") if source_found else (None, "not_present_in_bounded_header")


def json_items(path: Path) -> Iterator[tuple[str, Any]]:
    """Read one logical record at a time, including large known export wrappers."""
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        head = handle.read(65536)
    initial = head.lstrip()
    try:
        import ijson
    except ImportError:
        ijson = None
    if initial.startswith("["):
        if ijson:
            with path.open("rb") as handle:
                try:
                    for index, item in enumerate(ijson.items(handle, "item", use_float=True)):
                        yield f"$[{index}]", item
                except ijson.JSONError as exc:
                    raise ValueError("Invalid JSON array stream") from exc
        else:
            yield from stream_array(path)
        return
    # These containers have record semantics. Stream them rather than loading a
    # whole multi-gigabyte Google Takeout file into memory.
    wrappers = ("timelineObjects", "semanticSegments", "locations", "Data Points", "items", "events", "activities", "records", "tweets")
    wrapper = next((k for k in wrappers if f'"{k}"' in head and initial.startswith("{")), None)
    fit_source = None
    fit_context_status = "not_applicable"
    if wrapper == "Data Points":
        fit_source, fit_context_status = _fit_export_context(head)
    if wrapper and ijson:
        count = 0
        with path.open("rb") as handle:
            try:
                for index, item in enumerate(ijson.items(handle, wrapper + ".item", use_float=True)):
                    count += 1
                    yield f"$.{wrapper}[{index}]", {"_fit_point": item, "_fit_data_source": fit_source, "_fit_context_status": fit_context_status} if wrapper == "Data Points" else item
            except ijson.JSONError as exc:
                raise ValueError("Invalid JSON container stream") from exc
        if count:
            return
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Large non-array JSON requires a recognized streaming container; retained for fallback")
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        value = json.load(handle)
    if isinstance(value, dict):
        if isinstance(value.get("data"), dict) and isinstance(value["data"].get("children"), list):
            for index, item in enumerate(value["data"]["children"]):
                yield f"$.data.children[{index}]", item
            return
        # An actual collection, not every nested object in a profile, is a record.
        key = next((k for k in wrappers if isinstance(value.get(k), list) and value[k]), None)
        if key:
            for index, item in enumerate(value[key]):
                yield f"$.{key}[{index}]", {"_fit_point": item, "_fit_data_source": value.get("Data Source"), "_fit_context_status": "available" if "Data Source" in value else "not_present"} if key == "Data Points" else item
            return
    yield "$", value


def stream_array(path: Path) -> Iterator[tuple[str, Any]]:
    """Incremental stdlib fallback. Memory scales with the largest array item."""
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        buffer = ""
        started = False
        index = 0
        eof = False
        need_comma = False
        while True:
            if not eof and (not buffer.strip() or len(buffer) < 65536):
                chunk = handle.read(65536)
                buffer += chunk
                eof = not chunk
            buffer = buffer.lstrip()
            if not started:
                if not buffer.startswith("["):
                    raise ValueError("Expected JSON array")
                buffer = buffer[1:]
                started = True
                continue
            if buffer.startswith("]"):
                if (buffer[1:] + handle.read()).strip():
                    raise ValueError("Unexpected data after JSON array")
                return
            if need_comma:
                if not buffer.startswith(","):
                    raise ValueError("Expected comma between JSON array records")
                buffer = buffer[1:].lstrip()
                need_comma = False
            try:
                item, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                if eof:
                    raise
                chunk = handle.read(65536)
                buffer += chunk
                eof = not chunk
                continue
            # A number may end at the current chunk boundary. Read once more
            # before yielding if the decoder consumed every available byte.
            if end == len(buffer) and not eof:
                chunk = handle.read(65536)
                buffer += chunk
                eof = not chunk
                continue
            yield f"$[{index}]", item
            index += 1
            buffer = buffer[end:]
            need_comma = True


def fix_meta_encoding(text: Any) -> str:
    """Repair Meta's UTF-8 bytes encoded as Latin-1 only when reversible."""
    value = str(text or "")
    if not any(c in value for c in ("Ã", "Â", "â", "ð", "\x80", "\x9f")):
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired


def fingerprint(kind: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return kind + ":sha256:" + hashlib.sha256(encoded).hexdigest()


def meta_scope(record: dict, path: str) -> str | None:
    thread = str(record.get("thread_path") or "").replace("\\", "/").strip("/")
    if thread:
        match = re.search(r"(?:^|/)(inbox|archived_threads|filtered_threads|message_requests)/([^/]+)", thread)
        return "/".join(match.groups()) if match else thread
    normalized = path.replace("\\", "/")
    match = re.search(r"(?:^|/)(inbox|archived_threads|filtered_threads|message_requests)/([^/]+)/", normalized)
    return "/".join(match.groups()) if match else None


def adapt(record: Any, locator: str, source: str, path: str, limit: int) -> tuple[str, list[dict]]:
    """Dispatch by structural evidence; source names alone never imply a schema."""
    if not isinstance(record, dict):
        return "json.document", [event(Path(path).stem, document_text(record, limit), locator, limit=limit)]
    if isinstance(record.get("_fit_point"), dict):
        point = record["_fit_point"]
        if "fitValue" in point and any(key in point for key in ("startTimeNanos", "endTimeNanos")):
            return "google.fit_points", [google_fit_point(point, locator, limit, data_source=record.get("_fit_data_source"), context_status=record.get("_fit_context_status"))]
        record = point
    if "fitValue" in record and "dataTypeName" in record and any(key in record for key in ("startTimeNanos", "endTimeNanos")):
        return "google.fit_points", [google_fit_point(record, locator, limit)]
    if record.get("kind") in {"t1", "t3"} and isinstance(record.get("data"), dict):
        record = record["data"]
    if record.get("kind") == "git_commit":
        return "history.git", [git_commit(record, locator, limit)]
    if record.get("kind") in {"session_message", "shell_command"}:
        kind = record["kind"]
        metadata = {key: record[key] for key in ("agent", "shell", "session_id", "role", "message_id", "redacted", "source_path", "source_line", "source_record_sha256") if key in record}
        return "history.sessions" if kind == "session_message" else "history.shell", [event(record.get("title") or kind.replace("_", " "), record.get("body", ""), locator, native_id=record.get("stable_id"), when=record.get("timestamp"), category="message" if kind == "session_message" else "shell_command", metadata=metadata, limit=limit)]
    if record.get("kind") in {"ethereum_transaction", "ethereum_token_transfer", "ethereum_internal_transaction"}:
        return "history.ethereum", [ethereum_record(record, locator, limit)]
    low_source = source.casefold()
    if isinstance(record.get("mapping"), dict):
        return "chatgpt.messages", chatgpt(record, locator, limit)
    if "timestamp_ms" in record and ("sender_name" in record or "content" in record):
        return "meta.messages", [meta_message(record, locator, {}, limit)]
    if isinstance(record.get("messages"), list) and ("participants" in record or "thread_path" in record):
        context = {k: record[k] for k in ("title", "participants", "thread_path") if k in record}
        context["thread_scope"] = meta_scope(record, path)
        return "meta.messages", [meta_message(m, f"{locator}.messages[{i}]", context, limit) for i, m in enumerate(record["messages"]) if isinstance(m, dict)]
    if any(k in record for k in ("ms_played", "msPlayed")) and any(k in record for k in ("ts", "endTime")):
        title = pick(record, "master_metadata_track_name", "trackName", "episode_name", "audiobookName", "chapterName", default="Spotify listening")
        artist = pick(record, "master_metadata_album_artist_name", "artistName", "episode_show_name", "authorName")
        body = "\n".join(str(v) for v in [title, artist, record.get("master_metadata_album_album_name"), record.get("chapterName")] if v)
        metadata = {**record, "record_fingerprint": fingerprint("spotify.playback.v1", record), "fingerprint_basis": "complete source playback payload; per-file occurrence ordinal"}
        return "spotify.listening", [event(title, body, locator, native_id=None, when=pick(record, "ts", "endTime"), category="listening", metadata=metadata, assume_utc=True, limit=limit)]
    if "created_utc" in record and any(k in record for k in ("subreddit", "link_id", "selftext", "body")):
        comment = "body" in record
        native = pick(record, "name", default=("t1_" if comment else "t3_") + str(record["id"]) if record.get("id") else None)
        title = record.get("title") or ("Comment in r/" + str(record.get("subreddit", "Reddit")))
        body = pick(record, "body", "selftext", default="")
        metadata = {k: v for k, v in record.items() if k not in {"body", "selftext"}}
        return "reddit.records", [event(title, body, locator, native_id=native, when=record["created_utc"], category="comment" if comment else "post", metadata=metadata, unit="s", limit=limit)]
    if "placeVisit" in record or "activitySegment" in record:
        return "google.timeline", [google_timeline(record, locator, limit)]
    if "startTime" in record and any(k in record for k in ("visit", "activity", "timelinePath")):
        return "google.timeline", [google_semantic(record, locator, limit)]
    if "latitudeE7" in record and "longitudeE7" in record:
        when = pick(record, "timestamp", "timestampMs")
        return "google.location", [event("Location observation", f"{record['latitudeE7']/1e7}, {record['longitudeE7']/1e7}", locator, when=when, category="location", metadata=record, limit=limit, metadata_array_limit=None)]
    if "Messages" in record and isinstance(record["Messages"], list) and "tinder" in low_source:
        return "tinder.export", tinder(record, locator, limit)
    if isinstance(record.get("event_data"), dict) and "event_name" in record["event_data"]:
        info = record["event_data"]
        return "eventbrite.orders", [event(info["event_name"], document_text(info, limit), locator, native_id=pick(record, "order_id", "id"), when=info.get("start_date"), end=info.get("end_date"), category="event_booking", metadata=record, limit=limit)]
    if "eventbrite" in low_source and "event" in record:
        title = record["event"]
        if isinstance(title, dict):
            title = pick(title, "name", "event_name", "title", default="Event attendance")
        return "eventbrite.attendance", [event(title, document_text(record, limit), locator, native_id=pick(record, "attendee_id", "id"), category="event_attendance", metadata=record, limit=limit)]
    if "Title" in record and "Paid date" in record:
        return "etsy.purchases", [event(record["Title"], record.get("Description", ""), locator, native_id=pick(record, "Receipt ID", "Transaction ID"), when=record["Paid date"], category="purchase", metadata=record, limit=limit)]
    if "tweet" in record and isinstance(record["tweet"], dict):
        record = record["tweet"]
    if "full_text" in record and ("id_str" in record or "created_at" in record):
        return "twitter.posts", [event(clean_text(record["full_text"], 150), record["full_text"], locator, native_id=pick(record, "id_str", "id"), when=record.get("created_at"), category="post", metadata={k:v for k,v in record.items() if k != "full_text"}, limit=limit)]
    # Google activity exports provide a title plus source time without a stable ID.
    if "title" in record and "time" in record:
        return "google.activity", [event(record["title"], document_text(record, limit), locator, native_id=pick(record, "id"), when=record["time"], category="activity", metadata=record, limit=limit)]
    when = pick(record, "occurred_at", "timestamp", "timestamp_ms", "created_at", "created_utc", "date", "Date", "created", "date_accepted", "create date", "created_timestamp", "schedule_date", "start_date")
    # Spotify's UserPrompts export can already contain a malformed ISO epoch
    # value (seconds written as milliseconds upstream). Do not guess a repaired
    # instant or present it as personal activity in 1970. Preserve the raw value.
    if ('spotify' in low_source and Path(path).name.casefold()=='userprompts.json'
            and str(record.get('created_timestamp','')).startswith('1970-')):
        record={**record,'original_timestamp':record['created_timestamp'],
            'timestamp_warning':'Implausible timestamp in the original Spotify prompt export; date is unknown.',
            'timestamp_status':'invalid_source_timestamp'}
        when=None
    title = pick(record, "title", "name", "subject", "summary", "event_name", "Title", default=Path(path).stem)
    body = pick(record, "body", "text", "content", "description", "message", default=None)
    if not isinstance(body, str):
        body = document_text(record, limit)
    return "json.records", [event(title, body, locator, native_id=pick(record, "id", "uuid"), when=when, category="record" if when is not None else "document", metadata=record, unit="ms" if "timestamp_ms" in record else None, limit=limit)]


def chatgpt(record: dict, locator: str, limit: int) -> list[dict]:
    result = []
    conversation_id = pick(record, "conversation_id", "id")
    for node_id, node in record["mapping"].items():
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get("content") or {}
        parts = content.get("parts", []) if isinstance(content, dict) else [content]
        text_parts = []
        for part in parts:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                else:
                    text_parts.append(document_text(part, min(limit, 5000)))
        if not text_parts and isinstance(content, dict):
            text_parts.append(document_text(content, limit))
        role = (message.get("author") or {}).get("role", "unknown")
        metadata = {
            "conversation_id": conversation_id,
            "conversation_title": record.get("title"),
            "role": role,
            "parent_node": node.get("parent"),
            "content_type": content.get("content_type") if isinstance(content, dict) else None,
            "message_metadata": message.get("metadata", {}),
        }
        structured_parts = [part for part in parts if isinstance(part, dict)]
        if structured_parts:
            metadata["content_parts"] = structured_parts
        result.append(event(f"{record.get('title') or 'ChatGPT conversation'} · {role}", "\n".join(text_parts), f"{locator}.mapping[{json.dumps(node_id)}].message", native_id=message.get("id"), when=message.get("create_time"), category="message", metadata=metadata, unit="s", limit=limit))
    return result


def meta_message(record: dict, locator: str, context: dict, limit: int) -> dict:
    sender = fix_meta_encoding(record.get("sender_name", "Unknown sender"))
    metadata = dict(context)
    metadata.update({k: v for k, v in record.items() if k != "content"})
    if context.get("thread_scope"):
        canonical = {k:fix_meta_encoding(v) if k in {"content", "sender_name"} else v for k,v in record.items()}
        metadata["record_fingerprint"] = fingerprint("meta.message.v1", {"thread": context["thread_scope"], "message": canonical})
        metadata["fingerprint_basis"] = "thread scope and complete message payload; per-file occurrence ordinal"
    body = fix_meta_encoding(record.get("content", ""))
    if not body:
        body = document_text({k:v for k,v in record.items() if k not in {"timestamp_ms", "sender_name"}}, limit)
    # Meta exports often have no message ID. Timestamp is not an ID: multiple
    # distinct messages can have the same millisecond timestamp.
    return event(sender, body, locator, native_id=pick(record, "message_id", "id"), when=record.get("timestamp_ms"), unit="ms", category="message", metadata=metadata, limit=limit)


def google_fit_point(record: dict, locator: str, limit: int, *, data_source=None, context_status=None) -> dict:
    """One explicit Fit point; stream/interval/value fields remain source data.

    Modified/acquisition timestamps are never substituted for occurrence time,
    and equal repeated points retain separate locators without synthetic IDs.
    """
    metadata = dict(record)
    if data_source is not None and "Data Source" not in metadata:
        metadata["Data Source"] = data_source
    metadata["export_context"] = {"data_source": data_source, "data_source_status": context_status or "not_provided", "bounded_prefix_bytes": 65536}
    stream = record.get("originDataSourceId") or data_source
    if isinstance(stream, str) and stream:
        metadata["data_source_id"] = stream
    data_type = record.get("dataTypeName")
    description = data_type.removeprefix("com.google.").replace("_", " ").replace(".", " · ") if isinstance(data_type, str) else "data point"
    metadata["timestamp_unit"] = "ns"
    metadata["source_timestamp_fields"] = {"start": "startTimeNanos", "end": "endTimeNanos"}
    title = "Google Fit · " + description
    return event(title, "Recorded Google Fit measurement.", locator, native_id=pick(record, "dataPointId", "pointId"), when=record.get("startTimeNanos"), end=record.get("endTimeNanos"), category="measurement", metadata=metadata, unit="ns", limit=limit)


_ACTIVITY_LABELS = {
    "WALKING": "Walking", "ON_FOOT": "Walking", "RUNNING": "Running",
    "CYCLING": "Cycling", "ON_BICYCLE": "Cycling", "BICYCLE": "Cycling",
    "IN_PASSENGER_VEHICLE": "In a vehicle", "IN_VEHICLE": "In a vehicle",
    "IN_CAR": "In a vehicle", "DRIVING": "In a vehicle",
    "MOTORCYCLING": "Motorcycling", "ON_MOTORCYCLE": "Motorcycling",
    "IN_BUS": "By bus", "IN_CITY_BUS": "By bus",
    "IN_TRAIN": "By train", "IN_COMMUTER_TRAIN": "By train",
    "IN_HIGH_SPEED_TRAIN": "By train", "IN_SUBWAY": "By subway",
    "FLYING": "Flying", "IN_AIRPLANE": "Flying",
    "IN_FERRY": "By boat", "BOATING": "By boat", "SAILING": "Sailing",
    "SKIING": "Skiing", "IN_WHEELCHAIR": "In a wheelchair",
    "STILL": "Stationary", "STATIONARY": "Stationary",
}


def _timeline_activity_label(value) -> str:
    normalized = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(value or "")).replace(" ", "_").upper()
    return _ACTIVITY_LABELS.get(normalized, "Travel")


def _timeline_place(value) -> tuple[str, str]:
    """Use explicit display fields only; coordinates and place IDs are not names."""
    if not isinstance(value, dict):
        return "", ""
    def display(*keys):
        for key in keys:
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                text = text.strip()
                if not re.fullmatch(r"(?:ChIJ\S+|0x[0-9a-fA-F]+:0x[0-9a-fA-F]+|[0-9a-fA-F-]{36})", text) and not text.startswith("geo:"):
                    return text
        return ""
    name = display("name", "placeName", "displayName")
    address = display("address", "formattedAddress")
    return name, address


def _timeline_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timeline_duration(value: dict, start, end) -> float | None:
    # Direct duration fields and recorded interval endpoints are source facts.
    # No speed, route geometry, distance, or local-timezone guesses are used.
    containers = [value]
    if isinstance(value.get("duration"), dict):
        containers.insert(0, value["duration"])
    for container in containers:
        for key, scale in (("durationSeconds", 1), ("durationMillis", .001), ("durationMs", .001), ("durationMinutes", 60)):
            number = _timeline_number(container.get(key))
            if number is not None:
                return number * scale
    normalized_start, _, _ = parse_date(start)
    normalized_end, _, _ = parse_date(end)
    if not normalized_start or not normalized_end or "T" not in normalized_start or "T" not in normalized_end:
        return None
    elapsed = (datetime.fromisoformat(normalized_end.replace("Z", "+00:00")) - datetime.fromisoformat(normalized_start.replace("Z", "+00:00"))).total_seconds()
    return elapsed if elapsed >= 0 else None


def _timeline_duration_label(seconds: float) -> str:
    seconds = round(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = [f"{count} {unit}" for count, unit in ((days, "d"), (hours, "h"), (minutes, "min"), (seconds, "s")) if count]
    return " ".join(parts) or "0 s"


def _timeline_summary(title: str, value: dict, start, end, *, place=None, start_place=None, end_place=None) -> str:
    lines = [title]
    if place:
        name, address = _timeline_place(place)
        if address and address not in {name, title}:
            lines.append("Address: " + address)
    for label, point in (("From", start_place), ("To", end_place)):
        if point:
            name, address = _timeline_place(point)
            if name or address:
                lines.append(f"{label}: {name or address}")
    for key in ("distanceMeters", "distance", "distanceInMeters"):
        meters = _timeline_number(value.get(key))
        if meters is not None:
            distance = f"{meters/1000:.2f}".rstrip("0").rstrip(".") + " km" if meters >= 1000 else f"{meters:.1f}".rstrip("0").rstrip(".") + " m"
            lines.append("Distance: " + distance)
            break
    duration = _timeline_duration(value, start, end)
    if duration is not None:
        lines.append("Duration: " + _timeline_duration_label(duration))
    return "\n".join(lines)


def google_timeline(record: dict, locator: str, limit: int) -> dict:
    key = "placeVisit" if "placeVisit" in record else "activitySegment"
    value = record[key]
    duration = value.get("duration", {})
    location = value.get("location", {})
    start = pick(duration, "startTimestamp", "startTimestampMs")
    end = pick(duration, "endTimestamp", "endTimestampMs")
    name, address = _timeline_place(location)
    title = (name or address or "Place visit") if key == "placeVisit" else _timeline_activity_label(value.get("activityType"))
    body = _timeline_summary(title, value, start, end, place=location if key == "placeVisit" else None, start_place=value.get("startLocation"), end_place=value.get("endLocation"))
    return event(title, body, f"{locator}.{key}", native_id=None, when=start, end=end, category="place_visit" if key == "placeVisit" else "travel", metadata=value, limit=limit, metadata_array_limit=None)


def google_semantic(record: dict, locator: str, limit: int) -> dict:
    visit = record.get("visit", {})
    candidate = visit.get("topCandidate", {})
    activity = record.get("activity", {})
    name, address = _timeline_place(candidate)
    top_activity = activity.get("topCandidate", {})
    title = (name or address or "Place visit") if visit else _timeline_activity_label(pick(activity, "type", default=top_activity.get("type")))
    start, end = record.get("startTime"), record.get("endTime")
    details = {**record, **(visit if visit else activity)}
    body = _timeline_summary(title, details, start, end, place=candidate if visit else None, start_place=activity.get("startLocation") or activity.get("start"), end_place=activity.get("endLocation") or activity.get("end"))
    return event(title, body, locator, when=start, end=end, category="place_visit" if visit else "travel", metadata=record, limit=limit, metadata_array_limit=None)


def tinder(record: dict, locator: str, limit: int) -> list[dict]:
    result = []
    for i, match in enumerate(record["Messages"]):
        for j, message in enumerate(match.get("messages", [])):
            result.append(event("Tinder message", message.get("message", ""), f"{locator}.Messages[{i}].messages[{j}]", native_id=pick(message, "message_id", "id"), when=message.get("sent_date"), category="message", metadata={"match_id": match.get("match_id"), **{k:v for k,v in message.items() if k != "message"}}, limit=limit))
    for kind, purchases in record.get("Purchases", {}).items():
        if not isinstance(purchases, list):
            continue
        for i, purchase in enumerate(purchases):
            result.append(event(f"Tinder {purchase.get('product_type') or kind}", document_text(purchase, limit), f"{locator}.Purchases.{kind}[{i}]", native_id=pick(purchase, "id", "transaction_id"), when=purchase.get("create_date"), end=purchase.get("expire_date"), category="purchase", metadata=purchase, limit=limit))
    for kind, dates in record.get("Usage", {}).items():
        if not isinstance(dates, dict) or kind in {"advertising_id", "idfa"}:
            continue
        for day, count in dates.items():
            if isinstance(count, (int, float)) and count:
                result.append(event(f"Tinder {kind.replace('_', ' ')}", f"{count}", f"{locator}.Usage.{kind}[{json.dumps(day)}]", when=day, category="usage", metadata={"metric": kind, "count": count}, limit=limit))
    # Preserve profile and unsupported sections as a coherent undated document.
    rest = {k:v for k,v in record.items() if k not in {"Messages", "Purchases", "Usage"}}
    if rest:
        result.append(event("Tinder profile and export context", document_text(rest, limit), locator + ".context", metadata={"context_sections": list(rest)}, limit=limit))
    return result
