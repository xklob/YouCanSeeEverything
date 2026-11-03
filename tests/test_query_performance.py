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
        assert page['total']==len(expected)
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


def test_count_and_display_page_share_one_wal_snapshot(catalog,monkeypatch):
    original_connect=queries.connect
    wrote=False
    def wrapped_connect(path=None):
        conn=original_connect(path)
        def trace(sql):
            nonlocal wrote
            if not wrote and sql.startswith('SELECT e.*,s.name AS source'):
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
