"""Cited narratives of real inferred journey groups and coincident records."""
from __future__ import annotations

from datetime import timedelta
import fcntl
import json
from pathlib import Path
import re

from . import agents, journeys, retrieval, summary_freshness, synthesis
from .parsers.common import clean_text
from .trips import _instant

MAX_ANCHORS = 16
MAX_WINDOW_DAYS = 62


def _key(trip_id, source_ids, focus, model):
    if not isinstance(trip_id, str) or not re.fullmatch(r"[a-f0-9]{64}", trip_id):
        raise ValueError("Choose an existing journey ID.")
    if not isinstance(focus, str) or len(focus) > 4000:
        raise ValueError("A summary focus must be at most 4,000 characters.")
    agents._model(model)
    chosen = retrieval.selection(source_ids=source_ids)
    return retrieval._digest({"kind": "trip_summary", "trip_id": trip_id, "source_ids": chosen["source_ids"], "focus": focus.strip(), "model": model, "policy": synthesis.POLICY_VERSION, "retrieval_policy": retrieval.POLICY_VERSION})


def trip_summary_status(trip_id: str, work_dir: Path, path=None, *, source_ids=None, focus="", model=agents.DEFAULT_MODEL) -> dict:
    """Cheap saved lookup. No journey/catalog read and no inference."""
    key = _key(trip_id, source_ids, focus, model)
    saved = synthesis.saved_summary(key, work_dir)
    return {"status": "saved" if saved else "missing", "freshness": "not_checked", "trip_id": trip_id, "key": key, "model": model, "result": saved["result"] if saved else None}


def _journey(trip_id, path):
    detail = journeys.journey_detail(trip_id, path, limit=500)
    if detail is None:
        raise ValueError("The selected journey no longer exists; choose a current journey.")
    group = detail["journey"]
    departure, arrival = _instant(group["departure_at"]), _instant(group["arrival_at"])
    if not departure or not arrival or arrival < departure:
        raise ValueError("The selected journey does not have a reliable time window.")
    start, end = departure.date(), arrival.date()
    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise ValueError("This journey exceeds the bounded summary window; choose a shorter journey.")
    endpoints = [group["first_stop"], group["last_stop"]]
    endpoints.extend(endpoint for leg in detail["items"] for endpoint in (leg["from"], leg["to"]))
    identities = list(dict.fromkeys(endpoint["event_id"] for endpoint in endpoints))
    context = {"trip_id": trip_id, "source_id": group["source_id"], "source": group["source"],
               "departure_at": group["departure_at"], "arrival_at": group["arrival_at"],
               "window_start": start.isoformat(), "window_end": end.isoformat(),
               "window_end_exclusive": (end + timedelta(days=1)).isoformat(),
               "time_basis": "UTC calendar days intersecting the inferred departure/arrival window",
               "inferred": True, "route_known": False, "mode_known": False,
               "anchor_event_ids": [], "coincident_event_ids": [],
               "anchors_complete": len(identities) <= MAX_ANCHORS and bool(detail["complete"]),
               "journey_coverage_complete": bool(detail["coverage_complete"]),
               "notice": journeys.NOTICE,
               "coincident_notice": "Other records dated in this window are coincident context. Their dates do not confirm presence, trip participation or travel activity."}
    context["journey_signature"] = retrieval._digest([group, identities, detail["total"], detail["complete"], detail["coverage_complete"]])
    anchor_ids = identities[:2] + retrieval._temporal_order(identities[2:])
    return context, anchor_ids[:MAX_ANCHORS]


