"""Real SQLite contention and interrupted, idempotent archive replay."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
import zipfile

import pytest

from biography import db, importer, parsers, pipeline
from biography.parsers import ParseResult
from biography.storage import ImportPaused
from biography.config import Settings


def catalog(tmp_path):
    path = tmp_path / 'catalog.sqlite3'
    db.init_db(path)
    source = db.register_source('Synthetic export', [], 0, 0, path)
    raw = tmp_path / 'original.json'
    raw.write_bytes(b'{"synthetic":true}')
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    stored = importer.StoredFile(digest, raw.stat().st_size, raw, 'original.json', '', [], None, 'retained_leaf')
    return path, source, stored


def test_writer_waits_for_real_competing_transaction_then_commits(tmp_path):
    path, source, _ = catalog(tmp_path)
    acquired = threading.Event()
    release = threading.Event()

    def competing_writer():
        with db.transaction(path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            acquired.set()
            assert release.wait(5)

    worker = threading.Thread(target=competing_writer)
    worker.start()
    assert acquired.wait(5)
    timer = threading.Timer(.1, release.set)
    timer.start()
    try:
        with db.write_transaction(path, wait_seconds=2) as conn:
            conn.execute("UPDATE sources SET status='ready' WHERE id=?", (source,))
    finally:
        release.set()
        worker.join(5)
        timer.cancel()
    with db.transaction(path) as conn:
        assert conn.execute('SELECT status FROM sources').fetchone()[0] == 'ready'


def test_exhausted_writer_wait_is_a_resumable_pause(tmp_path):
    path, _, _ = catalog(tmp_path)
    with db.transaction(path) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(ImportPaused, match='catalog_busy'):
            with db.write_transaction(path, wait_seconds=.02):
                pytest.fail('The body cannot run before acquiring its writer')


def test_write_body_is_not_replayed_and_its_failure_rolls_back(tmp_path):
    path, source, _ = catalog(tmp_path)
    calls = []
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        with db.write_transaction(path) as conn:
            calls.append(1)
            conn.execute("UPDATE sources SET status='ready' WHERE id=?", (source,))
            conn.execute('INSERT INTO absent VALUES(1)')
    assert calls == [1]
    with db.transaction(path) as conn:
        assert conn.execute('SELECT status FROM sources').fetchone()[0] == 'queued'


def test_replaying_completed_file_does_not_wait_for_writer(tmp_path):
    path, source, stored = catalog(tmp_path)
    assert pipeline.index_stored(stored, source, None, 'Synthetic export', path) == 1
    with db.transaction(path) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        assert pipeline.index_stored(stored, source, None, 'Synthetic export', path) == 0


def test_interrupted_batches_resume_without_duplicates_or_lost_fallback_locators(tmp_path, monkeypatch):
    path, source, stored = catalog(tmp_path)
    records = [{'title': 'Repeated observation', 'category': 'note', 'metadata': {}} for _ in range(1201)]
    monkeypatch.setattr(parsers, 'parse_file', lambda *a, **k: ParseResult(
        parser='synthetic', status='parsed', events=records))
    original = pipeline.add_events
    calls = []

    def interrupt(conn, *args, **kwargs):
        calls.append(len(args[-1].events))
        if len(calls) == 2:
            # This batch is rolled back even if its first event was inserted.
            original(conn, *args[:-1], ParseResult(parser='synthetic', events=records[:1]), **kwargs)
            raise ImportPaused('catalog_busy')
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(pipeline, 'add_events', interrupt)
    with pytest.raises(ImportPaused):
        pipeline.index_stored(stored, source, None, 'Synthetic export', path)
    with db.transaction(path) as conn:
        assert conn.execute('SELECT count(*) FROM events').fetchone()[0] == 500
        assert conn.execute('SELECT parse_status FROM files').fetchone()[0] == 'pending'
    monkeypatch.setattr(pipeline, 'add_events', original)
    assert pipeline.index_stored(stored, source, None, 'Synthetic export', path) == 701
    assert pipeline.index_stored(stored, source, None, 'Synthetic export', path) == 0
    with db.transaction(path) as conn:
        assert conn.execute('SELECT count(*) FROM events').fetchone()[0] == 1201
        assert conn.execute('SELECT count(*) FROM evidence').fetchone()[0] == 1201
        assert {r[0] for r in conn.execute('SELECT locator FROM evidence')} == {f'record:{i}' for i in range(1201)}
        assert conn.execute('SELECT parse_status FROM files').fetchone()[0] == 'parsed'
    assert stored.path.read_bytes() == b'{"synthetic":true}'


def test_other_writers_can_commit_between_event_batches(tmp_path, monkeypatch):
    path, source, stored = catalog(tmp_path)
    monkeypatch.setattr(parsers, 'parse_file', lambda *a, **k: ParseResult(
        parser='synthetic', status='parsed', events=[{'title': str(i)} for i in range(1001)]))
    original = db.write_transaction
    observed = []

    @contextmanager
    def competing_write(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            yield conn
        with original(path, wait_seconds=0) as other:
            count = other.execute('SELECT count(*) FROM events').fetchone()[0]
            state = other.execute('SELECT parse_status FROM files').fetchone()[0]
            observed.append((count, state))
            other.execute("UPDATE sources SET updated_at='competing writer' WHERE id=?", (source,))

    monkeypatch.setattr(pipeline, 'write_transaction', competing_write)
    assert pipeline.index_stored(stored, source, None, 'Synthetic export', path) == 1001
    assert observed == [(0, 'pending'), (500, 'pending'), (1000, 'pending'), (1001, 'pending'), (1001, 'parsed')]


def test_catalog_pause_preserves_source_and_paused_manifest(tmp_path):
    source = tmp_path / 'exports' / 'synthetic.zip'
    source.parent.mkdir()
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('original.txt', 'Immutable original source bytes')
    before = source.read_bytes()

    def pause(update):
        if update['event'] == 'object_verified':
            raise ImportPaused('catalog_busy')

    with pytest.raises(ImportPaused) as raised:
        importer.ingest_path(source, tmp_path / 'vault', delete_source=True,
            allowed_root=source.parent, reserve_bytes=0, progress=pause)
    assert source.read_bytes() == before
    assert raised.value.report.status == 'paused'
    manifest = [json.loads(line) for line in raised.value.manifest_path.read_text().splitlines()]
    assert any(row['kind'] == 'paused' for row in manifest)
    assert not any(row['kind'] == 'source_deleted' for row in manifest)


def test_interrupted_serial_import_records_a_pause_and_preserves_input(tmp_path, monkeypatch):
    path, source, _ = catalog(tmp_path)
    inputs = tmp_path / 'exports' / 'Synthetic export'
    inputs.mkdir(parents=True)
    raw = inputs / 'original.txt'
    raw.write_bytes(b'An immutable fictional note')
    monkeypatch.setattr(pipeline, 'settings', Settings(data_dir=tmp_path/'vault',
        state_dir=tmp_path/'state',export_root=inputs.parent,reserve_bytes=0))
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(importer, 'ingest_path', interrupted)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run_dataset({'id':source,'name':'Synthetic export','root':str(inputs)},
            db_path=path,move=True)
    with db.transaction(path) as conn:
        row=conn.execute('SELECT status,checkpoint FROM imports').fetchone()
        assert row['status']=='paused'
        assert json.loads(row['checkpoint'])['raw_complete'] is False
        assert conn.execute('SELECT status FROM sources').fetchone()[0]=='paused'
    assert raw.read_bytes()==b'An immutable fictional note'
