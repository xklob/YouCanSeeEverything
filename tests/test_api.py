import pytest
from fastapi.testclient import TestClient
from biography import api,db
from biography.config import Settings

@pytest.fixture
def client(tmp_path,monkeypatch):
    local=Settings(data_dir=tmp_path/'vault',state_dir=tmp_path/'.local',google_maps_api_key='')
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
    overview=client.get('/api/overview').json()
    assert overview['total_events']==0
    assert overview['stats']['status']=='current'
    assert client.get('/api/events').json()['total']==0
    assert 'frame-ancestors' in client.get('/api/status').headers['content-security-policy']


def test_map_configuration_defaults_to_configured_provider_without_exposing_key_in_status(client,monkeypatch):
    from dataclasses import replace
    response=client.get('/api/map-config')
    assert response.json()=={'provider':'openstreetmap','google_maps_configured':False}
    assert response.headers['cache-control']=='no-store'
    assert 'google' not in response.headers['content-security-policy']
    fake_key='synthetic-browser-key-for-api-test'
    configured=replace(api.settings,google_maps_api_key=fake_key)
    monkeypatch.setattr(api,'settings',configured)
    response=client.get('/api/map-config')
    assert response.json()=={'provider':'google','google_maps_configured':True,'google_maps_api_key':fake_key}
    assert response.headers['cache-control']=='no-store'
    policy=response.headers['content-security-policy']
    script_policy=next(item for item in policy.split('; ') if item.startswith('script-src '))
    assert 'https://maps.googleapis.com' in script_policy
    assert 'https://maps.gstatic.com' in script_policy
    assert "'unsafe-inline'" not in script_policy
    assert fake_key not in repr(configured)
    assert fake_key not in client.get('/api/status').text
    assert fake_key not in policy
    assert client.get('/api/map-config',headers={'host':'rebound.example'}).status_code==403


def test_trip_api_validates_filters_and_keeps_full_adjacency(client):
    import json
    from biography.locations import reconcile_locations
    with db.transaction() as conn:
        conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('s','Synthetic','now','now')")
        for index,title in enumerate(('matching first','intervening stop','matching last')):
            conn.execute("""INSERT INTO events(id,source_id,title,category,occurred_at,end_at,time_precision,metadata,created_at)
                VALUES(?,'s',?,'place_visit',?,?,'second',?,'now')""",
                (f'stop-{index}',title,f'2024-06-01T{index*4:02d}:00:00Z',f'2024-06-01T{index*4+1:02d}:00:00Z',
                 json.dumps({'location':{'latitude':40,'longitude':-73+index}})))
    reconcile_locations()
    response=client.get('/api/trips?from=2024-06-01&to=2024-06-01&limit=1')
    assert response.status_code==200
    result=response.json()
    assert result['total']==2 and result['truncated'] and len(result['items'])==1
    assert result['items'][0]['from']['event_id']=='stop-0'
    assert result['items'][0]['to']['event_id']=='stop-1'
    assert client.get('/api/trips?q=matching').json()['total']==0
    assert client.get('/api/trips?from=2025-01-01').json()['total']==0
    assert client.get('/api/trips?from=invalid').status_code==400
    assert client.get('/api/trips?limit=5001').status_code==422
    groups=client.get('/api/journeys?from=2024-06-01&to=2024-06-01').json()
    assert groups['total']==1 and groups['items'][0]['legs']==2
    group=groups['items'][0]
    detail=client.get('/api/journeys/'+group['id']+'?limit=1').json()
    assert detail['total']==2 and detail['truncated'] and not detail['complete']
    assert detail['items'][0]['from']['event_id']=='stop-0'
    assert client.get('/api/journeys?kind=unknown').status_code==400
    assert client.get('/api/journeys?from=invalid').status_code==400
    assert client.get('/api/journeys/missing').status_code==404


def test_day_routes_preserve_evidence_and_report_unbuilt_index(client):
    from biography.daily import build_daily
    assert client.get('/api/daily/status').json()['status']=='not_built'
    assert client.get('/api/daily/categories').json()['items']==[]
    assert client.get('/api/days').json()['items']==[]
    with db.transaction() as conn:
        conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('s','Synthetic','now','now')")
        conn.execute("""INSERT INTO events(id,source_id,title,body,category,occurred_at,time_precision,created_at)
            VALUES('note','s','A note','Original wording','note','2024-06-01T12:00:00Z','second','now')""")
    build_daily(api.settings.database,api.settings.daily_database,capacity_paths=(),reserve_bytes=0)
    days=client.get('/api/days?from=2024-06-01&to=2024-06-01').json()
    assert days['total']==1
    detail=client.get('/api/days/2024-06-01').json()
    assert detail['items'][0]['id']=='note'
    assert detail['items'][0]['body']=='Original wording'
    categories=client.get('/api/daily/categories').json()['items']
    assert any(item['name']=='note' and item['count']==1 for item in categories)
    assert client.get('/api/days/2024-99-01').status_code==400
    assert client.get('/api/days?from=invalid').status_code==400
    assert client.get('/api/rollups/missing').status_code==404
    coverage=client.get('/api/coverage').json()
    assert coverage['indexing']['daily']['processed_records']==1


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
