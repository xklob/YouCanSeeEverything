"""Bounded relevant-scope checks; no global catalog watermark or counts."""
from __future__ import annotations

import json
import time
import sqlite3

from . import retrieval

MAX_ROWS = 2048
MAX_BYTES = 2 * 1024**2
MAX_FIELD = 16_000
SECONDS = 2


def scope_state(path, chosen: dict) -> dict:
    started = time.monotonic()
    rows, identities, byte_count, total = [], [], 0, 0
    complete, reason = True, None
    sources = []
    try:
        with retrieval._connection(path) as conn:
            deadline = time.monotonic() + SECONDS
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
            known = retrieval._known_sources(conn, dict(chosen))
            for source in known[:128]:
                stop = False
                condition, args = retrieval._where({**chosen, "source_ids": [source["id"]]})
                columns = "e.id,e.source_id,e.occurred_at,e.end_at,e.category,e.time_precision," + ",".join(f"substr(e.{name},1,{MAX_FIELD + 1}) AS {name}" for name in ("title", "body", "metadata"))
                row_limit = MAX_ROWS - len(rows) + 1
                entries = conn.execute("SELECT " + columns + " FROM events e INDEXED BY events_source WHERE " + condition + " ORDER BY e.occurred_at DESC LIMIT ?", args + [row_limit]).fetchall()
                total += len(entries)
                sources.append({"source_id": source["id"], "source": source["name"], "total_events": len(entries) if len(entries) < row_limit else None, "selected_events": 0})
                for entry in entries:
                    if len(rows) >= MAX_ROWS or time.monotonic() > deadline:
                        complete, reason = False, "scope_row_or_time_limit"
                        stop = True
                        break
                    record = dict(entry)
                    if any(len(record.get(name) or "") > MAX_FIELD for name in ("title", "body", "metadata")):
                        complete, reason = False, "scope_field_limit"
                    byte_count += len(json.dumps(record, ensure_ascii=False).encode())
                    if byte_count > MAX_BYTES:
                        complete, reason = False, "scope_byte_limit"
                        stop = True
                        break
                    try:
                        record["metadata"] = json.loads(record["metadata"] or "{}")
                    except (TypeError, ValueError):
                        record["metadata"] = {}
                        complete, reason = False, "invalid_or_truncated_metadata"
                    record["source"] = source["name"]
                    rows.append(record)
                    identities.append(record["id"])
                if stop or byte_count > MAX_BYTES or time.monotonic() > deadline:
                    complete, reason = False, reason or "scope_row_or_time_limit"
                    break
            if len(known) > 128:
                complete, reason = False, "source_limit"
            provenance = retrieval._provenance(conn, identities)
            narrative = []
            for record in rows:
                if time.monotonic() > deadline:
                    complete, reason = False, "scope_time_limit"
                    break
                entries = provenance.get(record["id"], [])
                if entries and not retrieval._raw_sample(record, entries):
                    narrative.append([record, entries])
            signature = retrieval._digest(sorted(narrative, key=lambda item: item[0]["id"])) if complete else None
    except (ValueError, OSError, sqlite3.Error):
        complete, signature, reason = False, None, "scope_check_unavailable"
    try:
        from .classification import CLASSIFIER_VERSION
    except ImportError:
        CLASSIFIER_VERSION = 0
    token = retrieval._digest({"scope": {key: chosen.get(key) for key in ("start", "end_exclusive", "source_ids")}, "records": signature, "retrieval_policy_version": retrieval.POLICY_VERSION, "classification_version": CLASSIFIER_VERSION}) if complete else None
    return {"complete": complete, "token": token, "reason": reason,
            "rows_checked": len(rows), "bytes_checked": byte_count,
            "elapsed_seconds": round(time.monotonic() - started, 4),
            "coverage": {"total_events_in_scope": total if complete else None,
                         "selected_events": 0, "sources": sources,
                         "time_basis": chosen.get("time_basis"),
                         "limitations": ["Bounded freshness validation only; no new narrative or exhaustive search was generated."]}}


def state(saved_basis, current, before_selected, after_selected):
    if before_selected is None or after_selected is None:
        return "unknown"
    if before_selected != after_selected:
        return "stale"
    if saved_basis and saved_basis.get("complete") and current.get("complete"):
        return "fresh" if saved_basis.get("token") == current.get("token") else "stale"
    return "unknown"
