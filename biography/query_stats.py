"""Bounded, asynchronous measurements for catalog browsing.

Requests never wait for a count. Measurements describe a committed SQLite
snapshot, not an estimate of the current catalog. This disposable in-memory
cache writes neither the catalog nor personal data to the application checkout.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
import time

from .config import settings


def _now():
    return datetime.now(timezone.utc).isoformat()


def _signature(path):
    values = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
            values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            values.append(None)
    return tuple(values)


def _wal_size(path):
    try:
        return Path(str(path) + "-wal").stat().st_size
    except FileNotFoundError:
        return 0


class _BudgetExceeded(Exception):
    pass


class _Reader:
    def __init__(self, path, cache, *, count_only=False):
        self.path, self.cache = path, cache
        self.started = time.monotonic()
        self.query_seconds = min(cache.query_seconds, 2) if count_only else cache.query_seconds
        self.wal_start = _wal_size(path)
        self.last_wal_check = self.started
        self.opcodes = 0
        self.reason = None
        self.conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA query_only=ON")
            self.conn.set_progress_handler(self._progress, 4000)
        except Exception:
            self.conn.close()
            raise

    def check(self):
        current = time.monotonic()
        if current - self.started >= self.query_seconds:
            self.reason = "query_budget_exceeded"
        elif self.opcodes >= self.cache.max_opcodes:
            self.reason = "opcode_budget_exceeded"
        elif current - self.last_wal_check >= 0.05:
            self.last_wal_check = current
            if _wal_size(self.path) - self.wal_start > self.cache.max_wal_growth_bytes:
                self.reason = "wal_growth_budget_exceeded"
        if self.reason:
            raise _BudgetExceeded(self.reason)

    def _progress(self):
        self.opcodes += 4000
        try:
            self.check()
            return 0
        except (OSError, _BudgetExceeded):
            self.reason = self.reason or "catalog_unavailable"
            return 1

    def execute(self, sql, args=()):
        self.check()
        return self.conn.execute(sql, args)

    def rows(self, sql, args=()):
        result = []
        for row in self.execute(sql, args):
            self.check()
            if len(result) >= self.cache.max_summary_rows:
                raise _BudgetExceeded("summary_result_limit")
            result.append(dict(row))
        return result

    def close(self):
        self.conn.close()  # Releases the read snapshot, including error paths.


@dataclass
class _Entry:
    attempted_at: float = 0
    measured_monotonic: float = 0
    pending: bool = False
    value: dict | None = None
    signature: tuple | None = None
    error: str | None = None


def _empty_catalog():
    return {"total_events": None, "undated_count": None,
            "date_range": {"start": None, "end": None}, "activity": None,
            "categories": None, "by_source": None}


class CatalogStatsCache:
    """One bounded worker for all summaries and filtered counts in this process."""

    def __init__(self, *, ttl_seconds=300, query_seconds=8,
                 max_opcodes=500_000_000, max_wal_growth_bytes=256 * 1024**2,
                 max_entries=128, max_pending=4, max_summary_rows=10000):
        self.ttl_seconds, self.query_seconds = ttl_seconds, query_seconds
        self.max_opcodes, self.max_wal_growth_bytes = max_opcodes, max_wal_growth_bytes
        self.max_entries, self.max_pending = max_entries, max_pending
        self.max_summary_rows = max_summary_rows
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._pending = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog-stats")

    def close(self):
        self._executor.shutdown(wait=True)

    def _request(self, path, kind, where="", args=()):
        path = Path(path or settings.database).expanduser().resolve()
        try:
            stat = path.stat()
            signature = _signature(path)
        except OSError:
            return None, False, "catalog_unavailable", False
        key = (str(path), stat.st_dev, stat.st_ino, kind, where, tuple(args))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                # Never evict running/queued work and create duplicate workers.
                while len(self._entries) >= self.max_entries:
                    old = next((k for k, v in self._entries.items() if not v.pending), None)
                    if old is None:
                        return None, False, "cache_busy", False
                    del self._entries[old]
                entry = self._entries[key] = _Entry()
            self._entries.move_to_end(key)
            age = time.monotonic() - entry.attempted_at
            # Copy before submission: even a fast worker never makes a cold
            # request's result depend on scheduler timing.
            value = deepcopy(entry.value)
            stale = bool(value is not None and (entry.signature != signature or time.monotonic() - entry.measured_monotonic >= self.ttl_seconds))
            due = entry.attempted_at == 0 or age >= self.ttl_seconds
            if due and not entry.pending and self._pending < self.max_pending:
                entry.attempted_at = time.monotonic()
                entry.pending = True
                self._pending += 1
                self._executor.submit(self._run, key, path, kind, where, tuple(args))
            return value, entry.pending, entry.error, stale

    def _run(self, key, path, kind, where, args):
        reader = None
        value = _empty_catalog() if kind == "catalog" else {}
        error = None
        signature = None
        try:
            signature = _signature(path)
            # The queued path may have been atomically replaced before opening.
            if signature[0] is None or signature[0][:2] != key[1:3]:
                raise _BudgetExceeded("catalog_replaced")
            reader = _Reader(path, self, count_only=kind == "count")
            reader.execute("BEGIN")
            watermark = reader.execute("SELECT rowid FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
            value.update(measured_at=_now(), watermark=watermark[0] if watermark else 0)
            if kind == "catalog":
                self._catalog(reader, value)
            else:
                self._count(reader, value, where, args)
        except (sqlite3.Error, OSError, _BudgetExceeded) as exc:
            if isinstance(exc, _BudgetExceeded):
                error = str(exc)
            elif reader is not None and reader.reason:
                error = reader.reason
            elif isinstance(exc, sqlite3.Error) and getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                error = "catalog_busy"
            else:
                error = "catalog_unavailable"
        finally:
            if reader is not None:
                reader.close()
            with self._lock:
                entry = self._entries[key]
                # Keep an earlier complete measurement when refresh is blocked.
                # Partial measurements are useful only when no earlier data exists.
                if not error or entry.value is None:
                    entry.value = value if "measured_at" in value else None
                    entry.signature = signature
                    entry.measured_monotonic = time.monotonic()
                entry.error, entry.pending = error, False
                self._pending -= 1

    def _catalog(self, reader, value):
        # Bounds are two covering-index seeks, available even if aggregates hit
        # their budget. Empty catalogs have measured null bounds and zero counts.
        first = reader.execute("SELECT occurred_at FROM events INDEXED BY events_time WHERE occurred_at IS NOT NULL ORDER BY occurred_at ASC LIMIT 1").fetchone()
        last = reader.execute("SELECT occurred_at FROM events INDEXED BY events_time WHERE occurred_at IS NOT NULL ORDER BY occurred_at DESC LIMIT 1").fetchone()
        value["date_range"] = {"start": first[0] if first else None, "end": last[0] if last else None}
        # Distinct names via successive seeks avoid a DISTINCT scan across every
        # sample. Counts stay unknown until the category aggregate completes.
        value["categories"], value["categories_status"] = [], "names_partial"
        previous = None
        while True:
            row = reader.execute("SELECT category FROM events INDEXED BY events_category " +
                ("WHERE category>? " if previous is not None else "") +
                "ORDER BY category LIMIT 1", (previous,) if previous is not None else ()).fetchone()
            if row is None:
                break
            if len(value["categories"]) >= self.max_summary_rows:
                raise _BudgetExceeded("summary_result_limit")
            previous = row[0]
            value["categories"].append({"name": previous, "count": None})
        value["categories_status"] = "names_only"
        sources = reader.rows("SELECT id FROM sources")
        # count(1), unlike SQLite's count(*) fast path, is interruptible by the
        # opcode progress handler. Only source/date index leaves are visited.
        rows = reader.rows("""SELECT source_id,count(1) AS total_events,
            sum(occurred_at IS NULL) AS undated_count,
            min(occurred_at) AS first_event,max(occurred_at) AS last_event
            FROM events INDEXED BY events_source GROUP BY source_id""")
        by_source = {row["id"]: {"total_events": 0, "undated_count": 0,
                     "date_range": {"start": None, "end": None}} for row in sources}
        for row in rows:
            by_source[row["source_id"]] = {"total_events": row["total_events"],
                "undated_count": row["undated_count"],
                "date_range": {"start": row["first_event"], "end": row["last_event"]}}
        value.update(total_events=sum(row["total_events"] for row in rows),
                     undated_count=sum(row["undated_count"] for row in rows), by_source=by_source)
        categories = reader.rows("SELECT category AS name,count(1) AS count FROM events INDEXED BY events_category GROUP BY category")
        value["categories"] = sorted(categories, key=lambda row: (-row["count"], row["name"]))
        value["categories_status"] = "measured"
        # Monthly grouping needs an unindexed expression and a temp sort at this
        # scale. Daily projection owns that browsing; do not imply zero activity.

    def _count(self, reader, value, where, args):
        sql = "SELECT count(1) FROM events e" + where
        plan = reader.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()
        event_access = [row[3] for row in plan if row[3].startswith(("SCAN e ", "SEARCH e "))]
        if not event_access or any("COVERING INDEX" not in detail for detail in event_access):
            raise _BudgetExceeded("count_requires_noncovering_scan")
        value["total"] = reader.execute(sql, args).fetchone()[0]

    def catalog(self, path=None):
        value, pending, error, stale = self._request(path, "catalog")
        result = _empty_catalog() if value is None else {k: v for k, v in value.items() if k not in {"measured_at", "watermark", "categories_status"}}
        status = "stale" if stale else "partial" if value is not None and error else "ready" if value is not None else "pending" if pending else "unavailable"
        result["stats"] = {"status": status, "measured_at": value.get("measured_at") if value else None,
            "watermark": value.get("watermark") if value else None, "refreshing": pending,
            "activity_status": "not_computed", "categories_status": value.get("categories_status", "unknown") if value else "unknown", "error": error}
        return result

    def count(self, path=None, where="", args=()):
        where = where.strip()
        if not where:
            result = self.catalog(path)
            stats = result["stats"]
            total = result["total_events"]
            return {"total": total, "total_status": "stale" if total is not None and stats["status"] == "stale" else "current" if total is not None else "pending" if stats["refreshing"] else "unavailable",
                    "counted_at": stats["measured_at"] if total is not None else None}
        if not where.upper().startswith("WHERE ") or ";" in where or "--" in where or "/*" in where:
            raise ValueError("A single parameterized event WHERE clause is required")
        value, pending, error, stale = self._request(path, "count", " " + where, args)
        total = value.get("total") if value else None
        return {"total": total, "total_status": "stale" if total is not None and stale else "current" if total is not None else "pending" if pending else "unavailable",
                "counted_at": value.get("measured_at") if total is not None else None}


_CACHE = CatalogStatsCache()


def get_catalog_stats(path=None):
    return _CACHE.catalog(path)


def get_count(path=None, where="", args=()):
    return _CACHE.count(path, where, args)
