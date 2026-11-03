"""Text, inert HTML, calendar, tabular and PDF extraction."""

from __future__ import annotations

import csv
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import document_text, event, pick, safe_value
from .events import EventCollection


class InertHTML(HTMLParser):
    """Extract text only. Imported markup never reaches a browser as markup."""

    ACTIVE = {"script", "style", "noscript", "iframe", "object", "svg", "template"}
    BREAKS = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "article", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.ACTIVE:
            self.hidden.append(tag)
        elif tag in self.BREAKS and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                self.hidden = self.hidden[:self.hidden.index(tag)]
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def take(self):
        text = "".join(self.parts)
        self.parts.clear()
        return text


def text_events(path: Path, source: str, limit: int, *, html: bool = False, logical_path: Path | None = None) -> Iterator[dict]:
    logical = logical_path or path
    extractor = InertHTML() if html else None
    buffer = ""
    chunk_index = 0
    metadata = {"format": "html_text" if html else logical.suffix.lower().lstrip(".") or "text"}
    if source.casefold() == "obsidian" or logical.suffix.casefold() == ".md":
        metadata["document_kind"] = "note"
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                if extractor:
                    extractor.close()
                    buffer += extractor.take()
                break
            if extractor:
                extractor.feed(chunk)
                chunk = extractor.take()
            buffer += chunk
            while len(buffer) >= limit:
                text, buffer = buffer[:limit], buffer[limit:]
                yield event(logical.stem, text, f"text:chunk:{chunk_index}", category="note" if metadata.get("document_kind") else "document", metadata={**metadata, "chunk_index": chunk_index}, limit=limit)
                chunk_index += 1
    if buffer or chunk_index == 0:
        yield event(logical.stem, buffer, f"text:chunk:{chunk_index}", category="note" if metadata.get("document_kind") else "document", metadata={**metadata, "chunk_index": chunk_index}, limit=limit)


def csv_events(path: Path, source: str, limit: int, *, logical_path: Path | None = None) -> Iterator[dict]:
    logical = logical_path or path
    csv.field_size_limit(32 * 1024 * 1024)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(65536)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel_tab if logical.suffix.lower() == ".tsv" else csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        for index, row in enumerate(reader):
            # DictReader's None key stores overflow cells. Preserve them clearly.
            record = {str(k) if k is not None else "extra_columns": v for k, v in row.items()}
            locator = f"csv:row:{index + 2}"
            if "Text Content" in record and "Date" in record:
                link = record.get("Link", "")
                match = re.search(r"/status/(\d+)", link)
                text = record["Text Content"]
                yield event(str(text)[:150], text, locator, native_id=match.group(1) if match else None, when=record["Date"], category="post", metadata=record, limit=limit)
            elif "Paid date" in record and "Title" in record:
                yield event(record["Title"], record.get("Description", ""), locator, native_id=pick(record, "Receipt ID", "Transaction ID"), when=record["Paid date"], category="purchase", metadata=record, limit=limit)
            else:
                when = pick(record, "occurred_at", "timestamp", "timestamp_ms", "created_at", "Date", "date", "Timestamp", "Timestamp (UTC)", "Date and time", "created", "sent_date", "Paid date", "create date")
                title = pick(record, "title", "Title", "name", "Name", "Subject", "Transaction Type", default=logical.stem)
                body = pick(record, "body", "text", "Text", "Content", "Description", "description", default=document_text(record, limit))
                yield event(title, body, locator, native_id=pick(record, "id", "ID", "Transaction ID"), when=when, category="record" if when else "document", metadata=record, unit="ms" if "timestamp_ms" in record else None, limit=limit)


def calendar_events(path: Path, limit: int) -> Iterator[dict]:
    try:
        from icalendar import Calendar
    except ImportError:
        yield from simple_calendar_events(path, limit)
        return
    if path.stat().st_size > 64 * 1024 * 1024:
        yield from simple_calendar_events(path, limit)
        return
    calendar = Calendar.from_ical(path.read_bytes())
    for index, component in enumerate(calendar.walk("VEVENT")):
        uid = str(component.get("UID", "")) or None
        recurrence_id = component.get("RECURRENCE-ID")
        metadata = {"uid": uid, "calendar_method": str(calendar.get("METHOD", ""))}
        for name in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID", "STATUS", "SEQUENCE", "TRANSP", "ORGANIZER", "ATTENDEE", "LOCATION", "URL", "CREATED", "LAST-MODIFIED", "DTSTAMP", "DURATION"):
            item = component.get(name)
            if item is not None:
                values = item if isinstance(item, list) else [item]
                metadata[name.casefold()] = [v.to_ical().decode("utf-8", "replace") if hasattr(v, "to_ical") else str(v) for v in values]
        start = component.get("DTSTART")
        end = component.get("DTEND")
        if start is not None:
            metadata["dtstart_parameters"] = {str(k):str(v) for k,v in start.params.items()}
        if end is not None:
            metadata["dtend_parameters"] = {str(k):str(v) for k,v in end.params.items()}
            metadata["end_is_exclusive"] = True
        if "rrule" in metadata:
            metadata["recurrence_expanded"] = False
        native_id = uid
        if uid and recurrence_id is not None:
            native_id = uid + "#" + recurrence_id.to_ical().decode("utf-8", "replace")
        description = str(component.get("DESCRIPTION", ""))
        location = str(component.get("LOCATION", ""))
        body = "\n".join(x for x in (description, location) if x)
        yield event(str(component.get("SUMMARY", "Calendar event")), body, f"ics:VEVENT:{index}", native_id=native_id, when=getattr(start, "dt", None), end=getattr(end, "dt", None), category="calendar", metadata=metadata, limit=limit)


