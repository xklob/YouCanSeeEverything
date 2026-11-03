"""Cheap durable catalog totals and bounded asynchronous filtered counts.

Catalog reads use transactionally maintained aggregates. Complex filters use
a disposable single-flight cache; a missed measurement remains unknown.
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
from .catalog_counts import count_status


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


class CatalogStatsCache:
    """One bounded worker for complex filtered counts in this process."""

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
        value = {}
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

    def _count(self, reader, value, where, args):
        sql = "SELECT count(1) FROM events e" + where
        plan = reader.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()
        event_access = [row[3] for row in plan if row[3].startswith(("SCAN e ", "SEARCH e "))]
        if not event_access or any("COVERING INDEX" not in detail for detail in event_access):
            raise _BudgetExceeded("count_requires_noncovering_scan")
        value["total"] = reader.execute(sql, args).fetchone()[0]

    def catalog(self, path=None):
        # Durable aggregates change in the same transaction as events. Do not
        # hide progress or current totals behind the filtered-count cache TTL.
        return count_status(path)

    def count(self, path=None, where="", args=()):
        where = where.strip()
        simple = {'WHERE e.source_id=?': 'source', 'WHERE e.category=?': 'category',
                  'WHERE e.occurred_at IS NULL': 'undated'}
        if where and (not where.upper().startswith("WHERE ") or ";" in where or "--" in where or "/*" in where):
            raise ValueError("A single parameterized event WHERE clause is required")
        kind = simple.get(where)
        if kind in {'source', 'category'} and len(args) != 1:
            raise ValueError('One count filter parameter is required')
        # TEXT affinity can coerce numeric SQL parameters; use the SQL worker
        # for those rather than a Python dictionary lookup with different rules.
        simple_key = kind == 'undated' or (kind in {'source', 'category'} and
                     (isinstance(args[0], str) or args[0] is None))
        if not where or simple_key:
            result = count_status(path, bounds=False)
            stats = result['stats']
            total, counted = result['total_events'], stats['counted_events']
            kind = simple.get(where)
            if kind in {'source', 'category'}:
                if kind == 'source':
                    value = (result['by_source'] or {}).get(args[0])
                    total = value['total_events'] if value else 0 if stats['status'] == 'current' else None
                    counted = value['counted_events'] if value else 0 if stats['counted_events'] is not None else None
                else:
                    value = next((v for v in result['categories'] or [] if v['name'] == args[0]), None)
                    total = value['count'] if value else 0 if stats['status'] == 'current' else None
                    counted = value['counted_events'] if value else 0 if stats['counted_events'] is not None else None
            elif kind == 'undated':
                total, counted = result['undated_count'], None
            answer = {'total': total, 'total_status': 'current' if total is not None else
                      'counting' if stats['status'] == 'counting' else 'unavailable',
                      'counted_at': stats['measured_at'] if total is not None else None}
            if total is None and stats['status'] == 'counting':
                answer['counted_events'] = counted
            return answer
        value, pending, error, stale = self._request(path, "count", " " + where, args)
        total = value.get("total") if value else None
        return {"total": total, "total_status": "stale" if total is not None and stale else "current" if total is not None else "pending" if pending else "unavailable",
                "counted_at": value.get("measured_at") if total is not None else None}


_CACHE = CatalogStatsCache()


def get_catalog_stats(path=None):
    return _CACHE.catalog(path)


def get_count(path=None, where="", args=()):
    return _CACHE.count(path, where, args)
