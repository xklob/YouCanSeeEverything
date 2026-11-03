import sqlite3
import threading
from pathlib import Path

import pytest

from biography.db import init_db
from biography import query_stats as stats


def catalog(tmp_path, *, name="catalog.sqlite3", samples=0):
    path = tmp_path / name
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)", [
            ("journal", "Example journal", "2025-01-01", "2025-01-01"),
            ("empty", "Example empty export", "2025-01-01", "2025-01-01")])
        rows = [("one", "journal", "Example", "2024-01-03", "note", "2025-01-01"),
                ("two", "journal", "Undated", None, "note", "2025-01-01"),
                ("three", "journal", "A sample", "2024-02-06", "sample", "2025-01-01")]
        rows.extend((f"sample-{i}", "journal", "Synthetic sample", "2024-02-06", "sample", "2025-01-01") for i in range(samples))
        conn.executemany("INSERT INTO events(id,source_id,title,occurred_at,category,created_at) VALUES(?,?,?,?,?,?)", rows)
    return path


@pytest.fixture
def cache():
    value = stats.CatalogStatsCache()
    yield value
    value.close()


def settle(cache):
    cache._executor.submit(lambda: None).result(timeout=10)


def test_cold_requests_return_unknown_and_polls_are_single_flight(tmp_path, cache, monkeypatch):
    path = catalog(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = cache._catalog
    calls = []

    def blocked(reader, value):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        original(reader, value)

    monkeypatch.setattr(cache, "_catalog", blocked)
    try:
        cold = cache.catalog(path)
        assert cold["total_events"] is None
        assert cold["stats"]["status"] == "pending"
        assert entered.wait(2)
        for _ in range(10):
            assert cache.catalog(path)["stats"]["refreshing"] is True
        assert len(calls) == 1
    finally:
        release.set()
    settle(cache)
    warm = cache.catalog(path)
    assert warm["total_events"] == 3
    assert warm["undated_count"] == 1
    assert warm["date_range"] == {"start": "2024-01-03", "end": "2024-02-06"}
    assert warm["categories"] == [{"name": "note", "count": 2}, {"name": "sample", "count": 1}]
    assert warm["activity"] is None
    assert warm["stats"]["activity_status"] == "not_computed"
    assert warm["stats"]["watermark"] == 3
    assert warm["by_source"]["empty"]["total_events"] == 0


def test_measurement_is_one_snapshot_and_subsequent_writes_are_stale(tmp_path, cache, monkeypatch):
    path = catalog(tmp_path)
    original = cache._catalog

    def concurrent_write(reader, value):
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('later','Later export','2025','2025')")
            conn.execute("INSERT INTO events(id,source_id,title,occurred_at,category,created_at) VALUES('later','later','Later record','2025-01-01','note','2025')")
        original(reader, value)

    monkeypatch.setattr(cache, "_catalog", concurrent_write)
    cache.catalog(path)
    settle(cache)
    report = cache.catalog(path)
    assert report["stats"]["status"] == "stale"
    assert report["total_events"] == 3
    assert report["stats"]["watermark"] == 3
    assert "later" not in report["by_source"]
    assert report["stats"]["refreshing"] is False
    assert cache.count(path)["total_status"] == "stale"


def test_filtered_count_is_async_and_parameterized(tmp_path, cache):
    path = catalog(tmp_path)
    clause = " WHERE e.category=? AND e.occurred_at>=?"
    args = ["note", "2024-01-01"]
    assert cache.count(path, clause, args) == {"total": None, "total_status": "pending", "counted_at": None}
    settle(cache)
    counted = cache.count(path, clause, args)
    assert counted["total"] == 1
    assert counted["total_status"] == "current"
    assert counted["counted_at"]
    # SQL-looking values stay parameters and cannot become executable clauses.
    cache.count(path, " WHERE e.source_id=?", ["journal' OR 1=1 --"])
    settle(cache)
    assert cache.count(path, " WHERE e.source_id=?", ["journal' OR 1=1 --"])["total"] == 0
    with pytest.raises(ValueError):
        cache.count(path, "WHERE 1; DELETE FROM events", [])


def test_noncovering_filtered_count_stays_unknown(tmp_path, cache):
    path = catalog(tmp_path)
    clause, args = " WHERE e.source_id=? AND e.category=?", ["journal", "note"]
    cache.count(path, clause, args)
    settle(cache)
    assert cache.count(path, clause, args) == {"total": None, "total_status": "unavailable", "counted_at": None}


def test_opcode_budget_retains_category_names_and_fast_bounds(tmp_path):
    path = catalog(tmp_path, samples=2000)
    cache = stats.CatalogStatsCache(max_opcodes=4000)
    try:
        cache.catalog(path)
        settle(cache)
        report = cache.catalog(path)
        assert report["total_events"] is None
        assert report["by_source"] is None
        assert report["date_range"]["start"] == "2024-01-03"
        assert report["categories"] == [{"name": "note", "count": None}, {"name": "sample", "count": None}]
        assert report["stats"]["status"] == "partial"
        assert report["stats"]["error"] == "opcode_budget_exceeded"
        assert report["stats"]["categories_status"] == "names_only"
        # Polling a failed scan does not immediately restart the same expensive work.
        assert report["stats"]["refreshing"] is False
    finally:
        cache.close()


def test_elapsed_budget_is_bounded_without_faking_zero(tmp_path):
    path = catalog(tmp_path)
    cache = stats.CatalogStatsCache(query_seconds=0)
    try:
        cache.catalog(path)
        settle(cache)
        result = cache.catalog(path)
        assert result["total_events"] is None
        assert result["stats"]["status"] == "unavailable"
        assert result["stats"]["error"] == "query_budget_exceeded"
    finally:
        cache.close()


def test_wal_growth_guard_does_not_checkpoint_or_change_source(tmp_path, cache, monkeypatch):
    path = catalog(tmp_path)
    before = path.read_bytes()
    reader = stats._Reader(path, cache)
    try:
        reader.last_wal_check = 0
        monkeypatch.setattr(stats, "_wal_size", lambda path: reader.wal_start + cache.max_wal_growth_bytes + 1)
        with pytest.raises(stats._BudgetExceeded, match="wal_growth_budget_exceeded"):
            reader.execute("BEGIN")
    finally:
        reader.close()
    assert path.read_bytes() == before


def test_catalog_replacement_never_reuses_previous_counts(tmp_path, cache):
    path = catalog(tmp_path)
    cache.catalog(path)
    settle(cache)
    assert cache.catalog(path)["total_events"] == 3
    replacement = catalog(tmp_path, name="replacement.sqlite3", samples=5)
    # Close fixture connections and checkpoint fixture-only WAL before replacement.
    for fixture in (path, replacement):
        connection = sqlite3.connect(fixture)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
    replacement.replace(path)
    assert cache.catalog(path)["total_events"] is None
    settle(cache)
    assert cache.catalog(path)["total_events"] == 8


def test_no_catalog_writes_or_schema_changes(tmp_path, cache, monkeypatch):
    path = catalog(tmp_path)
    captured = []
    original = stats._Reader.__init__

    def trace(self, *args, **kwargs):
        original(self, *args, **kwargs)
        assert self.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        self.conn.set_trace_callback(captured.append)

    monkeypatch.setattr(stats._Reader, "__init__", trace)
    with sqlite3.connect(path) as conn:
        schema = conn.execute("SELECT sql FROM sqlite_schema ORDER BY name").fetchall()
    cache.catalog(path)
    settle(cache)
    assert all(not sql.upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "PRAGMA WAL_CHECKPOINT")) for sql in captured)
    assert all("COUNT(*)" not in sql.upper() for sql in captured)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT sql FROM sqlite_schema ORDER BY name").fetchall() == schema


