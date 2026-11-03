"""Apply reviewed supplemental evidence without changing retained source bytes."""
from datetime import date
import hashlib
import json
from .db import transaction, now


def ensure_schema(conn):
    # Independent of the derived events table so reindexing preserves reviews.
    conn.execute('''CREATE TABLE IF NOT EXISTS event_enrichments (
        event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, reviewed_at TEXT NOT NULL)''')


def _apply(conn, event_id, payload):
    row=conn.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
    if not row:return False
    metadata=json.loads(row['metadata'])
    current=metadata.get('enrichment')
    original=payload['original_values']
    if not current and any(row[k]!=original.get(k) for k in ('occurred_at','end_at','time_precision')):
        raise ValueError('Source values changed after the enrichment was reviewed')
    metadata['enrichment']=payload
    conn.execute('UPDATE events SET occurred_at=?,end_at=?,time_precision=?,metadata=? WHERE id=?',
        (payload['occurred_at'],payload.get('end_at'),payload['time_precision'],json.dumps(metadata,ensure_ascii=False),event_id))
    return True


def apply_reviewed_report(report, path=None):
    from .daily import begin_daily_mutation,end_daily_mutation
    token=begin_daily_mutation(catalog_path=path,reason='reviewed_enrichment')
    try:return _apply_reviewed_report(report,path)
    finally:end_daily_mutation(token,catalog_path=path)


def _apply_reviewed_report(report, path=None):
    """Apply high-confidence proposals only; candidates remain untouched.

    Callers must review the report before invoking this operation. No external
    page, source export, or model output can invoke it automatically.
    """
    digest=hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()
    reviewed=now();count=0
    with transaction(path) as conn:
        ensure_schema(conn)
        for proposal in report.get('proposals',[]):
            if proposal.get('confidence')!='high':continue
            proposed=proposal['proposed']
            if proposed.get('time_precision')!='day':raise ValueError('Expected reviewed day precision')
            date.fromisoformat(proposed['occurred_at'])
            if proposed.get('end_at'):date.fromisoformat(proposed['end_at'])
            originals={v['event_id']:v for v in proposal['original_values']}
            evidence=[report['public_sources'][key] for key in proposed.get('public_source_keys',[])
                      if not report['public_sources'][key].get('access_status')]
            for event_id in proposal['event_ids']:
                row=conn.execute('SELECT title FROM events WHERE id=?',(event_id,)).fetchone()
                if not row or row['title']!=proposal['title']:raise ValueError('Reviewed event identity does not match')
                original=originals[event_id]
                payload={**proposed,'confidence':'high','provenance':evidence,'reviewed_at':reviewed,
                    'report_sha256':digest,'original_values':{k:original.get(k) for k in ('occurred_at','end_at','time_precision')},
                    'local_evidence':original.get('provenance',[]),
                    'supporting_event_ids':[v['event_id'] for v in proposal.get('local_booking_candidates',[])]}
                if _apply(conn,event_id,payload):count+=1
                conn.execute('INSERT INTO event_enrichments VALUES(?,?,?) ON CONFLICT(event_id) DO UPDATE SET payload=excluded.payload,reviewed_at=excluded.reviewed_at',
                    (event_id,json.dumps(payload,ensure_ascii=False),reviewed))
    return {'updated':count,'report_sha256':digest}


def reapply(path=None, *,source_id=None):
    from .daily import begin_daily_mutation,end_daily_mutation
    token=begin_daily_mutation(catalog_path=path,reason='reapply_enrichment')
    try:return _reapply(path,source_id=source_id)
    finally:end_daily_mutation(token,catalog_path=path)


def _reapply(path=None, *,source_id=None):
    with transaction(path) as conn:
        ensure_schema(conn)
        # Reviews are sparse. Keep them outermost so a source filter cannot
        # turn reapplication into a scan of every event in that source.
        sql='SELECT r.* FROM event_enrichments r CROSS JOIN events e ON e.id=r.event_id'
        if source_id:sql+=' WHERE e.source_id=?'
        return sum(_apply(conn,row['event_id'],json.loads(row['payload']))
                   for row in conn.execute(sql,(source_id,) if source_id else ()).fetchall())
