import sqlite3
import threading
from contextlib import closing

import pytest

from biography.db import init_db, connect, SCHEMA
from biography import query_stats as stats
from biography.catalog_counts import build_counts


def catalog(tmp_path, *, name='catalog.sqlite3', samples=0, legacy=False):
    path = tmp_path / name
    if legacy:
        with closing(connect(path)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)
    else:
        init_db(path)
    with closing(connect(path)) as conn, conn:
        conn.executemany('INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)', [
            ('journal', 'Example journal', '2025-01-01', '2025-01-01'),
            ('empty', 'Example empty export', '2025-01-01', '2025-01-01')])
        rows = [('one', 'journal', 'Example', '2024-01-03', 'note', '2025-01-01'),
                ('two', 'journal', 'Undated', None, 'note', '2025-01-01'),
                ('three', 'journal', 'A sample', '2024-02-06', 'sample', '2025-01-01')]
        rows.extend((f'sample-{i}', 'journal', 'Synthetic sample', '2024-02-06', 'sample', '2025-01-01') for i in range(samples))
        conn.executemany('INSERT INTO events(id,source_id,title,occurred_at,category,created_at) VALUES(?,?,?,?,?,?)', rows)
    return path


@pytest.fixture
def cache():
    value = stats.CatalogStatsCache()
    yield value
    value.close()


def settle(cache):
    cache._executor.submit(lambda: None).result(timeout=10)


def test_initialized_totals_are_exact_on_first_request_without_count_scan(tmp_path, cache):
    path = catalog(tmp_path)
    result = cache.catalog(path)
    assert result['total_events'] == 3
    assert result['undated_count'] == 1
    assert result['stats']['status'] == 'current'
    assert result['date_range'] == {'start': '2024-01-03', 'end': '2024-02-06'}
    assert result['categories'] == [{'name': 'note', 'count': 2, 'counted_events': 2},
                                    {'name': 'sample', 'count': 1, 'counted_events': 1}]
    assert result['activity'] is None
    assert result['by_source']['empty']['total_events'] == 0
    assert cache._pending == 0


def test_legacy_progress_is_separate_and_refresh_never_waits_for_cache_ttl(tmp_path, cache):
    path = catalog(tmp_path, legacy=True)
    assert cache.catalog(path)['total_events'] is None
    assert cache.catalog(path)['stats']['status'] == 'unavailable'
    build_counts(path, batch_size=1)
    first = cache.catalog(path)
    assert first['stats']['status'] == 'counting'
    assert first['total_events'] is None
    assert first['stats']['counted_events'] == 1
    assert first['categories'] == [{'name': 'note', 'count': None, 'counted_events': 1},
                                   {'name': 'sample', 'count': None, 'counted_events': 0}]
    assert cache.count(path) == {'total': None, 'total_status': 'counting',
                                'counted_at': None, 'counted_events': 1}
    assert cache.count(path, 'WHERE e.source_id=?', ['empty'])['total'] is None
    build_counts(path, batch_size=10)
    assert cache.catalog(path)['total_events'] == 3
    with closing(connect(path)) as conn, conn:
        conn.execute("DELETE FROM events WHERE id='one'")
    assert cache.catalog(path)['total_events'] == 2
    assert cache.count(path, 'WHERE e.category=?', ['note'])['total'] == 1


def test_filtered_count_is_async_single_flight_and_parameterized(tmp_path, cache, monkeypatch):
    path = catalog(tmp_path)
    clause, args = ' WHERE e.category=? AND e.occurred_at>=?', ['note', '2024-01-01']
    entered, release = threading.Event(), threading.Event()
    original, calls = cache._count, []
    def blocked(*arguments):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        original(*arguments)
    monkeypatch.setattr(cache, '_count', blocked)
    try:
        assert cache.count(path, clause, args) == {'total': None, 'total_status': 'pending', 'counted_at': None}
        assert entered.wait(2)
        for _ in range(10):
            assert cache.count(path, clause, args)['total_status'] == 'pending'
        assert len(calls) == 1
    finally:
        release.set()
    settle(cache)
    assert cache.count(path, clause, args)['total'] == 1
    assert cache.count(path, ' WHERE e.source_id=?', ["journal' OR 1=1 --"])['total'] == 0
    with pytest.raises(ValueError):
        cache.count(path, 'WHERE 1; DELETE FROM events', [])


def test_noncovering_filtered_count_stays_unknown(tmp_path, cache):
    path = catalog(tmp_path)
    clause, args = ' WHERE e.source_id=? AND e.category=?', ['journal', 'note']
    cache.count(path, clause, args)
    settle(cache)
    assert cache.count(path, clause, args) == {'total': None, 'total_status': 'unavailable', 'counted_at': None}


def test_big_catalog_totals_are_independent_of_filtered_count_budget(tmp_path):
    path = catalog(tmp_path, samples=2000)
    cache = stats.CatalogStatsCache(max_opcodes=4000)
    try:
        assert cache.catalog(path)['total_events'] == 2003
        clause, args = ' WHERE e.category=? AND e.occurred_at>=?', ['sample', '2024']
        cache.count(path, clause, args)
        settle(cache)
        result = cache.count(path, clause, args)
        assert result['total'] is None
        assert result['total_status'] == 'unavailable'
        assert cache._pending == 0
    finally:
        cache.close()


