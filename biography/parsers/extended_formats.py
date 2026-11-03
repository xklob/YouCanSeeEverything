"""Streaming activity summaries and inert Office document text."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Iterator

from .common import event


def tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(element, name):
    return next((child.text for child in element if tag_name(child.tag) == name and child.text), None)


def number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def activity_events(path: Path, logical: Path, limit: int) -> Iterator[dict]:
    """Emit tracks/activities, never a separate event for every GPS sample."""
    activity = None
    track = None
    activity_index = 0
    track_index = 0
    for action, element in ET.iterparse(path, events=("start", "end")):
        tag = tag_name(element.tag)
        if action == "start":
            if tag == "Activity":
                activity = {"sport": element.get("Sport"), "laps": [], "trackpoint_count": 0}
            elif tag == "trk":
                track = {"trackpoint_count": 0, "distance_meters_computed": 0.0, "bounds": {}}
            continue
        if activity is not None:
            if tag == "Id" and not activity.get("source_activity_id"):
                activity["source_activity_id"] = element.text
            elif tag == "Trackpoint":
                stamp = child_text(element, "Time")
                if stamp:
                    activity.setdefault("first_trackpoint_time", stamp)
                    activity["last_trackpoint_time"] = stamp
                activity["trackpoint_count"] += 1
                element.clear()
            elif tag == "Lap":
                activity["laps"].append({"start_time": element.get("StartTime"), "duration_seconds": number(child_text(element, "TotalTimeSeconds")), "distance_meters": number(child_text(element, "DistanceMeters")), "calories": number(child_text(element, "Calories"))})
                element.clear()
            elif tag == "Activity":
                start = activity.get("source_activity_id") or activity.get("first_trackpoint_time") or next((lap["start_time"] for lap in activity["laps"] if lap.get("start_time")), None)
                duration = sum(lap.get("duration_seconds") or 0 for lap in activity["laps"])
                distance = sum(lap.get("distance_meters") or 0 for lap in activity["laps"])
                sport = activity.get("sport") or "Fitness"
                body = f"{sport} activity\nDuration: {duration:g} seconds\nDistance: {distance:g} meters"
                yield event(f"{sport} activity", body, f"xml:Activity:{activity_index}", native_id=activity.get("source_activity_id"), when=start, end=activity.get("last_trackpoint_time"), category="activity", metadata=activity, limit=limit)
                activity_index += 1
                activity = None
                element.clear()
        if track is not None:
            if tag in {"name", "desc", "type"} and element.text:
                track.setdefault(tag, element.text)
            elif tag == "trkpt":
                latitude, longitude = number(element.get("lat")), number(element.get("lon"))
                stamp = child_text(element, "time")
                if stamp:
                    track.setdefault("first_point_time", stamp)
                    track["last_point_time"] = stamp
                if latitude is not None and longitude is not None:
                    bounds = track["bounds"]
                    for key, value, fn in (("min_latitude", latitude, min), ("max_latitude", latitude, max), ("min_longitude", longitude, min), ("max_longitude", longitude, max)):
                        bounds[key] = fn(bounds.get(key, value), value)
                    previous = track.get("last_coordinates")
                    if previous:
                        lat1, lon1 = map(math.radians, previous)
                        lat2, lon2 = map(math.radians, (latitude, longitude))
                        haversine = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
                        track["distance_meters_computed"] += 6371000 * 2 * math.asin(min(1, math.sqrt(haversine)))
                    track.setdefault("first_coordinates", [latitude, longitude])
                    track["last_coordinates"] = [latitude, longitude]
                track["trackpoint_count"] += 1
                element.clear()
            elif tag == "trkseg":
                # Segment discontinuities are not traveled straight-line distance.
                track.pop("last_coordinates", None)
                element.clear()
            elif tag == "trk":
                title = track.get("name") or logical.stem
                body = f"{track.get('desc') or title}\nGPS samples: {track['trackpoint_count']}\nComputed track distance: {track['distance_meters_computed']:.1f} meters"
                yield event(title, body, f"xml:trk:{track_index}", when=track.get("first_point_time"), end=track.get("last_point_time"), category="activity", metadata=track, limit=limit)
                track_index += 1
                track = None
                element.clear()
    if not activity_index and not track_index:
        yield from xml_text_events(path, logical, limit)


def xml_text_events(path: Path, logical: Path, limit: int) -> Iterator[dict]:
    buffer = ""
    index = 0
    for _, element in ET.iterparse(path, events=("end",)):
        if element.text and element.text.strip():
            buffer += f"{tag_name(element.tag)}: {element.text.strip()}\n"
        while len(buffer) >= limit:
            yield event(logical.stem, buffer[:limit], f"xml:text:chunk:{index}", metadata={"format": "xml", "chunk_index": index}, limit=limit)
            buffer = buffer[limit:]
            index += 1
        element.clear()
    if buffer or not index:
        yield event(logical.stem, buffer, f"xml:text:chunk:{index}", metadata={"format": "xml", "chunk_index": index}, limit=limit)


def office_text_events(path: Path, logical: Path, limit: int) -> Iterator[dict]:
    document = "word/document.xml" if logical.suffix.casefold() == ".docx" else "content.xml"
    buffer = ""
    index = 0
    with zipfile.ZipFile(path) as archive:
        with archive.open(document) as handle:
            for _, element in ET.iterparse(handle, events=("end",)):
                if tag_name(element.tag) in {"p", "h"}:
                    buffer += "".join(element.itertext()) + "\n"
                    element.clear()
                    while len(buffer) >= limit:
                        yield event(logical.stem, buffer[:limit], f"office:text:chunk:{index}", metadata={"format": logical.suffix[1:], "chunk_index": index}, limit=limit)
                        index += 1
                        buffer = buffer[limit:]
    if buffer or not index:
        yield event(logical.stem, buffer, f"office:text:chunk:{index}", metadata={"format": logical.suffix[1:], "chunk_index": index}, limit=limit)


def rtf_text_events(path: Path, logical: Path, limit: int) -> Iterator[dict]:
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Large RTF needs streaming fallback")
    source = path.read_text(encoding="latin-1")
    tokens = re.finditer(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)", source)
    skip = False
    unicode_skip = 1
    skip_chars = 0
    stack = []
    buffer = ""
    index = 0
    for match in tokens:
        word, argument, hexadecimal, escaped, brace, text = match.groups()
        if brace == "{":
            stack.append((skip, unicode_skip))
        elif brace == "}":
            if stack:
                skip, unicode_skip = stack.pop()
        elif escaped == "*":
            skip = True
        elif word in {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "datastore", "themedata", "xmlnstbl"}:
            skip = True
        elif not skip:
            if word == "uc" and argument is not None:
                unicode_skip = int(argument)
            elif word == "u" and argument is not None:
                codepoint = int(argument)
                buffer += chr(codepoint if codepoint >= 0 else codepoint + 65536)
                skip_chars = unicode_skip
            elif word in {"par", "line", "tab"}:
                buffer += "\t" if word == "tab" else "\n"
            elif hexadecimal:
                if skip_chars:
                    skip_chars -= 1
                else:
                    buffer += bytes.fromhex(hexadecimal).decode("cp1252", "replace")
            elif escaped in {"{", "}", "\\"}:
                buffer += escaped
            elif text:
                discard = min(skip_chars, len(text))
                skip_chars -= discard
                buffer += text[discard:]
        while len(buffer) >= limit:
            yield event(logical.stem, buffer[:limit], f"rtf:text:chunk:{index}", metadata={"format": "rtf", "chunk_index": index}, limit=limit)
            index += 1
            buffer = buffer[limit:]
    if buffer or not index:
        # RTF Unicode can express a UTF-16 surrogate pair as two \u controls.
        buffer = buffer.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        yield event(logical.stem, buffer, f"rtf:text:chunk:{index}", metadata={"format": "rtf", "chunk_index": index}, limit=limit)
