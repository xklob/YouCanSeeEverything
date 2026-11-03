import json
from pathlib import Path
from biography.db import init_db,transaction,register_source,add_events,now
from biography.queries import list_events,event_detail,overview
from biography.parsers import ParseResult

def fixture(path):
    init_db(path)
    sid=register_source('Fixture',['/fixture'],10,0,path)
    with transaction(path) as c:
        c.execute('INSERT INTO objects VALUES(?,?,?,?)',('a'*64,10,'/canonical',now()))
        for fid in ('f1','f2'):
            c.execute('INSERT INTO files(id,source_id,original_path,object_hash,bytes,created_at) VALUES(?,?,?,?,?,?)',(fid,sid,'fixture.json','a'*64,10,now()))
    return sid

def test_reimport_preserves_evidence_without_duplicate_events(tmp_path):
    path=tmp_path/'catalog.sqlite3';sid=fixture(path)
    result=ParseResult(parser='fixture',events=[{'source_native_id':'native-1','title':'A dated event','body':'Searchable original evidence','occurred_at':'2024-03-03','category':'note','time_precision':'day','locator':'row:0','metadata':{}}])
    with transaction(path) as c:
        assert add_events(c,sid,'f1','a'*64,result)==1
        assert add_events(c,sid,'f2','a'*64,result)==0
    page=list_events(path,q='original evidence',start='2024-03-03',end='2024-03-03')
    assert page['total']==1
    assert len(event_detail(page['items'][0]['id'],path)['provenance'])==2
    assert overview(path)['date_range']['start']=='2024-03-03'

def test_repeated_real_events_and_changed_native_variants_survive(tmp_path):
    path=tmp_path/'catalog.sqlite3';sid=fixture(path)
    common={'title':'Track','body':'Artist','occurred_at':'2024-03-03T12:00:00Z','category':'music','metadata':{}}
    result=ParseResult(parser='fixture',events=[{**common,'locator':'row:0'},{**common,'locator':'row:1'}])
    with transaction(path) as c:assert add_events(c,sid,'f1','a'*64,result)==2
    changed=ParseResult(parser='fixture',events=[{**common,'source_native_id':'post-1','locator':'row:2'},{**common,'source_native_id':'post-1','locator':'row:3','body':'Edited text'}])
    with transaction(path) as c:assert add_events(c,sid,'f1','a'*64,changed)==2
    assert list_events(path)['total']==4

def test_undated_and_punctuation_search_are_supported(tmp_path):
    path=tmp_path/'catalog.sqlite3';sid=fixture(path)
    result=ParseResult(parser='fixture',events=[{'title':'A journal','body':'A quotation about hello','category':'note','metadata':{}}])
    with transaction(path) as c:add_events(c,sid,'f1','a'*64,result)
    assert list_events(path,undated=True)['total']==1
    assert list_events(path,q='"hello"')['total']==1
    assert list_events(path,q="nothing' OR '1'='1")['total']==0