def _anchors(path, identities, source_id):
    if not identities:
        return []
    with retrieval._connection(path) as conn:
        rows = conn.execute("SELECT " + retrieval._COLUMNS + ",substr(e.body,1,2000) AS body FROM events e JOIN sources s ON s.id=e.source_id WHERE e.source_id=? AND e.id IN (" + ",".join("?" for _ in identities) + ")", [source_id, *identities]).fetchall()
        provenance = retrieval._provenance(conn, identities)
    by_id = {}
    for row in rows:
        value = dict(row)
        if not provenance.get(value["id"]):
            continue
        try:
            metadata = json.loads(value["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        hints = {key: metadata[key] for key in ("timezone_unknown", "original_datetime", "timestamp_role") if isinstance(metadata, dict) and key in metadata}
        evidence = {key: value[key] for key in ("id", "source_id", "source", "occurred_at", "end_at", "category", "time_precision")}
        evidence.update(title=clean_text(value["title"], 300), body=clean_text(value["body"], 2000), metadata=hints,
                        provenance=[{key: item[key] for key in ("file_id", "locator", "parser")} for item in provenance[value["id"]]],
                        body_truncated=value["body_characters"] > len(value["body"]), excerpt_strategy="leading", retrieval_role="recorded_trip_stop", relation="recorded_trip_stop")
        by_id[value["id"]] = evidence
    return [by_id[identity] for identity in identities if identity in by_id]


def summarize_trip(trip_id: str, work_dir: Path, path=None, *, source_ids=None, focus="", model=agents.DEFAULT_MODEL, force=False) -> dict:
    key = _key(trip_id, source_ids, focus, model)
    root = synthesis._cache_root(work_dir)
    with (root / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        context, identities = _journey(trip_id, path)
        chosen = retrieval.selection(start=context["window_start"], end=context["window_end"], source_ids=source_ids)
        with retrieval._connection(path) as conn:
            retrieval._known_sources(conn, chosen, focus)
        before = summary_freshness.scope_state(path, chosen)
        saved = synthesis._read_cache(root, key)
        if saved and saved.get("trip_context", {}).get("journey_signature") == context["journey_signature"] and not force:
            state = summary_freshness.state(saved.get("freshness_basis"), before, saved.get("evidence_fingerprint"), synthesis._citation_state(path, saved["evidence_ids"]))
            if state == "fresh":
                saved["cache"]["status"] = state
                return saved
        anchors = _anchors(path, identities, context["source_id"])
        if not anchors:
            raise ValueError("The journey has no retained recorded-stop evidence; no narrative was generated.")
        context["anchors_complete"] = context["anchors_complete"] and len(anchors) == len(identities)
        if not context["anchors_complete"] or not context["journey_coverage_complete"]:
            before.update(complete=False, token=None, reason="journey_anchor_or_coverage_limit")
        bundle = retrieval.retrieve(focus, path, source_ids=chosen["source_ids"], start=context["window_start"], end=context["window_end"])
        combined, seen, size = [], set(), 0
        for item in anchors + bundle["evidence"]:
            if item["id"] in seen:
                continue
            item = dict(item)
            item.setdefault("relation", "coincident_record")
            amount = len(json.dumps(item, ensure_ascii=False))
            if len(combined) >= retrieval.MAX_EVIDENCE or size + amount > retrieval.MAX_CONTEXT_CHARS:
                continue
            combined.append(item)
            seen.add(item["id"])
            size += amount
        context["anchor_event_ids"] = [item["id"] for item in combined if item["relation"] == "recorded_trip_stop"]
        context["coincident_event_ids"] = [item["id"] for item in combined if item["relation"] == "coincident_record"]
        bundle["evidence"] = combined
        bundle["evidence_fingerprint"] = retrieval.evidence_state(path, list(seen))
        outside = [item for item in anchors if (chosen["source_ids"] and item["source_id"] not in chosen["source_ids"]) or not item["occurred_at"] or not chosen["start"] <= item["occurred_at"] < chosen["end_exclusive"]]
        coverage = bundle["coverage"]
        coverage.update(context_events_in_scope=coverage["total_events_in_scope"], total_events_in_scope=coverage["total_events_in_scope"] + len(outside), selected_events=len(combined), anchor_events=len(context["anchor_event_ids"]), coincident_events=len(context["coincident_event_ids"]))
        for source in coverage["sources"]:
            source["selected_events"] = sum(item["source_id"] == source["source_id"] for item in combined)
            source["total_events"] += sum(item["source_id"] == source["source_id"] for item in outside)
        if not any(source["source_id"] == context["source_id"] for source in coverage["sources"]):
            coverage["sources"].append({"source_id": context["source_id"], "source": context["source"], "status": "recorded_anchor_source", "total_events": len(anchors), "selected_events": len(anchors), "scope_role": "mandatory_recorded_stop_anchors"})
        bundle["coverage"]["limitations"] += [context["notice"], context["coincident_notice"], "Recorded-stop anchors retain their own source even when context-source filters select other records."]
        result = synthesis._generate(bundle, root, key, path, title="Trip · " + context["window_start"] + " to " + context["window_end"], before_state=before, focus=focus, model=model, kind="trip_summary", trip_context=context)
        try:
            current, _ = _journey(trip_id, path)
            if current["journey_signature"] != context["journey_signature"]:
                result["cache"]["status"] = "stale"
        except ValueError:
            result["cache"]["status"] = "stale"
        synthesis._store(root, key, result)
        return result


__all__ = ["trip_summary_status", "summarize_trip"]