def test_elapsed_filtered_budget_is_bounded_without_faking_zero(tmp_path):
    path = catalog(tmp_path)
    cache = stats.CatalogStatsCache(query_seconds=0)
    try:
        clause, args = ' WHERE e.category=? AND e.occurred_at>=?', ['note', '2024']
        cache.count(path, clause, args)
        settle(cache)
        assert cache.count(path, clause, args)['total'] is None
        assert cache.catalog(path)['total_events'] == 3
    finally:
        cache.close()


def test_wal_growth_guard_does_not_checkpoint_or_change_source(tmp_path, cache, monkeypatch):
    path = catalog(tmp_path)
    before = path.read_bytes()
    reader = stats._Reader(path, cache)
    try:
        reader.last_wal_check = 0
        monkeypatch.setattr(stats, '_wal_size', lambda path: reader.wal_start + cache.max_wal_growth_bytes + 1)
        with pytest.raises(stats._BudgetExceeded, match='wal_growth_budget_exceeded'):
            reader.execute('BEGIN')
    finally:
        reader.close()
    assert path.read_bytes() == before


def test_catalog_replacement_never_reuses_previous_counts(tmp_path, cache):
    path = catalog(tmp_path)
    assert cache.catalog(path)['total_events'] == 3
    replacement = catalog(tmp_path, name='replacement.sqlite3', samples=5)
    for fixture in (path, replacement):
        with closing(sqlite3.connect(fixture)) as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    replacement.replace(path)
    assert cache.catalog(path)['total_events'] == 8


def test_no_catalog_writes_or_schema_changes(tmp_path, cache, monkeypatch):
    from biography import catalog_counts
    path = catalog(tmp_path)
    captured = []
    original = catalog_counts.read_counts
    def trace(conn, **kwargs):
        assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
        conn.set_trace_callback(captured.append)
        return original(conn, **kwargs)
    monkeypatch.setattr(catalog_counts, 'read_counts', trace)
    with closing(connect(path)) as conn:
        schema = conn.execute('SELECT sql FROM sqlite_schema ORDER BY name').fetchall()
    assert cache.catalog(path)['total_events'] == 3
    assert all(not sql.upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER', 'PRAGMA WAL_CHECKPOINT')) for sql in captured)
    assert all('COUNT(' not in sql.upper() and 'GROUP BY' not in sql.upper() for sql in captured)
    with closing(connect(path)) as conn:
        assert conn.execute('SELECT sql FROM sqlite_schema ORDER BY name').fetchall() == schema


def test_filtered_refresh_failure_preserves_old_measurement_and_age(tmp_path, cache, monkeypatch):
    path = catalog(tmp_path)
    clause, args = ' WHERE e.category=? AND e.occurred_at>=?', ['note', '2024']
    cache.count(path, clause, args)
    settle(cache)
    first = cache.count(path, clause, args)
    with cache._lock:
        for entry in cache._entries.values():
            entry.attempted_at -= 1000
            entry.measured_monotonic -= 1000
    def blocked(*args):
        raise stats._BudgetExceeded('query_budget_exceeded')
    monkeypatch.setattr(cache, '_count', blocked)
    assert cache.count(path, clause, args)['total_status'] == 'stale'
    settle(cache)
    again = cache.count(path, clause, args)
    assert again['total'] == 1
    assert again['total_status'] == 'stale'
    assert again['counted_at'] == first['counted_at']


def test_unknown_path_is_not_created(tmp_path, cache):
    path = tmp_path / 'absent.sqlite3'
    report = cache.catalog(path)
    assert report['total_events'] is None
    assert report['stats']['status'] == 'unavailable'
    assert not path.exists()


def test_queue_and_cache_are_bounded_across_distinct_filters(tmp_path, monkeypatch):
    path = catalog(tmp_path)
    cache = stats.CatalogStatsCache(max_pending=2, max_entries=2)
    entered, release = threading.Event(), threading.Event()
    original = cache._count
    def blocked(*arguments):
        entered.set()
        assert release.wait(5)
        original(*arguments)
    monkeypatch.setattr(cache, '_count', blocked)
    try:
        clause = ' WHERE e.category=? AND e.occurred_at>=?'
        cache.count(path, clause, ['note', '2024'])
        assert entered.wait(2)
        for number in range(10):
            assert cache.count(path, clause, [f'different-{number}', '2024'])['total'] is None
        assert cache._pending == 2
        assert len(cache._entries) == 2
        # Expensive filtered work cannot block exact catalog/source counters.
        assert cache.catalog(path)['total_events'] == 3
        assert cache.count(path, 'WHERE e.source_id=?', ['journal'])['total'] == 3
    finally:
        release.set()
        cache.close()


def test_async_count_is_covering_and_interruptible(tmp_path):
    path = catalog(tmp_path)
    sql = "SELECT count(1) FROM events e WHERE e.category='note' AND e.occurred_at>='2024'"
    with closing(connect(path)) as conn:
        plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql)]
        assert any('COVERING INDEX' in detail for detail in plan)
        assert all('TEMP B-TREE' not in detail for detail in plan)
        assert 'Count' not in {row[1] for row in conn.execute('EXPLAIN ' + sql)}
