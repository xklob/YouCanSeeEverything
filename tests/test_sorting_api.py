"""Browse sorting is global, validated, and deterministic across pages."""
import pytest
from fastapi.testclient import TestClient
from biography import api, db, queries
from biography.config import Settings


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path/'vault', state_dir=tmp_path/'.local')
    monkeypatch.setattr(api, 'settings', settings)
    monkeypatch.setattr(db, 'settings', settings)
    with TestClient(api.app, base_url='http://127.0.0.1:8000') as client:
        with db.transaction() as conn:
            conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('s','Synthetic','now','now')")
            for index, (identity, date, amount) in enumerate([('b','2024-03-01',20),('a','2024-01-01',30),('c',None,10)]):
                conn.execute("INSERT INTO events(id,source_id,title,occurred_at,category,created_at) VALUES(?,'s',?,?,'note','now')",(identity,identity,date))
                conn.execute("INSERT INTO imports(id,source_id,status,events,total_files,total_bytes,started_at) VALUES(?,'s','ready',?,?,?,?)",(identity,amount,amount,amount,str(index)))
                conn.execute("INSERT INTO jobs(id,kind,status,prompt,created_at,updated_at) VALUES(?,'ask','completed',?,?,?)",(identity,identity,str(index),str(index)))
        yield client, settings.database


@pytest.mark.parametrize('field,direction,expected',[
    ('date','asc',['a','b','c']),('date','desc',['b','a','c']),
    ('imported','asc',['b','a','c']),('imported','desc',['c','a','b'])])
def test_records_sort_before_paging_with_filters(catalog,field,direction,expected):
    client,path=catalog
    pages=[client.get(f'/api/events?sort={field}&direction={direction}&source=s&limit=1&offset={index}').json() for index in range(3)]
    assert [page['items'][0]['id'] for page in pages]==expected
    assert [page['has_more'] for page in pages]==[True,True,False]
    assert queries.list_events(path,sort='asc')['items'][0]['id']=='a'


@pytest.mark.parametrize('route,field,expected',[
    ('imports','records',['c','b','a']),('imports','date',['b','a','c']),
    ('jobs','prompt',['a','b','c']),('jobs','date',['b','a','c'])])
def test_history_sort_before_limit(catalog,route,field,expected):
    client,_=catalog
    for direction,order in [('asc',expected),('desc',list(reversed(expected)))]:
        pages=[client.get(f'/api/{route}?sort={field}&direction={direction}&limit=1&offset={i}').json() for i in range(3)]
        assert [page['items'][0]['id'] for page in pages]==order
        assert [page['has_more'] for page in pages]==[True,True,False]


@pytest.mark.parametrize('route', ['events','imports','jobs','journeys','days','summaries/library'])
def test_invalid_sort_is_rejected(catalog,route):
    client,_=catalog
    assert client.get(f'/api/{route}?sort=drop_table').status_code==400
    assert client.get(f'/api/{route}?direction=sideways').status_code==400


def test_location_direction_validated_and_index_ordered(catalog):
    from biography.locations import ensure_schema, map_points
    _,path=catalog
    with db.transaction(path) as conn:
        ensure_schema(conn)
        for identity,date in [('b','2024-03-01'),('a','2024-01-01'),('c',None)]:
            conn.execute("INSERT INTO event_locations(id,event_id,point_index,latitude,longitude,occurred_at,kind,locator) VALUES(?,?,0,1,2,?,'explicit_coordinates','fixture')",(identity,identity,date))
    for direction,expected in [('asc',['a','b','c']),('desc',['b','a','c'])]:
        assert [map_points(path,direction=direction,limit=1,offset=i)['items'][0]['id'] for i in range(3)]==expected
    with pytest.raises(ValueError):map_points(path,direction='sideways')
