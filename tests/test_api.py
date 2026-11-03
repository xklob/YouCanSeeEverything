import pytest
from fastapi.testclient import TestClient
from biography import api,db
from biography.config import Settings

@pytest.fixture
def client(tmp_path,monkeypatch):
    local=Settings(data_dir=tmp_path/'vault')
    monkeypatch.setattr(db,'settings',local)
    monkeypatch.setattr(api,'settings',local)
    with TestClient(api.app,base_url='http://127.0.0.1:8000') as c:yield c

def test_host_origin_and_session_boundaries(client):
    assert client.get('/api/status').status_code==200
    assert client.get('/api/status',headers={'host':'rebound.example'}).status_code==403
    assert client.post('/api/assistant',json={'prompt':'hello'}).status_code==403
    token=client.get('/api/session').json()['token']
    assert client.post('/api/assistant',headers={'x-biography-token':token,'origin':'https://attacker.example'},json={'prompt':'hello'}).status_code==403
    assert client.post('/api/assistant',headers={'x-biography-token':token},json={'prompt':'hello','mode':'delete_everything'}).status_code==400

def test_api_unknown_objects_and_safe_static_boundaries(client):
    assert client.get('/api/events/missing').status_code==404
    assert client.get('/api/files/missing/download').status_code==404
    assert client.get('/api/not-real').status_code==404
    assert client.get('/api/overview').json()['total_events']==0
    assert 'frame-ancestors' in client.get('/api/status').headers['content-security-policy']


def test_media_range_and_map_filters(client):
    import hashlib
    import json
    from biography.media import index_media_files
    from biography.locations import reconcile_locations
    png=b'\x89PNG\r\n\x1a\nsynthetic-media-body'
    digest=hashlib.sha256(png).hexdigest()
    target=api.settings.data_dir/'objects'/digest
    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(png)
    with db.transaction() as conn:
        conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('s','Synthetic','now','now')")
        conn.execute("INSERT INTO objects VALUES(?,?,?,?)",(digest,len(png),str(target),'now'))
        conn.execute("INSERT INTO files(id,source_id,original_path,object_hash,bytes,created_at) VALUES('image','s','photo.png',?,?,'now')",(digest,len(png)))
        conn.execute("INSERT INTO events(id,source_id,title,category,occurred_at,metadata,created_at) VALUES('point','s','Place','location','2024-06-01',?,'now')",(json.dumps({'latitude':40.0,'longitude':-73.0}),))
    index_media_files();reconcile_locations()
    response=client.get('/api/files/image/media',headers={'Range':'bytes=0-7'})
    assert response.status_code==206 and response.content==png[:8]
    assert response.headers['content-type']=='image/png'
    assert response.headers['content-disposition'].startswith('inline;')
    assert client.get('/api/map?from=2024-06-01&to=2024-06-01').json()['total']==1
    assert client.get('/api/map?from=2025-01-01').json()['total']==0
    assert client.get('/api/map?bounds=wrong').status_code==400
