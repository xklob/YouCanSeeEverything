import hashlib
import json
import sqlite3

import pytest

from biography import media,media_candidates
from biography.db import init_db,transaction


@pytest.fixture
def catalog(tmp_path):
    path=tmp_path/'catalog.sqlite3';init_db(path)
    with transaction(path) as conn:
        conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('source','Synthetic','now','now')")
        media.ensure_schema(conn)
    return path


def event(conn,identity,*,metadata=None,category='record',native=None):
    conn.execute('INSERT INTO events(id,source_id,native_id,title,category,metadata,created_at) VALUES(?,?,?,?,?,?,?)',
                 (identity,'source',native,'Synthetic',category,json.dumps(metadata or {}),'now'))


def retained_file(conn,tmp_path,identity,original_path,content):
    digest=hashlib.sha256(content).hexdigest();physical=tmp_path/digest;physical.write_bytes(content)
    conn.execute('INSERT INTO objects VALUES(?,?,?,?)',(digest,len(content),str(physical),'now'))
    conn.execute('INSERT INTO files(id,source_id,original_path,object_hash,bytes,created_at) VALUES(?,?,?,?,?,?)',
                 (identity,'source',original_path,digest,len(content),'now'))
    return physical


def test_new_null_native_nonmedia_records_do_no_candidate_sql(catalog):
    with transaction(catalog) as conn:
        statements=[];conn.set_trace_callback(statements.append)
        for index in range(100):
            assert not media.record_media_candidate(conn,f'absent-{index}','source',None,None,'record',
                {'quantity':index,'timestamp':'2024-01-01'},direct_media=False,is_new=True)
        conn.set_trace_callback(None)
        assert statements==[]


def test_direct_evidence_plan_uses_500_primary_keys_among_dense_documents(catalog,tmp_path):
    with transaction(catalog) as conn:
        for index in range(6000):event(conn,f'unrelated-{index}',category='document')
        selected=[f'selected-{index}' for index in range(500)]
        for identity in selected:event(conn,identity,category='document')
        retained_file(conn,tmp_path,'photo','photo.data',b'II*\x00synthetic')
        conn.execute('INSERT INTO evidence VALUES(?,?,?,?)',(selected[0],'photo','file:1','fixture'))
        media.index_media_files(conn,file_ids=['photo'])
        conn.execute('ANALYZE')
        statements=[];conn.set_trace_callback(statements.append)
        assert media_candidates._direct_events(conn,selected)=={selected[0]}
        conn.set_trace_callback(None)
        query=next(sql for sql in statements if 'SELECT DISTINCT e.id' in sql)
        plan=[row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+query)]
        assert any('SEARCH e USING INDEX sqlite_autoindex_events_1 (id=?)' in step for step in plan)
        assert not any('events_category' in step or 'SCAN e' in step for step in plan)
        assert any('SEARCH v USING COVERING INDEX sqlite_autoindex_evidence_1 (event_id=?)' in step for step in plan)
        assert any('SEARCH m USING COVERING INDEX sqlite_autoindex_media_file_index_1 (file_id=?)' in step for step in plan)


def test_nonmedia_backfill_batch_skips_direct_evidence_lookup(catalog,monkeypatch):
    with transaction(catalog) as conn:
        for index in range(500):event(conn,str(index),category='record' if index%2 else 'health_sample')
    def unexpected_lookup(*args):raise AssertionError('Nonmedia rows must skip direct-evidence lookup')
    monkeypatch.setattr(media_candidates,'_direct_events',unexpected_lookup)
    result=media.backfill_media_candidates(catalog,batch_size=500)
    assert result['checkpoint_rowid']==500 and result['discovered_candidates']==0


def test_nonmedia_mutation_hook_skips_direct_evidence_lookup(catalog,monkeypatch):
    with transaction(catalog) as conn:
        event(conn,'record')
        def unexpected_lookup(*args):raise AssertionError('A record category cannot have direct-source media')
        monkeypatch.setattr(media_candidates,'_direct_events',unexpected_lookup)
        assert not media.record_media_candidate(conn,'record','source',None,None,'record',{},is_new=False)


def test_sparse_reconcile_does_not_discover_or_write_empty_event_states(catalog):
    with transaction(catalog) as conn:
        for index in range(3000):event(conn,str(index))
        event(conn,'attachment',metadata={'photos':[{'uri':'missing.png'}]})
        media.record_media_candidate(conn,'attachment','source',None,None,'record',{'photos':[{'uri':'missing.png'}]},direct_media=False,is_new=True)
        # A whole-event metadata scan exceeds this VM-work bound. Sparse lookup
        # resolves the one declared attachment without examining the bulk rows.
        ticks=0
        def budget():
            nonlocal ticks
            ticks+=1
            return int(ticks>100)
        conn.set_progress_handler(budget,100)
        result=media.reconcile_media(conn,index_files=False)
        conn.set_progress_handler(None,0)
        assert result['events']==1 and not result['has_more']
        assert conn.execute('SELECT count(*) FROM media_event_state').fetchone()[0]==1
        assert result['candidate_discovery']['status']=='not_started'
        assert not result['candidate_discovery']['discovery_complete_for_watermark']