def test_refresh_failure_preserves_old_measurement_and_age(tmp_path, cache, monkeypatch):
    path = catalog(tmp_path)
    cache.catalog(path)
    settle(cache)
    first = cache.catalog(path)
    with cache._lock:
        for entry in cache._entries.values():
            entry.attempted_at -= 1000
            entry.measured_monotonic -= 1000

    def blocked(reader, value):
        raise stats._BudgetExceeded("query_budget_exceeded")

    monkeypatch.setattr(cache, "_catalog", blocked)
    assert cache.catalog(path)["stats"]["status"] == "stale"
    settle(cache)
    again = cache.catalog(path)
    assert again["total_events"] == 3
    assert again["stats"]["status"] == "stale"
    assert again["stats"]["measured_at"] == first["stats"]["measured_at"]
    assert again["stats"]["refreshing"] is False


def test_unknown_path_is_not_created(tmp_path, cache):
    path = tmp_path / "absent.sqlite3"
    report = cache.catalog(path)
    assert report["total_events"] is None
    assert report["stats"]["status"] == "unavailable"
    assert not path.exists()


def test_queue_and_cache_are_bounded_across_distinct_requests(tmp_path, monkeypatch):
    path = catalog(tmp_path)
    cache = stats.CatalogStatsCache(max_pending=2, max_entries=2)
    entered, release = threading.Event(), threading.Event()
    original = cache._catalog

    def blocked(reader, value):
        entered.set()
        assert release.wait(5)
        original(reader, value)

    monkeypatch.setattr(cache, "_catalog", blocked)
    try:
        cache.catalog(path)
        assert entered.wait(2)
        assert cache.count(path, " WHERE e.category=?", ["note"])["total_status"] == "pending"
        for number in range(10):
            assert cache.count(path, " WHERE e.source_id=?", [f"different-{number}"])["total"] is None
        assert cache._pending == 2
        assert len(cache._entries) == 2
        release.set()
        settle(cache)
        assert cache.count(path, " WHERE e.category=?", ["note"])["total"] == 2
    finally:
        release.set()
        cache.close()


def test_aggregate_plans_are_covering_and_counts_are_interruptible(tmp_path):
    path = catalog(tmp_path)
    statements = [
        "SELECT source_id,count(1),sum(occurred_at IS NULL),min(occurred_at),max(occurred_at) FROM events INDEXED BY events_source GROUP BY source_id",
        "SELECT category,count(1) FROM events INDEXED BY events_category GROUP BY category",
        "SELECT count(1) FROM events e WHERE e.category='note'",
    ]
    with sqlite3.connect(path) as conn:
        for sql in statements:
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
            assert any("COVERING INDEX" in detail for detail in plan)
            assert all("TEMP B-TREE" not in detail for detail in plan)
            assert "Count" not in {row[1] for row in conn.execute("EXPLAIN " + sql)}
