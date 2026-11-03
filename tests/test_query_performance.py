"""Index-plan regressions for catalogs far larger than a display page."""
import json
import sqlite3

import pytest

from biography import queries
from biography.db import connect,init_db,transaction


@pytest.fixture
def catalog(tmp_path):
    path=tmp_path/'catalog.sqlite3'
    init_db(path)
    with transaction(path) as conn:
        conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('source','Synthetic source','now','now')")
        rows=[('old','2024-01-01'),('same-a','2024-02-01'),('same-b','2024-02-01'),
              ('new','2024-03-01'),('undated-a',None),('undated-b',None)]
        for identity,when in rows:
            conn.execute("""INSERT INTO events(id,source_id,title,body,occurred_at,category,metadata,created_at)
                VALUES(?,'source',?,'searchable fixture',?,'note',?,'now')""",
                (identity,identity,when,json.dumps({'fixture':identity})))
    return path


@pytest.mark.parametrize('sort,expected',[
    ('desc',['new','same-a','same-b','old','undated-a','undated-b']),
    ('asc',['old','same-b','same-a','new','undated-b','undated-a']),
])
def test_null_last_tied_timestamps_and_offset_pages_are_deterministic(catalog,sort,expected):
    whole=queries.list_events(catalog,sort=sort)
    assert [item['id'] for item in whole['items']]==expected
    paged=[]
    for offset in range(0,len(expected),2):
        page=queries.list_events(catalog,sort=sort,limit=2,offset=offset)
        assert page['total'] in (None,len(expected))
        assert page['has_more']==(offset+2<len(expected))
        paged.extend(item['id'] for item in page['items'])
    assert paged==expected
    assert len(set(paged))==len(expected)
    assert queries.list_events(catalog,undated=True,sort=sort)['total']==2


@pytest.mark.parametrize('sort',['asc','desc'])
def test_actual_page_sql_uses_chronological_index_without_temporary_sort(catalog,sort,monkeypatch):
    statements=[]
    def traced_connect(path=None):
        conn=connect(path)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(queries,'connect',traced_connect)
    queries.list_events(catalog,sort=sort,limit=2,offset=1)
    sql=next(statement for statement in statements if 'SELECT e.*,s.name AS source' in statement)
    conn=connect(catalog)
    try:
        plan=[row['detail'] for row in conn.execute('EXPLAIN QUERY PLAN '+sql)]
    finally:
        conn.close()
    assert any('events_time' in detail for detail in plan)
    assert not any('TEMP B-TREE' in detail for detail in plan)


def test_complete_page_count_describes_its_snapshot_during_concurrent_write(catalog,monkeypatch):
    original_connect=queries.connect
    wrote=False
    def wrapped_connect(path=None):
        conn=original_connect(path)
        def trace(sql):
            nonlocal wrote
            if not wrote and sql.startswith('SELECT v.event_id'):
                wrote=True
                with transaction(catalog) as writer:
                    writer.execute("""INSERT INTO events(id,source_id,title,occurred_at,category,created_at)
                        VALUES('concurrent','source','Concurrent fixture','2025-01-01','note','now')""")
        conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(queries,'connect',wrapped_connect)
    result=queries.list_events(catalog)
    assert wrote
    assert result['total']==6
    assert len(result['items'])==6
    assert 'concurrent' not in {item['id'] for item in result['items']}
    assert queries.list_events(catalog)['total']==7


def test_page_never_counts_synchronously_and_empty_overrun_is_not_a_total(catalog,monkeypatch):
    statements=[]
    def traced_connect(path=None):
        conn=connect(path)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(queries,'connect',traced_connect)
    monkeypatch.setattr(queries,'get_count',lambda *args:{'total':None,'total_status':'pending','counted_at':None})
    page=queries.list_events(catalog,limit=2)
    assert page['total'] is None and page['has_more'] is True
    overrun=queries.list_events(catalog,limit=2,offset=100)
    assert overrun['items']==[] and overrun['total'] is None and not overrun['has_more']
    assert not any('count(' in sql.lower() for sql in statements)


def test_overview_and_sources_leave_event_measurement_to_cache(catalog,monkeypatch):
    statements=[]
    def traced_connect(path=None):
        conn=connect(path)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(queries,'connect',traced_connect)
    monkeypatch.setattr(queries,'get_catalog_stats',lambda path:{
        'total_events':None,'undated_count':None,'date_range':{'start':None,'end':None},
        'activity':None,'categories':[{'name':'note','count':None}],
        'by_source':None,'stats':{'status':'pending'}})
    summary=queries.overview(catalog)
    assert summary['total_events'] is None and summary['activity']==[]
    assert summary['categories']==[{'name':'note','count':None}]
    source=queries.sources(catalog)['items'][0]
    assert source['event_count'] is None and source['first_event'] is None
    assert not any('FROM events' in sql for sql in statements)


@pytest.mark.parametrize('filters',[
    {'source':'source'}, {'category':'note'}, {'source':'source','category':'note'},
])
@pytest.mark.parametrize('direction',['asc','desc'])
def test_selective_search_with_filters_uses_fts_rowids_before_sorting(catalog,monkeypatch,filters,direction):
    # The newest thousands of records share the filter but not the search.
    # A chronological source scan exhausts this deliberately small budget.
    with transaction(catalog) as conn:
        conn.executemany("""INSERT INTO events(id,source_id,title,occurred_at,category,created_at)
            VALUES(?,'source','Unrelated sample','2025-01-01','note','now')""",
            [(f'sample-{i}',) for i in range(12000)])
    monkeypatch.setattr(queries,'RECORD_QUERY_MAX_OPCODES',20000)
    statements=[]
    def traced_connect(path=None):
        conn=connect(path);conn.set_trace_callback(statements.append);return conn
    monkeypatch.setattr(queries,'connect',traced_connect)
    page=queries.list_events(catalog,q='searchable fixture',direction=direction,**filters)
    assert page['total']==6
    assert {item['id'] for item in page['items']}=={'old','same-a','same-b','new','undated-a','undated-b'}
    assert len(queries.list_events(catalog,q='searchable fixture',limit=2,offset=2,direction=direction,**filters)['items'])==2
    sql=next(statement for statement in statements if 'SELECT e.*,s.name AS source' in statement)
    with transaction(catalog) as conn:
        plan=[row['detail'] for row in conn.execute('EXPLAIN QUERY PLAN '+sql)]
    assert any('INTEGER PRIMARY KEY' in detail for detail in plan)
    assert not any('events_source' in detail or 'events_category' in detail for detail in plan)


def test_search_filters_and_import_sort_preserve_all_constraints(catalog):
    page=queries.list_events(catalog,q='searchable fixture',source='source',category='note',
        start='2024-02-01',end='2024-02-01',sort='imported',direction='asc',limit=1)
    assert [item['id'] for item in page['items']]==['same-a']
    assert page['has_more'] is True
    assert queries.list_events(catalog,q='searchable fixture',source='missing')['total']==0
    assert {item['id'] for item in queries.list_events(catalog,q='searchable fixture',undated=True)['items']}=={'undated-a','undated-b'}