def test_backfill_fixed_target_resume_and_append_with_native_keys_only(catalog):
    with transaction(catalog) as conn:
        for index in range(6):event(conn,str(index),native='generic-id' if index==2 else None)
        conn.execute("UPDATE events SET metadata=? WHERE id='4'",(json.dumps({'attachments':[{'path':'missing.png'}]}),))
    first=media.backfill_media_candidates(catalog,batch_size=2)
    assert first['watermark_rowid']==6 and first['checkpoint_rowid']==2 and first['processed_records']==2
    with transaction(catalog) as conn:event(conn,'new',metadata={'photos':[{'uri':'new.png'}]})
    second=media.backfill_media_candidates(catalog,batch_size=2,max_batches=10)
    assert second['watermark_rowid']==6 and second['checkpoint_rowid']==6
    assert second['append_backlog_rowid_span']==1 and second['has_more']
    third=media.backfill_media_candidates(catalog,max_batches=10)
    assert third['watermark_rowid']==7 and third['checkpoint_rowid']==7 and third['processed_records']==7
    with transaction(catalog) as conn:
        assert {r[0] for r in conn.execute('SELECT event_id FROM media_candidates')}=={'4','new'}
        assert [tuple(r) for r in conn.execute('SELECT event_id,native_id FROM media_native_keys')]==[('2','generic-id')]
        assert conn.execute('SELECT count(*) FROM media_event_state').fetchone()[0]==0


def test_failed_backfill_batch_rolls_back_candidates_native_keys_and_checkpoint(catalog,monkeypatch):
    with transaction(catalog) as conn:
        for index in range(6):event(conn,str(index),native=str(index),metadata={'photos':[{'uri':'missing.png'}]})
    first=media.backfill_media_candidates(catalog,batch_size=2)
    original=media_candidates.record_media_candidate
    def interrupted(conn,event_id,*args,**kwargs):
        if event_id=='3':raise RuntimeError('synthetic interruption')
        return original(conn,event_id,*args,**kwargs)
    monkeypatch.setattr(media_candidates,'record_media_candidate',interrupted)
    with pytest.raises(RuntimeError,match='synthetic interruption'):
        media.backfill_media_candidates(catalog,batch_size=2)
    after=media.media_candidate_status(catalog)
    assert after['checkpoint_rowid']==first['checkpoint_rowid']==2
    with transaction(catalog) as conn:
        assert {r[0] for r in conn.execute('SELECT event_id FROM media_candidates')}=={'0','1'}
        assert {r[0] for r in conn.execute('SELECT event_id FROM media_native_keys')}=={'0','1'}
    monkeypatch.setattr(media_candidates,'record_media_candidate',original)
    assert media.backfill_media_candidates(catalog,max_batches=10)['checkpoint_rowid']==6


def test_backfill_cleans_legacy_empty_states_in_committed_batches(catalog):
    with transaction(catalog) as conn:
        for index in range(4):
            event(conn,str(index))
            conn.execute('INSERT INTO media_event_state VALUES(?,?,?,?,?,?)',(str(index),3,0,0,'old',0))
    media.backfill_media_candidates(catalog,batch_size=2)
    with transaction(catalog) as conn:
        assert {r[0] for r in conn.execute('SELECT event_id FROM media_event_state')}=={'2','3'}
        assert conn.execute('SELECT count(*) FROM events').fetchone()[0]==4
    media.backfill_media_candidates(catalog,batch_size=2)
    with transaction(catalog) as conn:assert conn.execute('SELECT count(*) FROM media_event_state').fetchone()[0]==0


def test_late_direct_tiff_file_discovers_existing_document_without_event_scan(catalog,tmp_path):
    with transaction(catalog) as conn:
        retained_file(conn,tmp_path,'photo','export.zip!photo.data',b'II*\x00synthetic')
        event(conn,'photo-event',category='document')
        conn.execute("INSERT INTO evidence VALUES('photo-event','photo','file:1','fixture')")
    media.backfill_media_candidates(catalog)
    assert media.media_candidate_status(catalog)['candidate_count']==0
    media.index_media_files(catalog,file_ids=['photo'])
    result=media.reconcile_media(catalog,index_files=False)
    assert result['resolved']==1
    assert media.media_for_event('photo-event',catalog)[0]['mime']=='image/jpeg'


