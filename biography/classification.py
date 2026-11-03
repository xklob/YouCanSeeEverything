"""Pure, source-backed presentation hints over immutable catalog records.

This module never reads files, contacts providers, or changes source/catalog
rows. Missing units, timezones, dates, and occurrence identities stay unknown.
Aggregation flags describe explicit source semantics, never a guessed daily
total. Callers retain variants and must not add overlapping interval summaries.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from .parsers.common import parse_date
from .source_policy import package_member

CLASSIFIER_VERSION = 3
CLASSIFICATION_VERSION = CLASSIFIER_VERSION

_ACTIVITY_CATEGORIES = {
    "activity", "travel", "place_visit", "post", "comment", "purchase",
    "event_booking", "event_attendance", "git_commit", "listening", "usage",
    "ethereum_transaction", "ethereum_token_transfer", "ethereum_internal_transaction",
    "sleep", "health_achievement",
}
_TECHNICAL_CATEGORIES = {"shell_command", "project_context", "technical", "diagnostic", "telemetry"}
_MEASUREMENT_CATEGORIES = {"location", "measurement", "health_sample", "health_measurement", "sensor_sample"}
_HEALTH_FOLDERS = {
    "steps", "distance", "calories", "heart rate", "daily resting heart rate",
    "heart rate variability", "daily heart rate variability", "oxygen saturation",
    "oxygen saturation (spo2)", "daily oxygen saturation", "body temperature",
    "temperature", "daily respiratory rate", "respiratory rate sleep summary",
    "height", "weight", "floors", "altitude", "active energy burned",
    "active minutes", "active zone minutes", "active zone minutes (azm)",
    "time in heart rate zone", "calories in heart rate zone", "activity level",
    "micro stillness", "micro motion", "continuous eda", "live pace",
    "breathing quality", "body response algorithm features", "cardio load",
    "daily activity metrics", "sleep score", "stress score", "daily readiness",
    "cardio load observed interval", "daily sleep temperature derivations",
    "daily sleep breathing quality", "daily heart rate zones", "run vo2max",
    "vo2 max", "daily vo2 max", "demographic vo2max",
}
_UNITS = {
    "bpm": "bpm", "beats per minute": "bpm", "beats/min": "bpm",
    "breaths per minute": "breaths/min", "breaths/min": "breaths/min",
    "milliseconds": "ms", "millisecond": "ms", "ms": "ms",
    "seconds": "s", "second": "s", "s": "s",
    "minutes": "min", "minute": "min", "min": "min",
    "meters": "m", "metres": "m", "meter": "m", "m": "m",
    "millimeters": "mm", "millimeter": "mm", "mm": "mm",
    "centimeters": "cm", "centimeter": "cm", "cm": "cm",
    "kilometers": "km", "kilometer": "km", "km": "km",
    "grams": "g", "gram": "g", "g": "g",
    "kilograms": "kg", "kilogram": "kg", "kg": "kg",
    "pounds": "lb", "pound": "lb", "lb": "lb", "lbs": "lb",
    "celsius": "°C", "degc": "°C", "°c": "°C",
    "fahrenheit": "°F", "degf": "°F", "°f": "°F",
    "kilocalories": "kcal", "kilocalorie": "kcal", "kcal": "kcal",
    "percentage": "%", "percent": "%", "%": "%",
    "steps": "steps", "step": "steps", "floors": "floors", "floor": "floors",
    "count": "count", "score": "score", "m/s": "m/s",
    "micro siemens": "µS", "usiemens": "µS",
    "microseconds": "µs", "microsecond": "µs", "µs": "µs", "us": "µs",
    "milli breaths per minute": "milli-breaths/min",
}
_FIELDS = {
    "beats per minute": ("heart_rate", "bpm"),
    "average heart rate (bpm)": ("average_heart_rate", "bpm"),
    "max heart rate (bpm)": ("maximum_heart_rate", "bpm"),
    "min heart rate (bpm)": ("minimum_heart_rate", "bpm"),
    "non rem heart rate beats per minute": ("non_rem_heart_rate", "bpm"),
    "hr mean bpm": ("mean_heart_rate", "bpm"),
    "average heart rate variability milliseconds": ("average_heart_rate_variability", "ms"),
    "deep sleep root mean square of successive differences milliseconds": ("deep_sleep_hrv_rmssd", "ms"),
    "breaths per minute": ("respiratory_rate", "breaths/min"),
    "full sleep stats - milli breaths per minute": ("full_sleep_respiratory_rate", "milli-breaths/min"),
    "deep sleep stats - milli breaths per minute": ("deep_sleep_respiratory_rate", "milli-breaths/min"),
    "light sleep stats - milli breaths per minute": ("light_sleep_respiratory_rate", "milli-breaths/min"),
    "rem sleep stats - milli breaths per minute": ("rem_sleep_respiratory_rate", "milli-breaths/min"),
    "height millimeters": ("height", "mm"), "weight grams": ("weight", "g"),
    "average weight (kg)": ("average_weight", "kg"),
    "max weight (kg)": ("maximum_weight", "kg"),
    "min weight (kg)": ("minimum_weight", "kg"),
    "temperature celsius": ("body_temperature", "°C"),
    "nightly temperature celsius": ("nightly_temperature", "°C"),
    "baseline temperature celsius": ("baseline_temperature", "°C"),
    "skin temperature magnitude celsius": ("skin_temperature", "°C"),
    "oxygen saturation percentage": ("oxygen_saturation", "%"),
    "kilocalories": ("active_energy", "kcal"),
    "calories (kcal)": ("energy", "kcal"), "kcal": ("energy", "kcal"),
    "distance (m)": ("distance", "m"), "distance millimeters": ("distance", "mm"),
    "altitude gain millimeters": ("altitude_gain", "mm"),
    "average speed (m/s)": ("average_speed", "m/s"),
    "max speed (m/s)": ("maximum_speed", "m/s"),
    "min speed (m/s)": ("minimum_speed", "m/s"),
    "steps": ("steps", "steps"), "step count": ("steps", "steps"),
    "cumulative steps": ("steps", "steps"), "floors": ("floors", "floors"),
    "stroke count": ("swimming_strokes", "count"),
    "total minutes": ("active_zone_minutes", "min"),
    "total_minutes": ("active_zone_minutes", "min"),
    "move minutes count": ("move_minutes", "min"),
    "eda level real micro siemens": ("electrodermal_activity", "µS"),
    "ceda magnitude real micro siemens": ("electrodermal_activity", "µS"),
}
_VALID_UNITS = frozenset(_UNITS.values())


def _mapping(value):
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def _key(value):
    return re.sub(r"\s+", " ", str(value).strip().casefold())


def _hash(value):
    def normalize(item):
        if isinstance(item, Mapping):
            return {str(k): normalize(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        return item
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value.strip()):
        return None
    try:
        result = float(Decimal(str(value).strip()))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def _unit(value):
    # SI case matters: µS is conductance, µs is time.
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value in {"µS", "μS"}:
        return "µS"
    if value in {"µs", "μs"}:
        return "µs"
    return _UNITS.get(_key(value))


def _paths(provenance):
    return [str(row.get("original_path") or row.get("path") or "").replace("\\", "/") for row in provenance]


def _source(event, metadata, provenance):
    original = str(event.get("source_name") or event.get("source_label") or event.get("source") or "Unknown")
    parsers = [str(row.get("parser") or "") for row in provenance]
    chatgpt = any("chatgpt.messages" in parser for parser in parsers) or (
        bool(metadata.get("conversation_id")) and metadata.get("role") in {"user", "assistant", "system", "tool"}
        and "content_type" in metadata and ("message_metadata" in metadata or "parent_node" in metadata)
    )
    if chatgpt:
        return "ChatGPT", True, False, set()
    path_parts = {part.casefold() for path in _paths(provenance) for part in path.split("/")}
    if "fitbit" in path_parts or original.casefold() == "fitbit":
        return "Fitbit", False, True, path_parts
    if "fit" in path_parts or "google fit" in path_parts or original.casefold() == "google fit":
        return "Google Fit", False, True, path_parts
    if "health connect" in path_parts or "healthconnect" in path_parts or original.casefold() in {"health connect", "healthconnect"}:
        return "Health Connect", False, True, path_parts
    if "google health & fit" in path_parts or "google health and fit" in path_parts or original.casefold() in {"google health & fit", "google health and fit"}:
        return "Google Health & Fit", False, True, path_parts
    return original, False, False, path_parts


def _time(event, metadata, notes):
    occurred = event.get("occurred_at")
    precision = str(event.get("time_precision") or "unknown")
    if occurred:
        basis = "source_local_calendar_date" if metadata.get("timezone_unknown") or metadata.get("timestamp_role") == "local_calendar_date_not_utc_instant" else "source_date" if precision in {"day", "month", "year"} else "source_timestamp"
        return occurred, precision, basis
    for field in ("timestamp", "dateTime", "datetime", "date_time", "Start time", "start time", "reading_time", "sleep_start_time", "body_response_start", "DATE", "date"):
        value = metadata.get(field)
        if value in (None, ""):
            continue
        # Time-only strings and two-digit years must not inherit today's date or
        # a guessed century. parse_date has the same explicit-year constraint.
        if isinstance(value, str) and not re.search(r"(?:19|20)\d{2}", value):
            continue
        normalized, normalized_precision, details = parse_date(value)
        if normalized:
            if details.get("timezone_unknown"):
                notes.append("Source local time has no recorded timezone; only its calendar date is used.")
                return normalized, normalized_precision, "source_local_calendar_date"
            if normalized_precision == "day" and details.get("date_is_day_bucket"):
                return normalized[:10], normalized_precision, "source_calendar_date"
            return normalized, normalized_precision, "explicit_source_field:" + field
    notes.append("No unambiguous source event date is available; it remains undated.")
    return None, "unknown", "unknown"


def _device(metadata):
    for key in ("device_id", "device id", "deviceId", "device"):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            value = value.get("id") or value.get("device_id") or value.get("name")
        if value not in (None, "") and not isinstance(value, (list, dict)):
            return str(value), "explicit_device"
    for key in ("data source", "data_source", "dataSourceId", "data_source_id"):
        value = metadata.get(key)
        if value not in (None, "") and not isinstance(value, (list, dict)):
            return "data source: " + str(value), "recorded_data_source"
    return None, "unknown"


def _boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        value = value.strip().casefold()
        if value in {"true", "1", "yes"}:
            return True
        if value in {"false", "0", "no"}:
            return False
    return None


def _aggregation(metadata, field, explicit=None):
    normalized = {_key(k).replace("_", " "): v for k, v in metadata.items()}
    semantics = str(normalized.get("sample semantics") or normalized.get("measurement type") or normalized.get("metric type") or "").casefold()
    aggregation = str(explicit or normalized.get("aggregation") or "").casefold()
    flags = [normalized[key] for key in ("is cumulative", "cumulative") if key in normalized and normalized[key] not in (None, "")]
    cumulative = "cumulative" in field.casefold() or semantics in {"cumulative", "counter", "running_total", "running total"} or aggregation in {"cumulative", "latest"} or any(_boolean(flag) is True for flag in flags)
    if cumulative:
        return "latest"
    if any(_boolean(flag) is None for flag in flags):
        return "none"
    interval_start = metadata.get("start_time") or metadata.get("startTime") or metadata.get("Start time")
    interval_end = metadata.get("end_time") or metadata.get("endTime") or metadata.get("End time")
    if interval_start and interval_end and (semantics in {"interval_delta", "interval delta", "additive", "delta"} or aggregation == "sum"):
        start, _, _ = parse_date(interval_start)
        end, _, _ = parse_date(interval_end)
        if start and end and "T" in start and "T" in end and datetime.fromisoformat(end.replace("Z", "+00:00")) > datetime.fromisoformat(start.replace("Z", "+00:00")):
            return "sum"
    return "none"


def _metric(name, value, unit, metadata, field, device, *, explicit_aggregation=None):
    numeric = _number(value)
    if numeric is None or not name or unit not in _VALID_UNITS:
        return None
    if unit in {"steps", "floors", "count"} and (numeric < 0 or not numeric.is_integer()):
        return None
    aggregation = _aggregation(metadata, field, explicit_aggregation)
    result = {"name": name, "value": numeric, "unit": unit, "aggregation": aggregation, "device": device}
    if aggregation == "sum":
        result["interval_start"] = metadata.get("start_time") or metadata.get("startTime") or metadata.get("Start time")
        result["interval_end"] = metadata.get("end_time") or metadata.get("endTime") or metadata.get("End time")
    result["aggregation_basis"] = "explicit_cumulative_counter" if aggregation == "latest" else "explicit_additive_interval" if aggregation == "sum" else "source_measurement_only; no daily total inferred"
    return result


def _metrics(metadata, health, folders, device, notes):
    result = []
    typed = metadata.get("metric")
    if isinstance(typed, Mapping):
        unit = _unit(typed.get("unit"))
        metric_name = str(typed.get("name") or "")
        context = {**metadata, **typed}
        if _aggregation(metadata, metric_name) == "latest":
            context["is_cumulative"] = True
        candidate = _metric(metric_name, typed.get("value"), unit, context, metric_name, device, explicit_aggregation=typed.get("aggregation"))
        if candidate:
            result.append(candidate)
        else:
            notes.append("A source measurement failed numeric/unit validation and was excluded from metrics.")
    if health:
        for field, value in metadata.items():
            normalized = _key(field)
            descriptor = _FIELDS.get(normalized)
            if not descriptor:
                continue
            name, unit = descriptor
            if normalized == "beats per minute" and "daily resting heart rate" in folders:
                name = "resting_heart_rate"
            candidate = _metric(name, value, unit, metadata, normalized, device)
            if candidate:
                result.append(candidate)
            elif value not in (None, ""):
                notes.append("A source measurement failed numeric/unit validation and was excluded from metrics.")
        if "daily oxygen saturation" in folders and "average percentage" in metadata:
            candidate = _metric("average_oxygen_saturation", metadata["average percentage"], "%", metadata, "average percentage", device)
            if candidate:
                result.append(candidate)
        # Explicit generic value+unit records may describe different quantities.
        # Neither an unlabeled value nor locale/profile preferences assign units.
        if not result and "value" in metadata and (metadata.get("unit") or metadata.get("units")):
            unit = _unit(metadata.get("unit") or metadata.get("units"))
            name = str(metadata.get("metric_name") or metadata.get("measurement_name") or "").strip()
            if name:
                candidate = _metric(name, metadata["value"], unit, metadata, name, device)
                if candidate:
                    result.append(candidate)
        if not result and any(key in metadata for key in ("value", "distance", "calories", "weight", "height")):
            notes.append("Values without an explicit validated quantity/unit are not converted into metrics.")
    unique = []
    seen = set()
    for metric in result:
        key = _hash(metric)
        if key not in seen:
            seen.add(key)
            unique.append(metric)
    return unique


def _identity(event, metadata, provenance, variant):
    account = metadata.get("account_id")
    if account is None or account == "":
        account = event.get("account_id")
    source = event.get("source_id")
    if not source:
        if account is None or account == "":
            return None, None, "unknown"
        source = event.get("source_name") or event.get("source")
    if not source:
        return None, None, "unknown"
    scope = [str(source), account]
    native = event.get("native_id")
    if native is None or native == "":
        native = event.get("source_native_id")
    if native is not None and native != "":
        occurrence = "occurrence:" + _hash([scope, "native", str(native)])
        return occurrence + ":" + variant, occurrence, "source_native_identity"
    anchors = sorted({(str(row.get("object_hash") or row.get("sha256")).casefold(), str(row.get("locator"))) for row in provenance if re.fullmatch(r"[0-9a-f]{64}", str(row.get("object_hash") or row.get("sha256") or ""), re.I) and row.get("locator")})
    if anchors:
        occurrence = "occurrence:" + _hash([scope, "canonical_record", anchors[0]])
        return occurrence + ":" + variant, occurrence, "canonical_object_and_record_locator"
    return None, None, "unknown"


def _sample_family(metadata, provenance, metrics):
    """Name a source stream without assigning missing numeric units or dates."""
    if len(metrics) == 1:
        return metrics[0]["name"]
    for key, family in (("daily_respiratory_rate", "respiratory_rate"), ("distance", "distance"), ("calories", "calories"), ("still", "stillness"), ("sleep coefficients", "motion")):
        if key in metadata:
            return family
    for path in _paths(provenance):
        parts = PurePosixPath(path).parts
        for part in reversed(parts[:-1]):
            if _key(part) in _HEALTH_FOLDERS:
                return re.sub(r"[^a-z0-9]+", "_", _key(part)).strip("_")
        filename = PurePosixPath(path).name.casefold()
        matched = re.match(r"(steps|distance|height|weight|altitude|calories|heart_rate|time_in_heart_rate_zones)-\d", filename)
        if matched:
            return matched[1]
    return "unclassified_health_measurement"


def classify_event(event: Mapping, provenance: list[Mapping] | None = None) -> dict:
    """Classify a single source record without mutating it or its provenance."""
    event = dict(event)
    provenance = list(provenance if provenance is not None else event.get("provenance") or [])
    provenance = [row for row in provenance if isinstance(row, Mapping)]
    metadata = _mapping(event.get("metadata"))
    notes = []
    source_label, chatgpt, health, folders = _source(event, metadata, provenance)
    category = str(event.get("category") or "document")
    if category == "git_commit" and metadata.get("personal_attribution") is False:
        category = "project_context"
        notes.append("The source attributes this commit to another author; it is project context.")
    is_sample = False
    if chatgpt or category == "message":
        display_class = "conversation"
    elif category in _ACTIVITY_CATEGORIES:
        display_class = "activity"
    elif category in _TECHNICAL_CATEGORIES:
        display_class = "technical"
    elif category == "media":
        display_class = "media"
    elif category in _MEASUREMENT_CATEGORIES:
        display_class, is_sample = "measurement", True
    else:
        display_class = "document"
    paths = _paths(provenance)
    if paths and all(package_member(path) for path in paths):
        display_class, category, is_sample = 'technical', 'application_resource', False
        notes.append('Evidence is inside an application package; this is implementation material.')
    device, device_basis = _device(metadata)
    measurement_keys = set(_key(key) for key in metadata) & set(_FIELDS)
    legacy_measurement_file = any(re.match(r"(?:steps|distance|height|weight|altitude|calories|heart_rate|time_in_heart_rate_zones)-\d", PurePosixPath(path).name, re.I) for path in _paths(provenance))
    timestamped_scalar = "dateTime" in metadata and "value" in metadata and _number(metadata["value"]) is not None
    health_sample = health and (bool(measurement_keys) or timestamped_scalar or (bool(folders & _HEALTH_FOLDERS) or legacy_measurement_file) and any(key in metadata for key in ("timestamp", "dateTime", "date_time", "Start time")))
    metric_context = dict(metadata)
    if event.get("occurred_at") and event.get("end_at"):
        metric_context.setdefault("start_time", event["occurred_at"])
        metric_context.setdefault("end_time", event["end_at"])
    metrics = _metrics(metric_context, health or category in _MEASUREMENT_CATEGORIES, folders, device, notes)
    for item in metrics:
        item["device_basis"] = device_basis
        account = metadata.get("account_id")
        if account is None or account == "":
            account = event.get("account_id")
        item["group_key"] = "metric:" + _hash([event.get("source_id") or source_label, account, source_label, item["name"], item["unit"], device, item["aggregation"]])
    if display_class == "document" and (health_sample or metrics):
        display_class, category, is_sample = "measurement", "health_sample" if health else "measurement", True
    # A Google Fit interval row with every measurement blank is a placeholder,
    # not a completed activity or an observed numeric measurement.
    if "daily activity metrics" in folders and health_sample and not metrics:
        numeric_fields = [value for field, value in metadata.items() if _key(field) in _FIELDS]
        if numeric_fields and all(value in (None, "") for value in numeric_fields):
            display_class, category, is_sample = "technical", "measurement_placeholder", False
            notes.append("This interval row contains no numeric measurements.")
    if device_basis == "recorded_data_source" and is_sample:
        notes.append("Grouping preserves the recorded data-source identity; it does not infer a physical device.")
    occurred, precision, date_basis = _time(event, metadata, notes)
    metric = metrics[0] if len(metrics) == 1 else None
    payload = {"category": event.get("category"), "occurred_at": event.get("occurred_at"), "end_at": event.get("end_at"), "time_precision": event.get("time_precision"), "body": event.get("body"), "metadata": metadata}
    if not is_sample:
        payload["title"] = event.get("title")
    variant = "variant:" + _hash(payload)
    dedup, occurrence, identity_basis = _identity(event, metadata, provenance, variant)
    sample_family = _sample_family(metadata, provenance, metrics) if is_sample else None
    # This key partitions stream summaries; it never proves duplicate occurrences.
    sample_group_key = "sample_stream:" + _hash([event.get("source_id") or source_label, metadata.get("account_id") if metadata.get("account_id") is not None else event.get("account_id"), source_label, sample_family, device]) if is_sample else None
    return {
        "category": category, "source_label": source_label, "occurred_at": occurred,
        "time_precision": precision, "date_basis": date_basis,
        "is_sample": is_sample, "is_raw_sample": is_sample, "display_class": display_class,
        "metric": metric, "metrics": metrics,
        "numeric_value": metric["value"] if metric else None,
        "unit": metric["unit"] if metric else None,
        "device": device, "device_basis": device_basis,
        "dedup_key": dedup, "display_group_key": dedup,
        "occurrence_key": occurrence, "identity_basis": identity_basis,
        "sample_family": sample_family, "sample_group_key": sample_group_key,
        "variant_key": variant, "notes": list(dict.fromkeys(notes)),
    }


__all__ = ["CLASSIFIER_VERSION", "CLASSIFICATION_VERSION", "classify_event"]
