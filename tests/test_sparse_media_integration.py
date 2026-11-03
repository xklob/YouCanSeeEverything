"""Sparse discovery is driven by ingestion, with late explicit references kept."""
import hashlib
import json
from types import SimpleNamespace
import pytest

from biography import db, media, pipeline
from biography.config import Settings
from biography.importer import StoredFile


def setup_catalog(tmp_path):
    path=tmp_path/'catalog.sqlite3'
    db.init_db(path)
    sid=db.register_source('Synthetic export',[],0,0,path)
    return path,sid


def retained(path,sid,tmp_path,file_id,name,content):
    digest=hashlib.sha256(content).hexdigest()
    target=tmp_path/digest
    target.write_bytes(content)
    with db.transaction(path) as conn:
        conn.execute('INSERT OR IGNORE INTO objects VALUES(?,?,?,?)',(digest,len(content),str(target),'now'))
        conn.execute('INSERT INTO files(id,source_id,original_path,object_hash,bytes,created_at) VALUES(?,?,?,?,?,?)',
                     (file_id,sid,name,digest,len(content),'now'))
        media.index_media_files(conn,file_ids=[file_id])
    return target,digest


def test_native_null_health_records_create_no_media_candidates_or_empty_states(tmp_path):
    path,sid=setup_catalog(tmp_path)
    _,digest=retained(path,sid,tmp_path,'measurements','export.zip!measurements.csv',b'timestamp,value\n')
    statements=[]
    with db.transaction(path) as conn:
        conn.set_trace_callback(statements.append)
        records=[{'title':'Synthetic reading','category':'record','locator':f'row:{i}',
                  'metadata':{'heart_rate':70,'unit':'bpm'},'occurred_at':'2024-01-01T00:00:00Z'} for i in range(1500)]
        assert db.add_events(conn,sid,'measurements',digest,SimpleNamespace(parser='csv.records',events=records))==1500
        assert conn.execute('SELECT count(*) FROM media_candidates').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM media_native_keys').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM media_event_state').fetchone()[0]==0
    assert sum(sql.startswith('SELECT mime FROM media_file_index WHERE file_id=') for sql in statements)==1
    result=media.reconcile_media(path,source_id=sid,index_files=False)
    assert result['events']==0


def test_generic_native_id_becomes_candidate_when_manifest_arrives_later(tmp_path):
    path,sid=setup_catalog(tmp_path)
    _,digest=retained(path,sid,tmp_path,'document','export.zip!records.json',b'{}')
    with db.transaction(path) as conn:
        db.add_events(conn,sid,'document',digest,SimpleNamespace(parser='json.records',events=[
            {'title':'Generic native record','source_native_id':'custom:late-reference','category':'project_context','metadata':{}}]))
        event_id=conn.execute("SELECT id FROM events WHERE native_id='custom:late-reference'").fetchone()[0]
        assert conn.execute('SELECT count(*) FROM media_candidates').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM media_native_keys').fetchone()[0]==1
    _,image_hash=retained(path,sid,tmp_path,'photo','export.zip!photo.png',b'\x89PNG\r\n\x1a\nsynthetic source image')
    manifest={'items':[{'status':'downloaded','file':'photo.png','sha256':image_hash,
                        'references':[{'native_id':'custom:late-reference'}]}]}
    retained(path,sid,tmp_path,'manifest','export.zip!manifest.json',json.dumps(manifest).encode())
    media.index_explicit_manifests(path,source_id=sid)
    media.reconcile_media(path,source_id=sid,index_files=False)
    links=media.media_for_event(event_id,path)
    assert len(links)==1 and links[0]['file_id']=='photo'
    assert links[0]['match_basis']=='explicit_manifest_native_id_and_hash'


@pytest.mark.parametrize('filename',['photo.png','photo.data'])
def test_verified_file_is_sniffed_before_pipeline_registers_direct_media(tmp_path,filename):
    path,sid=setup_catalog(tmp_path)
    original=b'\x89PNG\r\n\x1a\nsynthetic retained image'
    digest=hashlib.sha256(original).hexdigest()
    physical=tmp_path/digest
    physical.write_bytes(original)
    stored=StoredFile(digest,len(original),physical,filename,'',[],None,'retained_leaf')
    assert pipeline.index_stored(stored,sid,None,'Synthetic export',path)==1
    with db.transaction(path) as conn:
        event_id=conn.execute('SELECT id FROM events').fetchone()[0]
        assert conn.execute('SELECT count(*) FROM media_candidates WHERE event_id=?',(event_id,)).fetchone()[0]==1
    media.reconcile_media(path,source_id=sid,index_files=False)
    assert len(media.media_for_event(event_id,path))==1
    assert physical.read_bytes()==original


def test_source_completion_enqueues_files_without_draining_all_events(tmp_path,monkeypatch):
    path,sid=setup_catalog(tmp_path)
    source=tmp_path/'exports'/'Synthetic export'
    source.mkdir(parents=True)
    local=Settings(data_dir=tmp_path/'vault',state_dir=tmp_path/'state',export_root=source.parent,reserve_bytes=0)
    monkeypatch.setattr(pipeline,'settings',local)
    from biography import enrichment,locations
    monkeypatch.setattr(enrichment,'reapply',lambda *args,**kwargs:None)
    monkeypatch.setattr(locations,'reconcile_locations',lambda *args,**kwargs:None)
    seen=[]
    monkeypatch.setattr(media,'reconcile_media',lambda *args,**kwargs:seen.append(kwargs))
    result=pipeline.run_dataset({'id':sid,'name':'Synthetic export','root':str(source)},data_dir=local.data_dir,db_path=path)
    assert result['status']=='ready'
    assert seen==[{'source_id':sid,'event_ids':[]}]