def test_deleted_and_shared_evidence_refresh_preserves_other_direct_media(catalog,tmp_path):
    with transaction(catalog) as conn:
        for identity in ('one','two'):
            retained_file(conn,tmp_path,identity,identity+'.data',b'II*\x00'+identity.encode())
        event(conn,'shared',category='document',native='native')
        for identity in ('one','two'):conn.execute('INSERT INTO evidence VALUES(?,?,?,?)',('shared',identity,'file:1','fixture'))
    media.index_media_files(catalog)
    media.reconcile_media(catalog,event_ids=['shared'],index_files=False)
    assert {r['file_id'] for r in media.media_for_event('shared',catalog)}=={'one','two'}
    with transaction(catalog) as conn:conn.execute("DELETE FROM evidence WHERE file_id='one'")
    media.reconcile_media(catalog,event_ids=['shared'],index_files=False)
    assert [r['file_id'] for r in media.media_for_event('shared',catalog)]==['two']
    with transaction(catalog) as conn:
        conn.execute("DELETE FROM evidence WHERE event_id='shared'")
        conn.execute("DELETE FROM events WHERE id='shared'")
    with transaction(catalog) as conn:
        assert conn.execute('SELECT count(*) FROM media_candidates').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM media_native_keys').fetchone()[0]==0


def test_pending_queue_is_separate_from_incomplete_historical_discovery(catalog):
    with transaction(catalog) as conn:
        for identity in ('a','b','c'):
            payload={'photos':[{'uri':identity+'.png'}]};event(conn,identity,metadata=payload)
            media.record_media_candidate(conn,identity,'source',None,None,'record',payload,direct_media=False,is_new=True)
    first=media.reconcile_media(catalog,index_files=False,limit=2)
    assert first['events']==2 and first['has_more']
    assert first['candidate_discovery']['pending_candidates']==1
    second=media.reconcile_media(catalog,index_files=False,limit=2)
    assert second['events']==1 and not second['has_more']
    assert second['candidate_discovery']['queue_drained']
    assert second['candidate_discovery']['status']=='not_started'


def test_readonly_status_does_not_create_catalog_or_schema(catalog,tmp_path):
    absent=tmp_path/'absent'/'catalog.sqlite3'
    assert media.media_candidate_status(absent)['status']=='not_started'
    assert not absent.parent.exists()
    with transaction(catalog) as conn:
        before=list(conn.iterdump());conn.execute('PRAGMA query_only=ON')
        assert media.media_candidate_status(conn)['candidate_count']==0
        assert list(conn.iterdump())==before


def test_missing_or_reused_anchor_requires_explicit_reset(catalog):
    with transaction(catalog) as conn:
        for identity in ('a','b','c'):event(conn,identity)
    media.backfill_media_candidates(catalog,batch_size=1)
    with transaction(catalog) as conn:conn.execute("DELETE FROM events WHERE id='c'")
    assert media.media_candidate_status(catalog)['status']=='rebuild_required'
    with pytest.raises(ValueError,match='requires reset'):media.backfill_media_candidates(catalog)
    fixed=media.backfill_media_candidates(catalog,reset=True,max_batches=10)
    assert fixed['checkpoint_rowid']==fixed['watermark_rowid']==2


def test_completed_old_version_never_claims_discovery_complete(catalog):
    with transaction(catalog) as conn:event(conn,'event')
    media.backfill_media_candidates(catalog)
    with transaction(catalog) as conn:conn.execute('UPDATE media_candidate_backfill SET version=0')
    status=media.media_candidate_status(catalog)
    assert status['status']=='rebuild_required' and status['has_more']
    assert not status['discovery_complete_for_watermark']


def test_backfill_holds_bounded_write_transaction_against_metadata_race(catalog,monkeypatch):
    with transaction(catalog) as conn:event(conn,'event',metadata={'photos':[{'uri':'one.png'}]})
    original=media_candidates._record_rows;attempts=[]
    def concurrent_update(conn,rows):
        with sqlite3.connect(catalog,timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                other.execute("UPDATE events SET metadata='{}' WHERE id='event'")
        attempts.append(True)
        return original(conn,rows)
    monkeypatch.setattr(media_candidates,'_record_rows',concurrent_update)
    assert media.backfill_media_candidates(catalog)['checkpoint_rowid']==1
    assert attempts==[True]


def test_caller_connection_controls_backfill_commit(catalog):
    with transaction(catalog) as conn:event(conn,'event',native='native')
    conn=sqlite3.connect(catalog);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON')
    try:
        media.backfill_media_candidates(conn)
        assert conn.in_transaction
        conn.rollback()
    finally:conn.close()
    assert media.media_candidate_status(catalog)['status']=='not_started'
