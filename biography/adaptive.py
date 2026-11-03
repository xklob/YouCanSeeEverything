"""Source-grounded fallback using cached, declarative Codex suggestions only.

The model never receives a source path it can open and never supplies executable
parser code. A mapping reads source values locally; text suggestions must quote
the supplied sanitized source verbatim. Failure preserves the original result.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Iterator

from .agents import AgentError, adapt as suggest_adapter
from .config import settings
from .parsers import ParseResult
from .parsers.common import SECRET_KEY, clean_text, document_text, event
from .parsers.engine import CREDENTIAL_FILE


TEXT_FILE_LIMIT = 512 * 1024
TEXT_SAMPLE_LIMIT = 40_000
STRUCTURED_FILE_LIMIT = 512 * 1024 * 1024
RECORD_LIMIT = 250_000
RECORD_BYTE_LIMIT = 1024 * 1024
PARSE_SECONDS = 45
PROFILE_LIMIT = 52_000
_MISSING = object()
_FIELDS = ("id", "title", "body", "occurred_at", "end_at", "category")
_PATH = re.compile(r"[A-Za-z0-9_@$-]+(?:\.[A-Za-z0-9_@$-]+)*")
_CREDENTIAL_HEADER = re.compile(r"(?im)^(\s*(?:cookie|set-cookie|authorization|proxy-authorization|access[ _-]?token|refresh[ _-]?token)\s*[:=]).*$")
_QUOTED_ASSIGNMENT = re.compile(r'''(["']([^"'\r\n]{1,100})["']\s*:\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,;}\r\n]+)''')


class AdaptationUnavailable(ValueError):
    """A bounded adaptation could not cover this source safely."""


def _sanitized_text(value: str, limit: int) -> str:
    value = _CREDENTIAL_HEADER.sub(r"\1 [REDACTED CREDENTIAL]", value)
    value = _QUOTED_ASSIGNMENT.sub(lambda match: match[1] + '"[REDACTED CREDENTIAL]"' if SECRET_KEY.search(match[2]) else match[0], value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED CREDENTIAL]", value)
    return clean_text(value, limit)


class _JSONReader:
    """Stop an oversized scalar before the streaming parser materializes it."""

    def __init__(self, handle):
        self.handle, self.since_event = handle, 0

    def read(self, size=-1):
        data = self.handle.read(min(size, 65_536) if size >= 0 else 65_536)
        self.since_event += len(data)
        if self.since_event > RECORD_BYTE_LIMIT + 65_536:
            raise AdaptationUnavailable("A JSON scalar exceeded the bounded record size")
        return data

    def readinto(self, target):
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)


class _CSVLines:
    def __init__(self, handle):
        self.handle, self.since_record = handle, 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.handle.readline(RECORD_BYTE_LIMIT + 1)
        if not line:
            raise StopIteration
        self.since_record += len(line.encode())
        if self.since_record > RECORD_BYTE_LIMIT:
            raise AdaptationUnavailable("A CSV record exceeded the bounded record size")
        return line


def _retained(baseline: ParseResult, note: str) -> ParseResult:
    return ParseResult(events=baseline.events, parser=baseline.parser, status="needs_fallback", notes=[*baseline.notes, note])


def _work_root(work_dir: Path) -> Path:
    directory = Path(work_dir).resolve()
    if ".local" not in directory.parts:
        raise AdaptationUnavailable("Adaptive artifacts must be stored under .local")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _check_elapsed(started: float) -> None:
    if time.monotonic() - started > PARSE_SECONDS:
        raise AdaptationUnavailable("Structured source exceeded the local adaptation time budget")


def _json_layout(path: Path) -> tuple[str, bool]:
    """Find the first collection without loading its records or a large object."""
    import ijson

    started = time.monotonic()
    with path.open("rb") as handle:
        reader = _JSONReader(handle)
        for index, (prefix, kind, value) in enumerate(ijson.parse(reader, use_float=True)):
            reader.since_event = 0
            if index % 256 == 0:
                _check_elapsed(started)
                if handle.tell() > TEXT_FILE_LIMIT:
                    raise AdaptationUnavailable("No JSON record collection was found within the bounded structural scan")
            if kind == "start_array":
                # This is the first array, so its path contains no array traversal.
                if prefix and not _PATH.fullmatch(prefix):
                    raise AdaptationUnavailable("JSON collection keys require unsupported path notation")
                if any(SECRET_KEY.search(part) for part in prefix.split(".")):
                    raise AdaptationUnavailable("Credential collections cannot be sent for adaptive extraction")
                return prefix, True
            if kind == "map_key" and "." in value:
                raise AdaptationUnavailable("Literal dotted JSON keys require unsupported path notation")
    return "", False


def _json_records(path: Path, record_path: str, collection: bool) -> Iterator[tuple[str, Any]]:
    """Use a bounded record builder while validating the complete JSON stream."""
    import ijson

    prefix = ((record_path + ".") if record_path else "") + "item" if collection else record_path
    started, building, depth, cost, count = time.monotonic(), None, 0, 0, 0
    with path.open("rb") as handle:
        reader = _JSONReader(handle)
        for step, (current, kind, value) in enumerate(ijson.parse(reader, use_float=True)):
            reader.since_event = 0
            if step % 256 == 0:
                _check_elapsed(started)
            if building is None:
                if current != prefix or kind in {"end_map", "end_array", "map_key"}:
                    continue
                if count >= RECORD_LIMIT:
                    raise AdaptationUnavailable("JSON collection exceeded the record adaptation budget")
                building, depth, cost = ijson.ObjectBuilder(), 0, 0
            cost += len(value.encode("utf-8")) if isinstance(value, str) else len(str(value)) + 16
            if cost > RECORD_BYTE_LIMIT:
                raise AdaptationUnavailable("A JSON record exceeded the bounded record size")
            building.event(kind, value)
            if kind in {"start_array", "start_map"}:
                depth += 1
            elif kind in {"end_array", "end_map"}:
                depth -= 1
            if depth == 0:
                locator = f"$.{record_path}[{count}]" if record_path else f"$[{count}]" if collection else "$"
                yield locator, building.value
                count += 1
                building = None


def _csv_rows(path: Path, suffix: str) -> Iterator[tuple[str, dict]]:
    started = time.monotonic()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8_192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel_tab if suffix == ".tsv" else csv.excel
        lines = _CSVLines(handle)
        reader = csv.DictReader(lines, dialect=dialect)
        headers = reader.fieldnames or []
        if not headers or len(headers) > 200 or len(set(headers)) != len(headers):
            raise AdaptationUnavailable("CSV requires a bounded set of unique column names")
        for index, row in enumerate(reader):
            lines.since_record = 0
            if index >= RECORD_LIMIT:
                raise AdaptationUnavailable("CSV exceeded the record adaptation budget")
            if index % 256 == 0:
                _check_elapsed(started)
            if None in row or any(value is None for value in row.values()):
                raise AdaptationUnavailable("CSV has rows that do not match its column structure")
            if sum(len(key.encode()) + len(value.encode()) for key, value in row.items()) > RECORD_BYTE_LIMIT:
                raise AdaptationUnavailable("A CSV record exceeded the bounded record size")
            yield f"csv:row:{index + 2}", row


def _sample_value(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[nested data retained in source]"
    if isinstance(value, dict):
        return {clean_text(key, 200): "[REDACTED CREDENTIAL]" if SECRET_KEY.search(str(key)) else _sample_value(item, depth + 1) for key, item in list(value.items())[:40]}
    if isinstance(value, list):
        return [_sample_value(item, depth + 1) for item in value[:3]]
    return _sanitized_text(value, 700) if isinstance(value, str) else value


def _structure(samples: list) -> list[dict]:
    fields: dict[str, set[str]] = {}

    def visit(value: Any, prefix: str, depth: int = 0):
        if len(fields) >= 120 or depth > 5:
            return
        kind = "null" if value is None else "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "string"
        fields.setdefault(prefix[:300], set()).add(kind)
        if isinstance(value, dict):
            for key, item in list(value.items())[:40]:
                if not SECRET_KEY.search(str(key)):
                    visit(item, (prefix + "." if prefix else "") + str(key), depth + 1)
        elif isinstance(value, list):
            for item in value[:2]:
                visit(item, prefix + "[]", depth + 1)

    for sample in samples:
        visit(sample, "")
    return [{"path": path, "types": sorted(types)} for path, types in sorted(fields.items())]


def _profile_structured(path: Path, suffix: str, source: str) -> tuple[dict, dict]:
    if path.stat().st_size > STRUCTURED_FILE_LIMIT:
        raise AdaptationUnavailable("Structured source exceeds the 512 MiB adaptation file budget")
    format_name = "csv" if suffix in {".csv", ".tsv"} else "json"
    record_path, collection = _json_layout(path) if format_name == "json" else ("", True)
    rows = _json_records(path, record_path, collection) if format_name == "json" else _csv_rows(path, suffix)
    samples, total = [], 0
    # Consume to EOF before calling the model. A malformed large JSON file must
    # not be treated as a valid streamed collection just because its head parses.
    for _, item in rows:
        if len(samples) < 3:
            samples.append(item)
        total += 1
    if not samples:
        raise AdaptationUnavailable("The selected source collection contains no records")
    structural = {"format": format_name, "record_path": record_path, "collection": collection, "schema": _structure(samples)}
    safe_samples = [_sample_value(item) for item in samples]
    if format_name == "json":
        sample: Any = safe_samples if collection else safe_samples[0]
        for part in reversed(record_path.split(".") if record_path else []):
            sample = {part: sample}
    else:
        sample = safe_samples
    profile = {"source": clean_text(source, 200), **structural, "record_count": total, "sample": sample, "sample_is_partial": total > len(samples)}
    encoded = json.dumps(profile, ensure_ascii=False)
    if len(encoded) > PROFILE_LIMIT:
        raise AdaptationUnavailable("The structural sample exceeds the bounded model profile budget")
    return profile, structural


def _lookup(record: Any, path: str, format_name: str) -> Any:
    if not path:
        return _MISSING
    parts = [path] if format_name == "csv" else path.split(".")
    value = record
    for part in parts:
        if SECRET_KEY.search(part):
            raise AdaptationUnavailable("Credential fields cannot become timeline events")
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _mapping(value: dict, profile: dict) -> dict:
    if not isinstance(value, dict) or value.get("kind") != "mapping" or value.get("format") != profile["format"]:
        raise AdaptationUnavailable("Codex did not propose a compatible declarative mapping")
    fields = value.get("fields")
    if not isinstance(fields, dict) or set(fields) != set(_FIELDS) or any(not isinstance(item, str) or len(item) > 300 for item in fields.values()):
        raise AdaptationUnavailable("Codex proposed an invalid field mapping")
    if value.get("record_path") != profile["record_path"]:
        raise AdaptationUnavailable("The suggested record path differs from the validated source collection")
    if value.get("events"):
        raise AdaptationUnavailable("A declarative mapping cannot include generated event values")
    for field_path in fields.values():
        if profile["format"] == "json" and field_path and not _PATH.fullmatch(field_path):
            raise AdaptationUnavailable("Only simple JSON field paths are supported")
        parts = field_path.split(".") if profile["format"] == "json" else [field_path]
        if any(SECRET_KEY.search(part) for part in parts):
            raise AdaptationUnavailable("Credential fields cannot become timeline events")
    if not fields["title"] and not fields["body"]:
        raise AdaptationUnavailable("A timeline mapping requires a title or text field")
    return {"kind": "mapping", "format": profile["format"], "record_path": profile["record_path"], "fields": fields, "events": [], "notes": clean_text(value.get("notes"), 2_000), "model": value.get("model", "gpt-6.1-sol")}


def _mapped_events(path: Path, suffix: str, original_path: str, mapping: dict, structural: dict, cache_key: str, cached: bool) -> ParseResult:
    rows = _json_records(path, structural["record_path"], structural["collection"]) if mapping["format"] == "json" else _csv_rows(path, suffix)
    result = ParseResult(parser="codex.declarative.v1", status="parsed")
    try:
        for locator, record in rows:
            if not isinstance(record, dict):
                raise AdaptationUnavailable("A mapped source record is not an object")
            values = {key: _lookup(record, field_path, mapping["format"]) for key, field_path in mapping["fields"].items()}
            if any(isinstance(value, (dict, list)) for key, value in values.items() if key != "body"):
                raise AdaptationUnavailable("Mapped titles, identifiers, dates and categories must be scalar source values")
            title, body = values["title"], values["body"]
            if title in (_MISSING, None, "") and body in (_MISSING, None, ""):
                raise AdaptationUnavailable("A mapped record has neither the proposed title nor text field")
            if not isinstance(body, str):
                body = document_text(record if body is _MISSING else body, 200_000)
            metadata = {"adaptive": {"kind": "declarative_mapping", "model": mapping["model"], "schema_sha256": cache_key, "cached_mapping": cached, "mapping": mapping["fields"], "source_path": clean_text(original_path, 500)}}
            result.events.append(event(title if title not in (_MISSING, None, "") else Path(original_path).stem, body, locator, native_id=None if values["id"] is _MISSING else values["id"], when=None if values["occurred_at"] is _MISSING else values["occurred_at"], end=None if values["end_at"] is _MISSING else values["end_at"], category=clean_text(values["category"], 100) if values["category"] not in (_MISSING, None, "") else "record", metadata=metadata))
        if not result.events:
            raise AdaptationUnavailable("The declarative mapping extracted no source records")
        return result
    except Exception:
        result.events.close()
        raise


def _structured(path: Path, original_path: str, source: str, suffix: str, baseline: ParseResult, work: Path) -> ParseResult:
    profile, structural = _profile_structured(path, suffix, source)
    cache_key = hashlib.sha256(json.dumps({"version": 1, "source": source, **structural}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cache = work / "adapter-cache"
    cache.mkdir(exist_ok=True, mode=0o700)
    cached_path = cache / (cache_key + ".json")
    with (cache / (cache_key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = False
        if cached_path.is_file():
            try:
                proposed = json.loads(cached_path.read_text(encoding="utf-8"))
                mapping = _mapping(proposed, profile)
                cached = True
            except (ValueError, TypeError, KeyError):
                proposed = suggest_adapter(profile, work / "jobs")
                mapping = _mapping(proposed, profile)
        else:
            proposed = suggest_adapter(profile, work / "jobs")
            mapping = _mapping(proposed, profile)
        result = _mapped_events(path, suffix, original_path, mapping, structural, cache_key, cached)
        if not cached:
            cached_path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
            cached_path.chmod(0o600)
    result.notes = [*baseline.notes, "Codex suggested a declarative field mapping; values and dates were read locally from the complete selected collection.", f"Mapping {cache_key[:16]} ({'cached' if cached else 'new'}); original source retained."]
    if result.events.has_local_datetime:
        result.notes.append("Local datetimes without a timezone were preserved without inventing UTC occurrence times.")
    return result


def _text(path: Path, source: str, original_path: str, baseline: ParseResult, work: Path) -> ParseResult:
    if path.stat().st_size > TEXT_FILE_LIMIT:
        raise AdaptationUnavailable("Text or malformed JSON exceeds the 512 KiB automatic adaptation limit")
    raw = path.read_bytes()
    if b"\x00" in raw or raw.startswith((b"%PDF", b"PK\x03\x04", b"\x7fELF", b"MZ")):
        raise AdaptationUnavailable("This source is binary; a local text extractor is required")
    text = _sanitized_text(raw.decode("utf-8-sig"), TEXT_FILE_LIMIT)
    if not text.strip():
        raise AdaptationUnavailable("The source has no nonempty text to adapt")
    if len(text) > TEXT_SAMPLE_LIMIT:
        raise AdaptationUnavailable("Text exceeds the bounded model sample; baseline retained without claiming complete AI extraction")
    proposed = suggest_adapter({"format": "text", "source": clean_text(source, 200), "sample": text, "sample_is_partial": False}, work / "jobs")
    if not isinstance(proposed, dict) or proposed.get("kind") != "events" or proposed.get("format") != "text" or not isinstance(proposed.get("events"), list) or not 1 <= len(proposed["events"]) <= 100:
        raise AdaptationUnavailable("Codex could not safely extract source-grounded events")
    result = ParseResult(parser="codex.quoted-events.v1", status="parsed")
    dropped_dates = 0
    seen = set()
    try:
        for candidate in proposed["events"]:
            if not isinstance(candidate, dict):
                raise AdaptationUnavailable("The suggested text event is not a structured object")
            body = candidate.get("body")
            if not isinstance(body, str) or not body.strip() or body not in text:
                raise AdaptationUnavailable("An AI event did not quote the supplied source verbatim")
            if body in seen:
                continue
            seen.add(body)
            start = text.index(body)
            dates = {}
            for field in ("occurred_at", "end_at"):
                date = candidate.get(field) or ""
                dates[field] = date if isinstance(date, str) and date and date in body else None
                dropped_dates += bool(date and dates[field] is None)
            title = candidate.get("title")
            title = title if isinstance(title, str) and title.strip() and title in body else body.strip().splitlines()[0][:120]
            result.events.append(event(title, body, f"ai:sample:chars:{start}-{start + len(body)}", when=dates["occurred_at"], end=dates["end_at"], category=clean_text(candidate.get("category"), 100) or "record", metadata={"ai_inferred": True, "adaptive": {"kind": "quoted_text_events", "model": proposed.get("model", "gpt-6.1-sol"), "evidence_sha256": hashlib.sha256(body.encode()).hexdigest(), "evidence_is_verbatim": True, "locator_basis": "sanitized bounded UTF-8 source sample", "source_path": clean_text(original_path, 500)}}))
        result.notes = [*baseline.notes, "AI-inferred event selection and labels; event bodies are verified verbatim source excerpts. Original source retained."]
        if dropped_dates:
            result.notes.append(f"Discarded {dropped_dates} AI-suggested date values that were not quoted in their event evidence.")
        return result
    except Exception:
        result.events.close()
        raise


def adapt_file(path: Path, original_path: str, source: str, baseline: ParseResult, work_dir: Path, *, excluded_directory_names: tuple[str, ...] | None = None) -> ParseResult:
    """Adapt one retained file, preserving baseline events on any safe failure.

    Called automatically for ``needs_fallback`` by the ingestion pipeline. It can
    also be invoked explicitly for generic JSON/CSV that already parsed locally.
    Model access and cache artifacts are confined to the supplied .local workdir.
    """
    path = Path(path)
    excluded = {str(name).casefold() for name in (excluded_directory_names if excluded_directory_names is not None else getattr(settings, "excluded_directory_names", ()))}
    if baseline.status == "excluded" or CREDENTIAL_FILE.search(original_path) or CREDENTIAL_FILE.search(path.name) or path.is_symlink() or any(part.casefold() in excluded for part in re.split(r"[/\\]", original_path)):
        return baseline
    suffix = Path(original_path.replace("\\", "/")).suffix.casefold()
    try:
        work = _work_root(work_dir)
        if suffix in {".json", ".csv", ".tsv"}:
            try:
                return _structured(path, original_path, source, suffix, baseline, work)
            except Exception as exc:
                # Large malformed JSON must not be sent merely because its head
                # looked valid. Small malformed JSON may be treated as inert text.
                try:
                    import ijson
                    malformed = isinstance(exc, (ijson.JSONError, json.JSONDecodeError))
                except ImportError:
                    malformed = False
                if suffix == ".json" and malformed:
                    return _text(path, source, original_path, baseline, work)
                raise
        return _text(path, source, original_path, baseline, work)
    except (AdaptationUnavailable, AgentError, OSError, UnicodeError, ValueError, TypeError, KeyError, ImportError, OverflowError, csv.Error) as exc:
        reason = str(exc) if isinstance(exc, (AdaptationUnavailable, AgentError)) else type(exc).__name__
        return _retained(baseline, f"Adaptive extraction needs fallback: {clean_text(reason, 300)}. Baseline and original source retained.")
