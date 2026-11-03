import json
from contextlib import contextmanager
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


def test_source_reapply_drives_from_sparse_reviews_and_preserves_other_sources(tmp_path,monkeypatch):
    from biography import enrichment
    path,eid,report=fixture(tmp_path)
    apply_reviewed_report(report,path)
    other=register_source('Other synthetic source',[],0,1,path)
    with transaction(path) as conn:
        sid=conn.execute('SELECT source_id FROM events WHERE id=?',(eid,)).fetchone()[0]
        payload=conn.execute('SELECT payload FROM event_enrichments WHERE event_id=?',(eid,)).fetchone()[0]
        conn.execute("UPDATE events SET occurred_at=NULL,time_precision='unknown',metadata='{}' WHERE id=?",(eid,))
        conn.execute("INSERT INTO events(id,source_id,title,category,created_at) VALUES('other-event',?,'Other fixture','note','now')",(other,))
        conn.execute("INSERT INTO event_enrichments VALUES('other-event',?,'now')",(payload,))
        # Durable reviews may outlive a derived event after reindexing.
        conn.execute("INSERT INTO event_enrichments VALUES('missing-event',?,'now')",(payload,))
        conn.executemany("INSERT INTO events(id,source_id,title,category,created_at) VALUES(?,?,'Unreviewed fixture','note','now')",[(f'bulk-{i}',sid) for i in range(100)])
    statements=[]
    original=enrichment.write_transaction
    @contextmanager
    def traced(path=None):
        with original(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(enrichment,'write_transaction',traced)
    assert reapply(path,source_id=sid)==1
    sql=next(statement for statement in statements if statement.startswith('SELECT r.* FROM event_enrichments'))
    with transaction(path) as conn:
        plan=[row['detail'] for row in conn.execute('EXPLAIN QUERY PLAN '+sql)]
        assert plan[0].startswith('SCAN r')
        assert any('SEARCH e USING INDEX sqlite_autoindex_events_1 (id=?)' in row for row in plan)
        assert not any('events_source' in row for row in plan)
        assert conn.execute('SELECT occurred_at FROM events WHERE id=?',(eid,)).fetchone()[0]=='2020-06-12'
        assert conn.execute("SELECT occurred_at FROM events WHERE id='other-event'").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM events WHERE id LIKE 'bulk-%' AND occurred_at IS NOT NULL").fetchone()[0]==0
