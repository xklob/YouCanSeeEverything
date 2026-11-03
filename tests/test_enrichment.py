import json
from types import SimpleNamespace
import pytest
from biography.db import init_db,register_source,transaction,add_events
from biography.enrichment import apply_reviewed_report,reapply


def fixture(tmp_path):
    path=tmp_path/'catalog.sqlite3';init_db(path)
    sid=register_source('Tickets',[],0,0,path)
    with transaction(path) as conn:
        conn.execute("INSERT INTO objects VALUES('hash',1,'original','now')")
        conn.execute("INSERT INTO files(id,source_id,original_path,object_hash,bytes,created_at) VALUES('file',?,'attendance.json','hash',1,'now')",(sid,))
        add_events(conn,sid,'file','hash',SimpleNamespace(parser='synthetic',events=[{'title':'Example Festival 2020','category':'event_attendance'}]))
        eid=conn.execute('SELECT id FROM events').fetchone()[0]
    report={'public_sources':{'organizer':{'url':'https://example.com/event/2020','evidence':'Edition dates'}},'proposals':[{'title':'Example Festival 2020','event_ids':[eid],'confidence':'high','original_values':[{'event_id':eid,'occurred_at':None,'end_at':None,'time_precision':'unknown'}],'proposed':{'occurred_at':'2020-06-12','end_at':None,'time_precision':'day','public_source_keys':['organizer'],'location':{'venue':'Example Hall'}}}]}
    return path,eid,report


def test_review_preserves_original_and_reapplies_after_reindex(tmp_path):
    path,eid,report=fixture(tmp_path)
    assert apply_reviewed_report(report,path)['updated']==1
    with transaction(path) as conn:
        row=conn.execute('SELECT * FROM events').fetchone()
        assert row['occurred_at']=='2020-06-12'
        assert json.loads(row['metadata'])['enrichment']['original_values']['occurred_at'] is None
        conn.execute("UPDATE events SET occurred_at=NULL,time_precision='unknown',metadata='{}'")
    assert reapply(path)==1
    with transaction(path) as conn:
        assert conn.execute('SELECT occurred_at FROM events').fetchone()[0]=='2020-06-12'
        assert conn.execute('SELECT path FROM objects').fetchone()[0]=='original'


def test_review_refuses_stale_values_and_ignores_candidates(tmp_path):
    path,eid,report=fixture(tmp_path)
    report['proposals'][0]['confidence']='candidate'
    assert apply_reviewed_report(report,path)['updated']==0
    report['proposals'][0]['confidence']='high'
    with transaction(path) as conn:conn.execute("UPDATE events SET occurred_at='2021-01-01'")
    with pytest.raises(ValueError,match='changed'):apply_reviewed_report(report,path)
