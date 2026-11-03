import hashlib
import json
import sqlite3

import pytest

from biography import daily, enrichment, locations, media, pipeline
from biography.db import identity, init_db, transaction


def fixture(tmp_path, count=1605):
    path = tmp_path / 'catalog.sqlite3'
    init_db(path)
    raw = tmp_path / 'retained.json'
    raw.write_bytes(b'{"synthetic":"immutable original"}')
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    fid = identity('s', 'retained.json', json.dumps([], sort_keys=True), digest)
    with transaction(path) as conn:
        conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('s','Synthetic','now','now')")
        conn.execute('INSERT INTO objects VALUES(?,?,?,?)', (digest, raw.stat().st_size, str(raw), 'now'))
        for file_id in (fid, 'other-file'):
            conn.execute("INSERT INTO files(id,source_id,original_path,object_hash,bytes,parse_status,created_at) VALUES(?,'s','retained.json',?,?,'parsed','now')", (file_id, digest, raw.stat().st_size))
        for number in range(count):
            eid = f'old-{number:05}'
            conn.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,'s','Old fixture','measurement','now')", (eid,))
            conn.execute("INSERT INTO evidence VALUES(?,?,'row:0','fixture')", (eid, fid))
        conn.execute("INSERT INTO evidence VALUES('old-00000','other-file','row:0','fixture')")
        conn.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES('unrelated','s','Unrelated fixture','note','now')")
        conn.execute("INSERT INTO evidence VALUES('unrelated','other-file','row:1','fixture')")
    return path, raw, fid


def test_targeted_reindex_bounds_old_and_new_ids_without_source_event_reconcile(tmp_path, monkeypatch):
    path, raw, fid = fixture(tmp_path)
    original_bytes = raw.read_bytes()
    calls, guards = [], []
    active = False

    def begin(**kwargs):
        nonlocal active
        active = True
        guards.append('begin')
        return 'mutation-token'

    def end(token, **kwargs):
        nonlocal active
        assert token == 'mutation-token'
        active = False
        guards.append('end')

    def index(stored, source_id, import_id, source_name, db_path, **kwargs):
        assert active and stored.path.read_bytes() == original_bytes
        with transaction(db_path) as conn:
            for number in range(1605):
                eid = f'new-{number:05}'
                conn.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,'s','New fixture','measurement','now')", (eid,))
                conn.execute("INSERT INTO evidence VALUES(?,?,'row:0','fixture')", (eid, fid))
        return 1605

    def reconcile(kind, target, *, event_ids=None, **kwargs):
        assert active
        assert event_ids is not None, 'A source-wide event pass is forbidden'
        assert len(event_ids) <= 800
        if event_ids and kind == 'media':
            assert kwargs['index_files'] is False
        calls.append((kind, list(event_ids), kwargs))
        return {}

    monkeypatch.setattr(daily, 'begin_daily_mutation', begin)
    monkeypatch.setattr(daily, 'end_daily_mutation', end)
    monkeypatch.setattr(pipeline, 'index_stored', index)
    monkeypatch.setattr(enrichment, 'reapply', lambda *args, **kwargs: calls.append(('enrichment', [], kwargs)))
    monkeypatch.setattr(locations, 'reconcile_locations', lambda *args, **kwargs: reconcile('locations', *args, **kwargs))
    monkeypatch.setattr(media, 'reconcile_media', lambda *args, **kwargs: reconcile('media', *args, **kwargs))
    assert pipeline.reindex_files([fid, fid], path) == [{'file_id': fid, 'events_added': 1605}]
    assert guards == ['begin', 'end'] and not active
    for kind in ('locations', 'media'):
        seen = {eid for current, ids, _ in calls if current == kind for eid in ids}
        assert seen == {f'{prefix}-{number:05}' for prefix in ('old', 'new') for number in range(1605)}
        assert 'unrelated' not in seen
    assert sum(kind == 'media' and not ids for kind, ids, _ in calls) == 1
    with transaction(path) as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE id LIKE 'old-%'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM evidence WHERE event_id='old-00000'").fetchone()[0] == 1
        assert conn.execute("SELECT title FROM events WHERE id='unrelated'").fetchone()[0] == 'Unrelated fixture'
    assert raw.read_bytes() == original_bytes


def test_missing_required_index_fails_before_record_mutation(tmp_path):
    path, raw, fid = fixture(tmp_path, count=2)
    with transaction(path) as conn:
        conn.execute('DROP INDEX evidence_file')
    with pytest.raises(RuntimeError, match='installed before targeted reindexing'):
        pipeline._reindex_files([fid], path)
    with transaction(path) as conn:
        assert conn.execute('SELECT count(*) FROM events').fetchone()[0] == 3
        assert conn.execute('SELECT parse_status FROM files WHERE id=?', (fid,)).fetchone()[0] == 'parsed'
        assert 'evidence_file' not in {row[1] for row in conn.execute('PRAGMA index_list(evidence)')}
    assert raw.exists()


def test_file_evidence_batches_use_covering_index_without_sort(tmp_path):
    path, _, fid = fixture(tmp_path, count=2)
    with sqlite3.connect(path) as conn:
        for suffix, args in (('', [fid]), (' AND event_id>?', [fid, 'old-00000'])):
            sql = 'SELECT DISTINCT event_id FROM evidence INDEXED BY evidence_file WHERE file_id=?' + suffix + ' ORDER BY event_id LIMIT 800'
            plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, args)]
            assert any('COVERING INDEX evidence_file' in row for row in plan)
            assert not any('TEMP B-TREE' in row for row in plan)
