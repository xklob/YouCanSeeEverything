"""Synthetic mutation/backfill tests; no personal catalog is opened."""
import sqlite3
from contextlib import closing

import pytest

from biography import catalog_counts as counts
from biography.db import SCHEMA, connect, init_db


def insert(conn, eid, *, rowid=None, source='a', category='note', date=None, mode='INSERT'):
    columns = 'id,source_id,title,category,occurred_at,created_at'
    values = [eid, source, eid, category, date, '2025']
    if rowid is not None:
        columns, values = 'rowid,' + columns, [rowid, *values]
    conn.execute(f'{mode} INTO events({columns}) VALUES({",".join("?" for _ in values)})', values)


def fixture(tmp_path, *, legacy=False, n=0):
    path = tmp_path / 'catalog.sqlite3'
    with closing(connect(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                         [(key, key, '2025', '2025') for key in ('a', 'b', 'empty')])
        for i in range(n):
            insert(conn, str(i), date='2025-01-01' if i % 2 else None)
        conn.commit()
        if not legacy:
            counts.ensure_counts(conn)
    return path


def actual(path):
    with closing(connect(path)) as conn:
        return conn.execute('SELECT count(1),sum(occurred_at IS NULL) FROM events').fetchone()


def finish(path, **kwargs):
    for _ in range(100):
        result = counts.build_counts(path, batch_size=3, **kwargs)
        if result['stats']['status'] == 'current':
            return result
    raise AssertionError('Counter did not finish')


def assert_exact(path):
    report = counts.count_status(path)
    total, undated = actual(path)
    assert report['stats']['status'] == 'current'
    assert report['total_events'] == total
    assert report['undated_count'] == (undated or 0)
    with closing(connect(path)) as conn:
        for source in report['by_source']:
            expected = conn.execute('SELECT count(1) FROM events WHERE source_id=?', (source,)).fetchone()[0]
            assert report['by_source'][source]['total_events'] == expected
        expected = dict(conn.execute('SELECT category,count(1) FROM events GROUP BY category'))
        assert {row['name']: row['count'] for row in report['categories']} == expected
    return report


def test_empty_initialized_catalog_and_insert_dedupe_replace_delete(tmp_path):
    path = fixture(tmp_path)
    assert_exact(path)
    with closing(connect(path)) as conn, conn:
        insert(conn, 'one')
        insert(conn, 'one', mode='INSERT OR IGNORE')
    assert assert_exact(path)['total_events'] == 1
    with closing(connect(path)) as conn, conn:
        insert(conn, 'one', mode='INSERT OR REPLACE', source='b', category='sample', date='2025-02-01')
    report = assert_exact(path)
    assert report['by_source']['a']['total_events'] == 0
    assert report['undated_count'] == 0
    with closing(connect(path)) as conn, conn:
        conn.execute('DELETE FROM events')
    assert assert_exact(path)['total_events'] == 0


def test_legacy_build_is_bounded_persistent_and_unknown_until_complete(tmp_path):
    path = fixture(tmp_path, legacy=True, n=10)
    assert counts.count_status(path)['stats']['error'] == 'counts_not_initialized'
    first = counts.build_counts(path, batch_size=2)
    assert first['processed_in_run'] == 2
    assert first['total_events'] is None
    assert first['stats']['status'] == 'counting'
    assert first['stats']['counted_events'] == 2
    assert first['by_source']['empty']['total_events'] is None
    assert first['by_source']['empty']['counted_events'] == 0
    assert first['stats']['checkpoint_rowid'] == 2
    assert counts.count_status(path)['stats']['counted_events'] == 2
    assert finish(path)['total_events'] == 10
    assert_exact(path)


def test_pending_and_counted_mutations_and_new_rows_during_backfill(tmp_path):
    path = fixture(tmp_path, legacy=True, n=8)
    counts.build_counts(path, batch_size=2)
    with closing(connect(path)) as conn, conn:
        # Counted old, uncounted old, and new rows all change in one commit.
        conn.execute("UPDATE events SET source_id='b',category='sample',occurred_at='2026' WHERE id IN ('0','6')")
        conn.execute("DELETE FROM events WHERE id IN ('1','5')")
        insert(conn, 'new', source='b')
    partial = counts.count_status(path)
    assert partial['stats']['counted_events'] == 2  # old0 + new only
    assert partial['by_source']['b']['counted_events'] == 2
    finish(path)
    assert_exact(path)


def test_rowid_moves_across_counted_and_pending_boundaries(tmp_path):
    path = fixture(tmp_path, legacy=True, n=6)
    counts.build_counts(path, batch_size=2)
    with closing(connect(path)) as conn, conn:
        conn.execute("UPDATE events SET rowid=20 WHERE id='5'")  # pending -> new
        conn.execute("UPDATE events SET rowid=0 WHERE id='4'")   # pending -> prefix
        conn.execute("UPDATE events SET rowid=5 WHERE id='0'")   # prefix -> pending
    assert counts.count_status(path)['stats']['counted_events'] == 3
    finish(path)
    assert_exact(path)


def test_deleted_watermark_and_reused_high_rowids_do_not_invent_counts(tmp_path):
    path = fixture(tmp_path, legacy=True, n=5)
    counts.build_counts(path, batch_size=2)
    with closing(connect(path)) as conn, conn:
        conn.execute('DELETE FROM events WHERE rowid>=3')
        insert(conn, 'reused')  # rowid3 lies in the pending portion <=H5
    assert counts.count_status(path)['stats']['counted_events'] == 2
    finish(path)
    assert assert_exact(path)['total_events'] == 3
    with closing(connect(path)) as conn, conn:
        conn.execute("DELETE FROM events WHERE id='reused'")
        insert(conn, 'again')
    assert assert_exact(path)['total_events'] == 3


def test_empty_tail_finishes_across_deleted_watermark(tmp_path):
    path = fixture(tmp_path, legacy=True, n=5)
    counts.build_counts(path, batch_size=2)
    with closing(connect(path)) as conn, conn:
        conn.execute('DELETE FROM events WHERE rowid>2')
    result = counts.build_counts(path, batch_size=1)
    assert result['stats']['checkpoint_rowid'] == 5
    assert assert_exact(path)['total_events'] == 2


def test_negative_zero_and_sparse_rowids(tmp_path):
    path = fixture(tmp_path, legacy=True)
    with closing(connect(path)) as conn, conn:
        for rowid in (-100, -1, 0, 30, 10000000):
            insert(conn, str(rowid), rowid=rowid)
    first = counts.build_counts(path, batch_size=2)
    assert first['stats']['checkpoint_rowid'] == -1
    assert first['stats']['counted_events'] == 2
    with closing(connect(path)) as conn, conn:
        insert(conn, 'prefix', rowid=-50)
    assert counts.count_status(path)['stats']['counted_events'] == 3
    finish(path)
    assert assert_exact(path)['total_events'] == 6


def test_failed_source_transaction_rolls_back_counters(tmp_path):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn:
        conn.execute('BEGIN')
        insert(conn, 'rollback')
        assert conn.execute("SELECT total FROM catalog_event_counts WHERE scope='all'").fetchone()[0] == 1
        conn.rollback()
    assert assert_exact(path)['total_events'] == 0


@pytest.mark.parametrize('mode', ['INSERT OR REPLACE', 'INSERT OR IGNORE'])
def test_unconfigured_conflicting_writer_is_rejected_not_silently_wrong(tmp_path, mode):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn, conn:
        insert(conn, 'existing')
    with closing(sqlite3.connect(path)) as conn:
        with pytest.raises(sqlite3.IntegrityError, match='recursive_triggers'):
            insert(conn, 'existing', mode=mode)
        conn.rollback()
    assert assert_exact(path)['total_events'] == 1


def test_explicit_rowid_replace_with_different_id_is_counted_once(tmp_path):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn, conn:
        insert(conn, 'old', rowid=7)
        insert(conn, 'new', rowid=7, source='b', mode='INSERT OR REPLACE')
    assert assert_exact(path)['total_events'] == 1
    with closing(sqlite3.connect(path)) as conn:
        with pytest.raises(sqlite3.IntegrityError, match='recursive_triggers'):
            insert(conn, 'bad', rowid=7, mode='INSERT OR REPLACE')
    assert_exact(path)


def test_metadata_only_updates_do_not_change_counts(tmp_path):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn, conn:
        insert(conn, 'e')
    before = counts.count_status(path)
    with closing(connect(path)) as conn, conn:
        conn.execute("UPDATE events SET metadata='{}',title='changed',time_precision='day' WHERE id='e'")
    assert counts.count_status(path) == before


def test_type_date_and_source_updates_retain_exact_counts(tmp_path):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn, conn:
        insert(conn, 'e')
        conn.execute("UPDATE events SET source_id='b',category='message',occurred_at='' WHERE id='e'")
    assert assert_exact(path)['undated_count'] == 0  # SQL NULL only, no invented dates
    with closing(connect(path)) as conn, conn:
        conn.execute("UPDATE events SET occurred_at=NULL WHERE id='e'")
    assert assert_exact(path)['undated_count'] == 1


def test_installation_in_caller_transaction_rolls_back_atomically(tmp_path):
    path = fixture(tmp_path, legacy=True, n=2)
    with closing(connect(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        counts.ensure_counts(conn)
        insert(conn, 'new')
        conn.rollback()
    assert counts.count_status(path)['stats']['error'] == 'counts_not_initialized'
    assert finish(path)['total_events'] == 2


def test_backfill_budget_preserves_atomic_checkpoint(tmp_path, monkeypatch):
    path = fixture(tmp_path, legacy=True, n=2500)
    counts.build_counts(path, batch_size=2)
    before = counts.count_status(path)
    # First calls permit BEGIN/state; progress interrupts the larger batch.
    clock = iter([0.0] * 6 + [10.0] * 100)
    monkeypatch.setattr(counts.time, 'monotonic', lambda: next(clock, 10.0))
    result = counts.build_counts(path, batch_size=2000)
    assert result['paused'] == 'count_batch_budget_exceeded'
    assert result['stats']['checkpoint_rowid'] == before['stats']['checkpoint_rowid']
    assert result['stats']['counted_events'] == before['stats']['counted_events']


def test_busy_writer_leaves_checkpoint_and_releases_own_connection(tmp_path):
    path = fixture(tmp_path, legacy=True, n=5)
    counts.build_counts(path, batch_size=1)
    with closing(connect(path)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        result = counts.build_counts(path, batch_size=2)
        assert result['paused'] == 'catalog_busy'
        assert result['stats']['counted_events'] == 1
        writer.rollback()
    finish(path)
    assert_exact(path)


def test_partial_vacuum_or_schema_change_requires_rebuild(tmp_path):
    path = fixture(tmp_path, legacy=True, n=8)
    counts.build_counts(path, batch_size=2)
    with closing(connect(path)) as conn:
        conn.execute('VACUUM')
    assert counts.count_status(path)['stats']['error'] == 'counts_rebuild_required'
    finish(path)
    assert_exact(path)
    with closing(connect(path)) as conn:
        conn.execute('VACUUM')
    assert_exact(path)  # Complete aggregates do not depend on rowid boundaries.


def test_missing_trigger_never_presents_stale_counts_as_current(tmp_path):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn, conn:
        conn.execute('DROP TRIGGER catalog_counts_insert')
        insert(conn, 'untracked')
    assert counts.count_status(path)['total_events'] is None
    finish(path)
    assert_exact(path)


def test_count_queries_use_rowid_ranges_and_small_tables(tmp_path):
    path = fixture(tmp_path, legacy=True, n=100)
    with closing(connect(path)) as conn:
        sql = 'SELECT rowid,source_id,category,occurred_at FROM events WHERE rowid<=? AND rowid>? ORDER BY rowid LIMIT ?'
        plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, (100, 2, 5))]
        assert any('INTEGER PRIMARY KEY' in row and 'SEARCH' in row for row in plan)
        assert all('SCAN' not in row and 'TEMP B-TREE' not in row for row in plan)
    counts.build_counts(path, batch_size=2)
    assert counts.count_status(path, max_opcodes=10000)['stats']['counted_events'] == 2


def test_missing_path_is_not_created(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    assert counts.count_status(path)['total_events'] is None
    assert not path.exists()


@pytest.mark.parametrize('column,value', [('id', 'second'), ('rowid', 2)])
def test_update_replace_guard_and_recursive_writer(column, value, tmp_path):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn, conn:
        insert(conn, 'first', rowid=1)
        insert(conn, 'second', rowid=2, source='b')
    with closing(sqlite3.connect(path)) as conn:
        with pytest.raises(sqlite3.IntegrityError, match='recursive_triggers'):
            conn.execute(f'UPDATE OR REPLACE events SET {column}=? WHERE id=?', (value, 'first'))
        conn.rollback()
    assert assert_exact(path)['total_events'] == 2
    with closing(connect(path)) as conn, conn:
        conn.execute(f'UPDATE OR REPLACE events SET {column}=? WHERE id=?', (value, 'first'))
    assert assert_exact(path)['total_events'] == 1


def test_catalog_read_remains_one_snapshot_during_concurrent_commit(tmp_path, monkeypatch):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn, conn:
        insert(conn, 'before', date='2025-01-01')
    original, committed = counts._bounds, []
    def concurrent(conn, source=None):
        if not committed:
            committed.append(True)
            with closing(connect(path)) as writer, writer:
                insert(writer, 'after', date='2026-01-01')
        return original(conn, source)
    monkeypatch.setattr(counts, '_bounds', concurrent)
    first = counts.count_status(path)
    assert first['total_events'] == 1
    assert first['date_range']['end'] == '2025-01-01'
    second = counts.count_status(path)
    assert second['total_events'] == 2
    assert second['date_range']['end'] == '2026-01-01'


def test_failed_backfill_insert_does_not_advance_checkpoint(tmp_path):
    path = fixture(tmp_path, legacy=True, n=6)
    counts.build_counts(path, batch_size=2)
    with closing(connect(path)) as conn, conn:
        # An unrelated schema change must reset the partial boundary safely.
        conn.execute("CREATE TRIGGER reject_counter_update BEFORE UPDATE ON catalog_event_counts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic failure'):
        counts.build_counts(path, batch_size=2)
    with closing(connect(path)) as conn, conn:
        conn.execute('DROP TRIGGER reject_counter_update')
    finish(path)
    assert_exact(path)


def test_optional_bounds_failure_never_discards_exact_counts(tmp_path, monkeypatch):
    path = fixture(tmp_path)
    with closing(connect(path)) as conn, conn:
        insert(conn, 'one')
    def unavailable(*args):
        exc = sqlite3.OperationalError('interrupted')
        exc.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
        raise exc
    monkeypatch.setattr(counts, '_bounds', unavailable)
    result = counts.count_status(path)
    assert result['total_events'] == 1
    assert result['by_source']['a']['total_events'] == 1
    assert result['stats']['status'] == 'current'
    assert result['stats']['date_bounds_status'] == 'unavailable'
    assert result['stats']['details_error'] == 'count_read_budget_exceeded'


def test_optional_category_discovery_failure_keeps_counted_subset(tmp_path):
    path = fixture(tmp_path, legacy=True, n=5)
    counts.build_counts(path, batch_size=2)
    with closing(connect(path)) as conn:
        class LimitedDetails:
            def execute(self, sql, args=()):
                if sql.startswith('SELECT category FROM events'):
                    exc = sqlite3.OperationalError('interrupted')
                    exc.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
                    raise exc
                return conn.execute(sql, args)
        result = counts.read_counts(LimitedDetails())
    assert result['total_events'] is None
    assert result['stats']['status'] == 'counting'
    assert result['stats']['counted_events'] == 2
    assert result['by_source']['a']['counted_events'] == 2
    assert result['categories'] == [{'name': 'note', 'count': None, 'counted_events': 2}]
    assert result['stats']['categories_status'] == 'names_partial'
    assert result['stats']['details_error'] == 'count_read_budget_exceeded'
