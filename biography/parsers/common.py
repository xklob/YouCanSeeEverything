"""Shared safe normalization. Filesystem timestamps are never event dates."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote, unquote

UTC = timezone.utc
SECRET_KEY = re.compile(
    r"(?:password|passwd|passphrase|private[ _-]?key|secret|access[ _-]?token|"
    r"refresh[ _-]?token|api[ _-]?key|authorization|cookie|session[ _-]?token|"
    r"recovery[ _-]?(?:phrase|code)|seed[ _-]?phrase|mnemonic|credit[ _-]?card|card[ _-]?number|cvv)",
    re.I,
)
SECRET_VALUE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"
)
SECRET_ASSIGNMENT = re.compile(
    r"(?im)(\b(?:[A-Za-z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|"
    r"PASSPHRASE|PRIVATE_KEY|SECRET_KEY|SEED_PHRASE|MNEMONIC)[A-Za-z0-9_]*|"
    r"password|passphrase|api[_ -]?key|private[_ -]?key|mnemonic)\s*[:=]\s*)"
    r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;}\n]+)"
)
PRIVATE_PEM = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z]+ )?PRIVATE KEY-----", re.S)
_UUID_VALUE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_AUTH_QUERY_UUID = re.compile(
    r"(?P<prefix>(?:^|[?&;])(?:token|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"id[_-]?token|session[_-]?token|api[_-]?key|apikey|authorization|auth|secret|"
    r"password|passwd|session(?:[_-]?id)?|jwt|key)=)(?P<value>" + _UUID_VALUE + r")(?![\w-])",
    re.I,
)
_QUERY_SEGMENT = re.compile(r'''[^\s"'<>`]+''')
_PERCENT_BYTE = re.compile(r"%([0-9a-f]{2})", re.I)
_ENCODED_QUERY_DELIMITER = re.compile(r"%(?:25)*(?:3f|26|3b|3d)", re.I)


def redact_auth_query_values(text: str, *, max_decode_depth: int = 8) -> str:
    """Redact UUID-valued auth query parameters without truncating the body.

    Plain and nested percent-encoded URL views are inspected, then only matched
    value spans are replaced in the original text. All other spelling/encoding,
    ordinary UUID prose, and public token contract addresses remain unchanged.
    No URLs are requested and decoded views are never persisted.
    """
    if "=" not in text and ("%" not in text or not _ENCODED_QUERY_DELIMITER.search(text)):
        return text
    replacements = {}
    for segment in _QUERY_SEGMENT.finditer(text):
        view = segment[0]
        if not _AUTH_QUERY_UUID.search(view) and not _ENCODED_QUERY_DELIMITER.search(view):
            continue
        spans = [(index, index + 1) for index in range(len(view))]
        for depth in range(max_decode_depth + 1):
            for match in _AUTH_QUERY_UUID.finditer(view):
                start, end = match.span("value")
                original = (segment.start() + spans[start][0], segment.start() + spans[end - 1][1])
                if original not in replacements:
                    marker = "[REDACTED CREDENTIAL]"
                    for _ in range(depth):
                        marker = quote(marker, safe="")
                    replacements[original] = marker
            if depth == max_decode_depth or not _PERCENT_BYTE.search(view):
                break
            decoded = []
            decoded_spans = []
            index = 0
            while index < len(view):
                match = _PERCENT_BYTE.match(view, index)
                if match:
                    decoded.append(chr(int(match[1], 16)))
                    decoded_spans.append((spans[index][0], spans[match.end() - 1][1]))
                    index = match.end()
                else:
                    decoded.append(view[index])
                    decoded_spans.append(spans[index])
                    index += 1
            view, spans = "".join(decoded), decoded_spans
    for (start, end), marker in sorted(replacements.items(), reverse=True):
        text = text[:start] + marker + text[end:]
    return text


def preserve_public_token_query(match: re.Match) -> bool:
    """A token contract address in a query is public data, not an auth secret."""
    if "token" not in match[1].casefold():
        return False
    preceding = match.string[:match.start()]
    if preceding and preceding[-1] not in "?&;" and not re.search(r"%(?:25)*(?:3f|26|3b)$", preceding, re.I):
        return False
    value = match[0][len(match[1]):].strip()
    for _ in range(8):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    value = value.strip("\"'").split("#", 1)[0].rstrip(")]}.:,")
    return bool(re.fullmatch(r"0x[0-9a-f]{40}", value, re.I))


def clean_text(value: Any, limit: int = 200_000) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    text = redact_auth_query_values(text)
    text = PRIVATE_PEM.sub("[REDACTED PRIVATE KEY]", text)
    text = SECRET_ASSIGNMENT.sub(lambda match: match[0] if "[REDACTED" in match[0] or preserve_public_token_query(match) else match[1] + "[REDACTED CREDENTIAL]", text)
    return SECRET_VALUE.sub("[REDACTED CREDENTIAL]", text)[:limit]


def safe_value(value: Any, depth: int = 0, *, array_limit: int | None = 30) -> Any:
    """Normalize metadata with bounded defaults and optional full JSON retention.

    ``array_limit=None`` preserves complete deterministic record structures,
    including nested fields and long strings. Only streaming Google Maps
    adapters opt into this mode. Credential redaction always remains active.
    """
    full_record = array_limit is None
    if not full_record and depth > 5:
        return "[nested data retained in source]"
    if isinstance(value, dict):
        fields = value.items() if full_record else list(value.items())[:100]
        return {
            clean_text(k, max(len(str(k)), 1) if full_record else 300): "[REDACTED CREDENTIAL]" if SECRET_KEY.search(str(k)) else safe_value(v, depth + 1, array_limit=array_limit)
            for k, v in fields
        }
    if isinstance(value, (list, tuple)):
        selected = value if full_record else value[:array_limit]
        items = [safe_value(v, depth + 1, array_limit=array_limit) for v in selected]
        if array_limit is not None and len(value) > array_limit:
            items.append({"additional_items_retained_in_source": len(value) - array_limit})
        return items
    if isinstance(value, str):
        return clean_text(value, max(len(value), 1) if full_record else 2000)
    if isinstance(value, (int, float, bool)) or value is None:
        return value if not isinstance(value, float) or math.isfinite(value) else str(value)
    return clean_text(value, 2000)


def document_text(value: Any, limit: int) -> str:
    """Render coherent data without promoting nested metadata to separate events."""
    def redact(item: Any, depth: int = 0) -> Any:
        if depth > 12:
            return "[nested data retained in source]"
        if isinstance(item, dict):
            return {str(k): "[REDACTED CREDENTIAL]" if SECRET_KEY.search(str(k)) else redact(v, depth + 1) for k,v in item.items()}
        if isinstance(item, list):
            return [redact(v, depth + 1) for v in item]
        if isinstance(item, str):
            return clean_text(item, limit)
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        return clean_text(item, limit)

    rendered = json.dumps(redact(value), ensure_ascii=False, indent=2)
    if len(rendered) > limit:
        marker = "\n[Document extraction limit reached; full content retained in source]"
        return clean_text(rendered, max(0, limit - len(marker))) + marker
    return clean_text(rendered, limit)


def parse_date(value: Any, *, assume_utc: bool = False, unit: str | None = None) -> tuple[str | None, str, dict]:
    """Return UTC, precision and provenance; never guess a local timezone."""
    metadata: dict[str, Any] = {}
    precision = "second"
    if value is None or value == "" or isinstance(value, bool):
        return None, "unknown", metadata
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and re.fullmatch(r"-?\d{10,19}(?:\.\d+)?", value.strip())
    ):
        try:
            number = float(value)
            inferred_unit = unit or ("ns" if abs(number) >= 1e17 else "us" if abs(number) >= 1e14 else "ms" if abs(number) >= 1e11 else "s")
            divisor = {"s": 1, "ms": 1000, "us": 1_000_000, "ns": 1_000_000_000}[inferred_unit]
            dt = datetime.fromtimestamp(number / divisor, UTC)
            metadata["timestamp_unit"] = inferred_unit
            precision = "microsecond" if inferred_unit in {"us", "ns"} or (inferred_unit == "s" and number != int(number)) else "millisecond" if inferred_unit == "ms" else "second"
            if inferred_unit == "ns":
                metadata["normalized_timestamp_precision"] = "microsecond"
            return iso_utc(dt), precision, metadata
        except (ValueError, OverflowError, OSError, KeyError):
            return None, "unknown", {"unparsed_date": clean_text(value, 300)}
    if isinstance(value, datetime):
        dt = value
        precision = "microsecond" if dt.microsecond else "second"
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time(), UTC)
        return iso_utc(dt), "day", {"original_date": value.isoformat(), "date_is_day_bucket": True}
    else:
        raw = str(value).strip()
        if re.search(r"\d{1,2}:\d{2}(?::\d{2})?", raw):
            clock = re.search(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?", raw)[0]
            precision = "minute" if clock.count(":") == 1 else "millisecond" if re.search(r"\.\d{1,3}$", clock) else "microsecond" if "." in clock else "second"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", raw):
            try:
                d = date.fromisoformat(raw)
                return iso_utc(datetime.combine(d, datetime.min.time(), UTC)), "day", {"original_date": raw, "date_is_day_bucket": True}
            except ValueError:
                return None, "unknown", {"unparsed_date": clean_text(raw, 300)}
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                from dateutil.parser import parse as parse_flexible

                # Require an explicit year and clock. Flexible parsing must not fill
                # omitted fields from today's date or interpret arbitrary numbers.
                if not (re.search(r"\b(?:19|20)\d{2}\b", raw) and re.search(r"\d{1,2}:\d{2}", raw)):
                    return None, "unknown", {"unparsed_date": clean_text(raw, 300)}
                dt = parse_flexible(raw, fuzzy=False, default=datetime(2000, 1, 1))
            except (ImportError, ValueError, OverflowError):
                return None, "unknown", {"unparsed_date": clean_text(raw, 300)}
    if dt.tzinfo is None:
        if not assume_utc:
            return dt.date().isoformat(), "day", {
                "original_datetime": dt.isoformat(),
                "timezone_unknown": True,
                "timestamp_role": "local_calendar_date_not_utc_instant",
            }
        dt = dt.replace(tzinfo=UTC)
        metadata["timezone_assumption"] = "UTC specified by source format"
    return iso_utc(dt), precision, metadata


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def event(
    title: Any,
    body: Any,
    locator: str,
    *,
    native_id: Any = None,
    when: Any = None,
    end: Any = None,
    category: str = "document",
    metadata: dict | None = None,
    assume_utc: bool = False,
    unit: str | None = None,
    limit: int = 200_000,
    metadata_array_limit: int | None = 30,
) -> dict:
    occurred, precision, date_meta = parse_date(when, assume_utc=assume_utc, unit=unit)
    ended, _, end_meta = parse_date(end, assume_utc=assume_utc, unit=unit)
    meta = safe_value(metadata or {}, array_limit=metadata_array_limit)
    meta.update(date_meta)
    if end_meta:
        meta["end_date_metadata"] = end_meta
    text = clean_text(body, limit)
    if len(str(body or "")) > limit:
        meta["body_truncated"] = True
        meta["full_content_retained_in_source"] = True
    return {
        "source_native_id": str(native_id) if native_id is not None and native_id != "" else None,
        "title": clean_text(title, 500) or "Untitled record",
        "body": text,
        "occurred_at": occurred,
        "end_at": ended,
        "category": category,
        "time_precision": precision,
        "metadata": meta,
        "locator": locator,
    }


def pick(record: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return default
