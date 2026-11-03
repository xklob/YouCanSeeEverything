"""Local deterministic dispatch, explicit failures, and provenance-friendly results."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from . import ParseResult
from .common import event
from .json_records import adapt, json_items
from .text_formats import calendar_events, csv_events, media_event, pdf_events, text_events
from .extended_formats import activity_events, office_text_events, rtf_text_events, xml_text_events


CREDENTIAL_FILE = re.compile(
    r"(?:^|[/\\])(?:\.env(?:\.[^/\\]+)?|id_(?:rsa|ed25519|dsa)(?:\.pub)?|"
    r"(?:saved[_ -]?)?credit[_ -]?cards?(?:\.[^/\\]+)?|cookies?(?:\.[^/\\]+)?|"
    r"credentials?(?:\.[^/\\]+)?|(?:access[_ -]?|refresh[_ -]?)?tokens?(?:\.[^/\\]+)?|"
    r"login data|key[ -]?material|private[_ -]?keys?(?:\.[^/\\]+)?)$", re.I
)


def parse(path: Path, original_path: str, source: str, *, max_text_chars: int, excluded_directory_names=()) -> ParseResult:
    if max_text_chars < 100:
        raise ValueError("max_text_chars must be at least 100")
    excluded = {str(name).casefold() for name in excluded_directory_names}
    if any(part.casefold() in excluded for part in re.split(r"[/\\]", original_path)):
        return ParseResult(parser="policy.excluded", status="excluded", notes=["Directory excluded by configured source policy"])
    if CREDENTIAL_FILE.search(original_path) or CREDENTIAL_FILE.search(path.name):
        return ParseResult(parser="policy.credentials", status="excluded", notes=["Credential material is excluded from history"])
    if path.is_symlink():
        return ParseResult(parser="policy.symlink", status="excluded", notes=["Symlink requires explicit source inventory handling"])
    logical_path = Path(original_path.replace("\\", "/"))
    suffix = logical_path.suffix.casefold()
    result = ParseResult(parser="unknown", status="parsed")
    try:
        if suffix == ".json":
            names = set()
            for locator, record in json_items(path):
                parser, events = adapt(record, locator, source, original_path, max_text_chars)
                names.add(parser)
                result.events.extend(events)
            result.parser = "+".join(sorted(names)) or "json.empty"
        elif suffix in {".jsonl", ".ndjson"}:
            result.parser = "jsonl.records"
            bad_lines = []
            with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        _, events = adapt(record, f"jsonl:line:{line_number}", source, original_path, max_text_chars)
                        result.events.extend(events)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        bad_lines.append(line_number)
            if bad_lines:
                result.status = "partial" if result.events else "needs_fallback"
                result.notes.append(f"Invalid JSONL records at lines {bad_lines[:50]}; source retained")
        elif suffix in {".csv", ".tsv"}:
            result.parser = "csv.records"
            result.events.extend(csv_events(path, source, max_text_chars, logical_path=logical_path))
        elif suffix == ".ics":
            result.parser = "ics.events"
            result.events.extend(calendar_events(path, max_text_chars))
            if result.events.has_recurrence:
                result.notes.append("Recurring calendar series preserved as definitions; occurrences were not expanded")
        elif suffix in {".html", ".htm"}:
            result.parser = "html.text"
            result.events.extend(text_events(path, source, max_text_chars, html=True, logical_path=logical_path))
        elif suffix in {".md", ".markdown", ".txt", ".log", ".rst", ".text"}:
            result.parser = "text.document"
            result.events.extend(text_events(path, source, max_text_chars, logical_path=logical_path))
        elif suffix == ".pdf":
            result.parser = "pdf.pdftotext"
            events, result.notes, result.status = pdf_events(path, max_text_chars, logical_path=logical_path)
            result.events.extend(events)
        elif suffix in {".tcx", ".gpx"}:
            result.parser = "xml.activity"
            result.events.extend(activity_events(path, logical_path, max_text_chars))
        elif suffix in {".xml", ".atom", ".opml"}:
            result.parser = "xml.text"
            result.events.extend(xml_text_events(path, logical_path, max_text_chars))
        elif suffix in {".docx", ".odt"}:
            result.parser = "office.text"
            result.events.extend(office_text_events(path, logical_path, max_text_chars))
        elif suffix == ".rtf":
            result.parser = "rtf.text"
            result.events.extend(rtf_text_events(path, logical_path, max_text_chars))
        else:
            result.parser = "file.metadata"
            result.status = "metadata_only"
            result.events.append(media_event(path, max_text_chars, logical_path=logical_path))
            result.notes.append("Original file retained; deterministic content extractor unavailable")
        if not result.events and result.status == "parsed":
            result.status = "empty"
        if result.events.has_truncated_body:
            result.notes.append("Some record text exceeds the extraction limit; full content retained in source")
        if result.events.has_truncated_document:
            result.notes.append("Some structured documents exceed the extraction limit; full content retained in source")
        if result.events.has_local_datetime:
            result.notes.append("Local datetimes without a timezone use calendar day buckets; exact local times are preserved in metadata without inventing UTC instants")
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, ET.ParseError, zipfile.BadZipFile) as exc:
        result.status = "partial" if result.events else "needs_fallback"
        result.parser = result.parser if result.parser != "unknown" else suffix.lstrip(".") + ".failed"
        # Exception text may contain the source message body. Report type only.
        result.notes.append(f"Deterministic extraction failed ({type(exc).__name__}); original file retained for fallback")
    return result
