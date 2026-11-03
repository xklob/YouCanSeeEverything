"""Read-only coverage of observed imported records, never complete service history.

File/progress summaries are current read snapshots. Record counts come from the
durable incremental catalog counters. Optional monthly presence has its own
bounded refresh and never authorizes a default whole-event aggregate.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

from .config import settings
from .session_sources import sanitize_visible
from .query_stats import get_catalog_stats
from .import_progress import public_activity


ACTIVE = {"queued", "pending", "running", "importing", "processing", "indexing"}
REVIEW = {"error", "needs_fallback", "partial"}
FILE_STATES = {"parsed", "partial", "metadata_only", "retained", "empty", "excluded", "pending", "error", "needs_fallback", "unsupported", "quarantined"}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coverage-read")
_LOCK = threading.Lock()
_CACHE: dict[tuple, "_Entry"] = {}


@dataclass
class _Entry:
    result: dict | None = None
    pending: bool = False
    attempted_at: float = 0
    error: str | None = None
    future: Future | None = None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=1000")
    return connection


def _signature(path: Path):
    parts = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            info = candidate.stat()
            parts.append(None if candidate != path and info.st_size == 0 else (info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            parts.append(None)
    return tuple(parts)


def _safe_text(value, limit=1200):
    return sanitize_visible(str(value or "")[:limit])[0]


def _notes(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        parsed = [value]
    if not isinstance(parsed, list):
        parsed = [parsed]
    return [_safe_text(note) for note in parsed[:30] if isinstance(note, (str, int, float))]


def _progress(completed, total):
    done, planned = max(0, int(completed or 0)), max(0, int(total or 0))
    return {"completed": done, "total": planned,
            "fraction": min(1.0, done / planned) if planned else None,
            "exceeds_total": done > planned if planned else False}


def _safe_error(value, status):
    if not value:
        return None
    match = re.match(r"([A-Za-z][A-Za-z0-9_]{0,60}(?:Error|Exception|Paused))\b", str(value))
    return {"code": match[1] if match else "import_needs_review",
            "message": "Import paused; review its status before retrying." if status == "paused"
            else "This import needs attention. Review its status before retrying."}


def _refresh(key, path, include_months, month_limit, budget):
    started = time.monotonic()
    connection = None
    try:
        signature = _signature(path)
        connection = _read(path)
        connection.set_progress_handler(lambda: time.monotonic() - started > budget, 20000)
        connection.execute("BEGIN")
        watermark = connection.execute("SELECT coalesce(max(rowid),0) FROM events").fetchone()[0]
        months = {}
        month_status = "not_requested"
        if include_months:
            # The counter and optional month scan use separate short snapshots.
            # Bound this guard even if a large import commits between them.
            bounded_total=connection.execute('SELECT count(1) FROM (SELECT 1 FROM events LIMIT 250001)').fetchone()[0]
            month_status = "observed_records_only" if bounded_total<=250000 else 'not_computed_at_request_scale'
            rows=connection.execute("""SELECT source_id,substr(occurred_at,1,7) AS month,count(*) AS records
                    FROM events WHERE length(occurred_at)>=7 AND time_precision NOT IN ('year','unknown')
                    AND substr(occurred_at,1,7) GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
                    AND CAST(substr(occurred_at,6,2) AS INTEGER) BETWEEN 1 AND 12
                    GROUP BY source_id,substr(occurred_at,1,7) ORDER BY month DESC""") if bounded_total<=250000 else []
            for row in rows:
                item = months.setdefault(row["source_id"], {"items": [], "truncated": False})
                if len(item["items"]) < month_limit:
                    item["items"].append({"month": row["month"], "records": row["records"]})
                else:
                    item["truncated"] = True
        result = {"measured_at": _now(),
                  "catalog_max_event_rowid": watermark, "signature": signature,
                  "months": months, "month_status": month_status}
        with _LOCK:
            entry = _CACHE[key]
            entry.result, entry.error = result, None
    except sqlite3.Error as error:
        with _LOCK:
            _CACHE[key].error = "scan_budget_or_catalog_unavailable" if "interrupted" in str(error) else "catalog_summary_unavailable"
    finally:
        if connection is not None:
            connection.close()
        with _LOCK:
            _CACHE[key].pending = False


def _stats(path, watermark, *, include_months, month_limit, cache_ttl_seconds, scan_budget_seconds, refresh, wait):
    info = path.stat()
    key = (str(path.resolve()), info.st_dev, info.st_ino, include_months, month_limit)
    synchronous = False
    pending_future = None
    with _LOCK:
        entry = _CACHE.setdefault(key, _Entry())
        due = refresh or entry.result is None or time.monotonic() - entry.attempted_at >= cache_ttl_seconds
        # Avoid retrying an unavailable large scan on every frontend poll.
        if entry.error and time.monotonic() - entry.attempted_at < cache_ttl_seconds and not refresh:
            due = False
        if due and not entry.pending:
            entry.pending, entry.attempted_at = True, time.monotonic()
            synchronous = wait or watermark <= 100000
            if not synchronous:
                entry.future = _EXECUTOR.submit(_refresh, key, path, include_months, month_limit, scan_budget_seconds)
        if wait and entry.pending and entry.future is not None:
            pending_future = entry.future
    if synchronous:
        _refresh(key, path, include_months, month_limit, scan_budget_seconds)
    elif pending_future is not None:
        try:
            pending_future.result(timeout=scan_budget_seconds + 2)
        except TimeoutError:
            pass
    with _LOCK:
        entry = _CACHE[key]
        return entry.result, entry.pending, entry.error


def _daily(path, watermark, supplied):
    if supplied is not None:
        return supplied
    try:
        from .daily import daily_status
    except ImportError:
        return {"status": "not_built", "watermark_rowid": None, "current_catalog_max_rowid": watermark}
    try:
        return daily_status(catalog_path=path)
    except (sqlite3.Error, OSError, ValueError):
        return {"status": "unavailable", "watermark_rowid": None, "current_catalog_max_rowid": watermark}


def coverage_overview(path=None, *, include_months=False, month_limit=120,
                      refresh=False, wait=False, cache_ttl_seconds=60,
                      scan_budget_seconds=15, index_status=None,
                      classifications=None, backup_status=None):
    """Return catalog-declared retention/progress and separately dated record stats.

    ``wait=True`` waits only for requested bounded monthly presence. Record
    counts always use the cheap durable counter read, never a refresh scan.
    Optional classifications/backup status must come from independently
    verified callers; this endpoint never guesses a service identity or backup.
    """
    path = Path(path or settings.database)
    month_limit = max(1, min(int(month_limit), 240))
    report = {"scope": "observed_imported_records", "continuous_coverage_asserted": False,
              "generated_at": _now(), "sources": [], "active_imports": [], "queue": [], "import_issues": [],
              "backup": backup_status if backup_status is not None else {"status": "not_supplied"},
              "limitations": ["Observed imported records do not establish complete service history or continuous date coverage.",
                              "File retention counts are catalog declarations; payload readback is not performed by this endpoint.",
                              "Import progress counts original input files and their bytes, not expanded retained leaves.",
                              "Observed date bounds retain stored source timestamps, which can have coarse precision; inspect original evidence for precision.",
                              "Counting records are a known subset, not an estimate of the full catalog. Date bounds come from indexed source timestamp seeks."]}
    connection = _read(path)
    try:
        connection.execute("BEGIN")
        watermark = connection.execute("SELECT coalesce(max(rowid),0) FROM events").fetchone()[0]
        source_rows = connection.execute("SELECT id,name,status,queue_order,snapshot_bytes,notes FROM sources ORDER BY queue_order,name,id").fetchall()
        file_groups = connection.execute("SELECT source_id,parse_status,count(*) AS files,coalesce(sum(bytes),0) AS bytes FROM files GROUP BY source_id,parse_status").fetchall()
        import_rows = connection.execute("""SELECT id,source_id,status,total_files,completed_files,total_bytes,
            completed_bytes,events,error,started_at,finished_at,checkpoint FROM imports ORDER BY started_at DESC""").fetchall()
    finally:
        connection.close()
    measured=get_catalog_stats(path)
    stats=measured.get('stats') or {}
    stats_status=stats.get('status') if stats.get('status') in {'current','counting','unavailable'} else 'unavailable'
    current=stats_status=='current'
    total=measured.get('total_events') if current else None
    undated=measured.get('undated_count') if current else None
    by_source=measured.get('by_source') or {}
    data=None;pending=False;error=None
    month_status='not_requested'
    if include_months:
        if total is None:month_status='unavailable_until_counted'
        elif total>250000:month_status='not_computed_at_request_scale'
        else:
            data,pending,error=_stats(path,watermark,include_months=True,month_limit=month_limit,
                cache_ttl_seconds=max(1,float(cache_ttl_seconds)),scan_budget_seconds=max(0.001,float(scan_budget_seconds)),
                refresh=bool(refresh),wait=bool(wait))
            month_status=data['month_status'] if data else 'refreshing' if pending else 'unavailable'
    # The durable counter and current progress have independent short snapshots.
    connection = _read(path)
    try:
        watermark = connection.execute("SELECT coalesce(max(rowid),0) FROM events").fetchone()[0]
    finally:
        connection.close()
    report["indexing"] = {"catalog_max_event_rowid": watermark,
        "coverage_stats": {"status": stats_status, "refreshing": False, "measured_at": stats.get('measured_at'),
                           "catalog_max_event_rowid": stats.get('watermark'),
                           'checkpoint_rowid':stats.get('checkpoint_rowid'),'scanned_records':stats.get('scanned_records'),
                           'counted_events':stats.get('counted_events'),
                           "refresh_interval_seconds": None, "error_code": stats.get('error')},
        "daily": _daily(path, watermark, index_status)}
    months_stale=bool(data and data['signature']!=_signature(path))
    file_stats = {}
    latest_imports = {}
    for row in import_rows:
        latest_imports.setdefault(row["source_id"], row)
    for row in file_groups:
        item = file_stats.setdefault(row["source_id"], {"total": 0, "bytes": 0, "by_parse_status": {}})
        state = row["parse_status"] if row["parse_status"] in FILE_STATES else "other"
        item["total"] += row["files"]; item["bytes"] += row["bytes"]
        item["by_parse_status"][state] = item["by_parse_status"].get(state, 0) + row["files"]
    totals = {"sources": len(source_rows), "files": 0, "bytes": 0, "parsed_files": 0,
              "metadata_only_files": 0, "retained_only_files": 0, "review_files": 0,
              "events": total,
              "dated_events": total-undated if total is not None and undated is not None else None,
              "undated_events": undated,'counted_events':stats.get('counted_events')}
    for source in source_rows:
        sid = source["id"]
        files = file_stats.get(sid, {"total": 0, "bytes": 0, "by_parse_status": {}}).copy()
        states = files["by_parse_status"]
        for name in ("parsed", "partial", "metadata_only", "empty", "excluded", "pending"):
            files[name] = states.get(name, 0)
        files["retained_only"] = states.get("retained", 0)
        files["needs_review"] = sum(states.get(name, 0) for name in REVIEW)
        records = {"status": stats_status, "total": None, "dated": None, "undated": None,
                   "first_observed": None, "last_observed": None,
                   "measured_at": stats.get('measured_at'),'counted_events':None}
        source_stats=by_source.get(sid)
        if source_stats is not None:
            source_total=source_stats.get('total_events') if current else None
            source_undated=source_stats.get('undated_count') if current else None
            records.update(total=source_total,undated=source_undated,
                dated=source_total-source_undated if source_total is not None and source_undated is not None else None,
                counted_events=source_stats.get('counted_events'),
                first_observed=(source_stats.get('date_range') or {}).get('start'),
                last_observed=(source_stats.get('date_range') or {}).get('end'))
        records["months"] = {"status": month_status, "items": [], "truncated": False,
                             'refreshing':pending,'error_code':error,'measured_at':data['measured_at'] if data else None}
        if data:
            records["months"].update(data["months"].get(sid, {}))
            if months_stale:records['months']['status']='stale'
        status = source["status"]
        if status == "ready" and files["needs_review"]:
            status = "partial"
        item = {"id": sid, "name": source["name"], "status": status, "queue_order": source["queue_order"],
                "notes": _notes(source["notes"]), "files": files, "records": records,
                "extraction": {"review_files": files["needs_review"], "pending_files": files["pending"],
                               "history_absence_established": False},
                "classification": (classifications or {}).get(sid, {"status": "source_label_only", "label": source["name"]})}
        latest = latest_imports.get(sid)
        if latest is not None:
            item["latest_import"] = {"id": latest["id"], "status": latest["status"],
                "started_at": latest["started_at"], "finished_at": latest["finished_at"],
                "error": _safe_error(latest["error"], latest["status"])}
            if latest["error"]:
                report["import_issues"].append({"id": latest["id"], "source_id": sid, "source": source["name"],
                                                "status": latest["status"], "error": _safe_error(latest["error"], latest["status"])})
        report["sources"].append(item)
        for name in ("files", "bytes"):
            totals[name] += files["total" if name == "files" else "bytes"]
        for total_key, source_key in (("parsed_files", "parsed"), ("metadata_only_files", "metadata_only"), ("retained_only_files", "retained_only"), ("review_files", "needs_review")):
            totals[total_key] += files[source_key]
        if source["status"] in {"queued", "pending", "paused", "failed"}:
            report["queue"].append({"source_id": sid, "source": source["name"], "status": source["status"],
                                    "position": len(report["queue"]) + 1, "snapshot_bytes": source["snapshot_bytes"],
                                    "error": _safe_error(latest["error"], latest["status"]) if latest else None})
    names = {row["id"]: row["name"] for row in source_rows}
    for row in import_rows:
        if row["status"] not in ACTIVE:
            continue
        report["active_imports"].append({"id": row["id"], "source_id": row["source_id"], "source": names.get(row["source_id"], "Source"),
            "status": row["status"], "files": _progress(row["completed_files"], row["total_files"]),
            "bytes": _progress(row["completed_bytes"], row["total_bytes"]), "events_reported": row["events"],
            "activity": public_activity(row["checkpoint"]),
            "started_at": row["started_at"], "error": _safe_error(row["error"], row["status"])})
    report["totals"] = totals
    return report


__all__ = ["coverage_overview"]
