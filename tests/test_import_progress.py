from datetime import datetime, timezone
import json
import sqlite3

import pytest

from biography.import_progress import InputProgress, public_activity, public_cleanup
from test_coverage import catalog, read


def test_current_check_has_its_own_denominator_and_restarts_for_each_phase():
    progress = InputProgress('/private/export.zip', 1000)
    progress.observe({'event': 'bytes_read', 'phase': 'source_hash', 'bytes': 600})
    progress.observe({'event': 'bytes_read', 'phase': 'source_hash', 'bytes': 200})
    current = public_activity(progress.checkpoint())
    assert current['progress'] == {'completed': 800, 'total': 1000, 'fraction': .8}
    progress.observe({'event': 'bytes_read', 'phase': 'source_reconciliation', 'bytes': 100})
    assert public_activity(progress.checkpoint())['progress']['fraction'] == .1
    progress.observe({'event': 'bytes_read', 'phase': 'retained_verification', 'bytes': 3000})
    assert public_activity(progress.checkpoint())['progress'] is None


def test_expanded_content_never_claims_an_input_percentage():
    progress = InputProgress('/private/export.zip', 100)
    for _ in range(4):
        progress.observe({'event': 'bytes_written', 'bytes': 500})
        progress.observe({'event': 'object_verified'})
    current = public_activity(progress.checkpoint())
    assert current['retained_entries'] == 4
    assert current['progress'] is None
    assert current['label'] == 'Indexing retained records'
    assert current['filename'] == 'export.zip'
    assert '/private' not in json.dumps(current)


def test_coverage_exposes_current_input_without_rewriting_completed_input_totals(tmp_path):
    path = catalog(tmp_path)
    checkpoint = {'current_source': 'C:\\PRIVATE_ROOT_SENTINEL\\export.zip',
        'current_source_bytes': 2000, 'phase': 'source_hash', 'phase_bytes': 1000,
        'retained_entries': 80, 'updated_at': '2025-01-01T00:00:00Z',
        'source_cleanup': {'removed': 0, 'retained': 1},
        'raw_diagnostic': 'PRIVATE_DIAGNOSTIC_SENTINEL'}
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE imports SET checkpoint=? WHERE id=?', (json.dumps(checkpoint), 'import-running'))
    report = read(path)
    active = report['active_imports'][0]
    assert active['files']['fraction'] == .25
    assert active['bytes']['fraction'] == .9
    assert active['activity']['progress']['fraction'] == .5
    assert active['activity']['filename'] == 'export.zip'
    assert active['source_cleanup'] == {'removed': 0, 'retained': 1}
    assert 'PRIVATE_ROOT_SENTINEL' not in json.dumps(report)
    assert 'PRIVATE_DIAGNOSTIC_SENTINEL' not in json.dumps(report)


@pytest.mark.parametrize('value',[None, [], 'not json', {'phase': []}, {'phase': 'unexpected'}])
def test_unknown_and_invalid_checkpoints_do_not_invent_activity(value):
    assert public_activity(value) is None


def test_legacy_checkpoint_keeps_known_phase_without_inventing_a_heartbeat():
    assert public_cleanup({}) is None
    result = public_activity({'current_source': '/private/example.zip', 'phase': 'bytes_written'})
    assert result['label'] == 'Retaining archive contents'
    assert result['progress'] is None
    assert result['updated_at'] is None
    assert result['seconds_since_update'] is None
    assert result['retained_entries'] is None


@pytest.mark.parametrize('value',[None, 'not json', [], {'source_cleanup': {'removed': True, 'retained': 1}},
    {'source_cleanup': {'removed': 1}}, {'source_cleanup': {'removed': -1, 'retained': 2}}])
def test_missing_or_invalid_cleanup_counters_stay_unknown(value):
    assert public_cleanup(value) is None


def test_invalid_counters_and_future_heartbeats_stay_unknown():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    value = {'phase': 'source_hash', 'phase_bytes': True, 'current_source_bytes': -1,
        'retained_entries': 'many', 'updated_at': '2026-01-01T00:00:00Z'}
    result = public_activity(value, now=now)
    assert result['progress'] is None and result['retained_entries'] is None
    assert result['seconds_since_update'] is None
    value.update(phase_bytes=5,current_source_bytes=3,updated_at='2024-12-31T23:57:00Z')
    result = public_activity(value, now=now)
    assert result['progress'] is None
    assert result['seconds_since_update'] == 180