def simple_calendar_events(path: Path, limit: int) -> Iterator[dict]:
    """Streaming fallback for conventional ICS; preserve recurrence without expansion."""
    def unfold(handle):
        current = ""
        for line in handle:
            line = line.rstrip("\r\n")
            if line.startswith((" ", "\t")):
                current += line[1:]
            else:
                if current:
                    yield current
                current = line
        if current:
            yield current

    def unescape(text):
        return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1].lower() == "n" else m[1], text)

    def parse_ics_time(prop):
        if not prop:
            return None
        params, value = prop
        if re.fullmatch(r"\d{8}", value):
            return date.fromisoformat(value)
        try:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ" if value.endswith("Z") else "%Y%m%dT%H%M%S")
        except ValueError:
            return value
        zone = "UTC" if value.endswith("Z") else params.get("TZID")
        if zone:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(zone))
            except ZoneInfoNotFoundError:
                pass
        return dt

    fields: dict[str, list[tuple[dict, str]]] | None = None
    index = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in unfold(handle):
            if line == "BEGIN:VEVENT":
                fields = {}
                continue
            if line == "END:VEVENT" and fields is not None:
                first = lambda key: fields.get(key, [({}, "")])[0]
                value = lambda key: unescape(first(key)[1])
                uid = value("UID") or None
                rid = value("RECURRENCE-ID")
                metadata = {key.casefold(): [v for _,v in values] for key,values in fields.items() if key not in {"SUMMARY", "DESCRIPTION"}}
                metadata.update({"calendar_parser": "stdlib", "recurrence_expanded": False, "end_is_exclusive": bool(fields.get("DTEND")), "dtstart_parameters": first("DTSTART")[0]})
                yield event(value("SUMMARY") or "Calendar event", "\n".join(x for x in (value("DESCRIPTION"), value("LOCATION")) if x), f"ics:VEVENT:{index}", native_id=uid + "#" + rid if uid and rid else uid, when=parse_ics_time(first("DTSTART")) if fields.get("DTSTART") else None, end=parse_ics_time(first("DTEND")) if fields.get("DTEND") else None, category="calendar", metadata=metadata, limit=limit)
                fields = None
                index += 1
                continue
            if fields is not None and ":" in line:
                label, value = line.split(":", 1)
                parts = label.split(";")
                params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
                fields.setdefault(parts[0].upper(), []).append((params, value))
    if fields is not None:
        raise ValueError("Unterminated calendar event")


def pdf_events(path: Path, limit: int, *, logical_path: Path | None = None) -> tuple[list[dict], list[str], str]:
    logical = logical_path or path
    executable = shutil.which("pdftotext")
    metadata = {"mime_type": "application/pdf", "bytes": path.stat().st_size}
    notes = []
    if not executable:
        return [event(logical.stem, "PDF retained; local text extraction is unavailable.", "pdf:document", metadata=metadata, limit=limit)], ["pdftotext unavailable; PDF retained for fallback"], "needs_fallback"
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run([executable, "-enc", "UTF-8", "-layout", str(path), "-"], stdout=output, stderr=subprocess.DEVNULL, timeout=90, check=False)
            if completed.returncode:
                raise ValueError(f"pdftotext exited {completed.returncode}")
            output.seek(0)
            events = EventCollection()
            index = 0
            # Decode each page-safe chunk independently with a text wrapper so
            # multi-byte characters are not split by a byte boundary.
            import io

            text = io.TextIOWrapper(output, encoding="utf-8", errors="replace")
            while True:
                chunk = text.read(limit)
                if not chunk:
                    break
                if chunk.strip():
                    events.append(event(logical.stem, chunk, f"pdf:text:chunk:{index}", metadata={**metadata, "chunk_index": index}, limit=limit))
                index += 1
            text.detach()
        if events:
            return events, notes, "parsed"
        return [event(logical.stem, "PDF retained; no extractable text was found.", "pdf:document", metadata=metadata, limit=limit)], ["No PDF text; image/scanned pages may require OCR"], "needs_fallback"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return [event(logical.stem, "PDF retained; text extraction requires fallback.", "pdf:document", metadata=metadata, limit=limit)], [f"PDF extraction failed: {type(exc).__name__}"], "needs_fallback"


def media_event(path: Path, limit: int, *, logical_path: Path | None = None) -> dict:
    logical = logical_path or path
    mime = mimetypes.guess_type(logical.name)[0] or "application/octet-stream"
    return event(logical.stem, f"Retained source file: {logical.name}\nMedia type: {mime}", "file:metadata", category="media" if mime.startswith(("image/", "audio/", "video/")) else "document", metadata={"mime_type": mime, "bytes": path.stat().st_size, "content_extraction": "not_performed"}, limit=limit)
