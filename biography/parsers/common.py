"""Shared safe normalization. Filesystem timestamps are never event dates."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any

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


def clean_text(value: Any, limit: int = 200_000) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    text = PRIVATE_PEM.sub("[REDACTED PRIVATE KEY]", text)
    text = SECRET_ASSIGNMENT.sub(lambda match: match[0] if "[REDACTED" in match[0] else match[1] + "[REDACTED CREDENTIAL]", text)
    return SECRET_VALUE.sub("[REDACTED CREDENTIAL]", text)[:limit]


def safe_value(value: Any, depth: int = 0) -> Any:
    """Keep useful source metadata without recursively copying large payloads."""
    if depth > 5:
        return "[nested data retained in source]"
    if isinstance(value, dict):
        return {
            clean_text(k, 300): "[REDACTED CREDENTIAL]" if SECRET_KEY.search(str(k)) else safe_value(v, depth + 1)
            for k, v in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        items = [safe_value(v, depth + 1) for v in value[:30]]
        if len(value) > 30:
            items.append({"additional_items_retained_in_source": len(value) - 30})
        return items
    if isinstance(value, str):
        return clean_text(value, 2000)
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
) -> dict:
    occurred, precision, date_meta = parse_date(when, assume_utc=assume_utc, unit=unit)
    ended, _, end_meta = parse_date(end, assume_utc=assume_utc, unit=unit)
    meta = safe_value(metadata or {})
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
