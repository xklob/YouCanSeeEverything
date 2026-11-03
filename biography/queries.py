from datetime import datetime, timedelta
import json
import re
from .db import connect

def decode_event(row):
    value = dict(row)
    value['metadata'] = json.loads(value.get('metadata') or '{}')
    return value

def event_filters(q='',source='',category='',start='',end='',undated=False):
    clauses, args = [], []
    if q.strip():
        tokens = re.findall(r'\S+',q)[:30]
        query = ' AND '.join('"'+token.replace('"','""')+'"' for token in tokens)
        clauses.append('e.rowid IN (SELECT rowid FROM events_fts WHERE events_fts MATCH ?)')
        args.append(query)
    if source:
        clauses.append('e.source_id=?'); args.append(source)
    if category:
        clauses.append('e.category=?'); args.append(category)
    if start:
        clauses.append('e.occurred_at>=?'); args.append(start)
    if end:
        if len(end)==10:
            end=(datetime.fromisoformat(end)+timedelta(days=1)).date().isoformat()
            clauses.append('e.occurred_at<?')
        else:
            clauses.append('e.occurred_at<=?')
        args.append(end)
    if undated:
        clauses.append('e.occurred_at IS NULL')
    return (' WHERE '+' AND '.join(clauses) if clauses else ''),args

def list_events(path=None, *,q='',source='',category='',start='',end='',limit=50,offset=0,sort='desc',undated=False):
    where,args=event_filters(q,source,category,start,end,undated)
    order='ASC' if sort=='asc' else 'DESC'
    conn=connect(path)
    try:
        total=conn.execute('SELECT count(*) FROM events e'+where,args).fetchone()[0]
        rows=conn.execute('SELECT e.*,s.name AS source FROM events e JOIN sources s ON s.id=e.source_id'+where+
            f' ORDER BY e.occurred_at IS NULL,e.occurred_at {order},e.id {order} LIMIT ? OFFSET ?',args+[min(max(limit,1),200),max(offset,0)]).fetchall()
        items=[decode_event(r) for r in rows]
        from .media import media_for_events
        attachments=media_for_events([item['id'] for item in items],path)
        for item in items:item['media']=attachments.get(item['id'],[])
        return {'items':items,'total':total,'limit':limit,'offset':offset}
    finally: conn.close()

def event_detail(event_id,path=None):
    conn=connect(path)
    try:
        row=conn.execute('SELECT e.*,s.name AS source FROM events e JOIN sources s ON s.id=e.source_id WHERE e.id=?',(event_id,)).fetchone()
        if not row:return None
        result=decode_event(row)
        result['provenance']=[dict(r) for r in conn.execute('''SELECT f.id AS file_id,f.original_path AS path,f.object_hash AS sha256,v.locator,v.parser
            FROM evidence v JOIN files f ON f.id=v.file_id WHERE v.event_id=? ORDER BY f.original_path''',(event_id,))]
        from .media import media_for_event
        result['media']=media_for_event(event_id,path,include_unresolved=True)
        return result
    finally:conn.close()

def overview(path=None):
    conn=connect(path)
    try:
        counts=conn.execute('SELECT count(*) total_events,min(occurred_at) start,max(occurred_at) end,sum(occurred_at IS NULL) undated FROM events').fetchone()
        return {'total_events':counts['total_events'],'sources_count':conn.execute('SELECT count(*) FROM sources').fetchone()[0],
            'files_count':conn.execute('SELECT count(*) FROM files').fetchone()[0],
            'stored_bytes':conn.execute('SELECT coalesce(sum(bytes),0) FROM objects').fetchone()[0],
            'date_range':{'start':counts['start'],'end':counts['end']},'undated_count':counts['undated'] or 0,
            'activity':[dict(r) for r in conn.execute("SELECT substr(occurred_at,1,7) month,count(*) count FROM events WHERE occurred_at IS NOT NULL GROUP BY 1 ORDER BY 1")],
            'categories':[dict(r) for r in conn.execute('SELECT category name,count(*) count FROM events GROUP BY category ORDER BY count(*) DESC')]}
    finally:conn.close()

def sources(path=None):
    conn=connect(path)
    try:
        rows=conn.execute('''SELECT s.*,
          (SELECT count(*) FROM files f WHERE f.source_id=s.id) file_count,
          (SELECT count(*) FROM events e WHERE e.source_id=s.id) event_count,
          (SELECT coalesce(sum(bytes),0) FROM files f WHERE f.source_id=s.id) bytes,
          (SELECT min(occurred_at) FROM events e WHERE e.source_id=s.id) first_event,
          (SELECT max(occurred_at) FROM events e WHERE e.source_id=s.id) last_event
          FROM sources s ORDER BY s.queue_order,s.name''').fetchall()
        items=[]
        for row in rows:
            v=dict(row);v['roots']=json.loads(v['roots']);v['notes']=json.loads(v['notes']);items.append(v)
        return {'items':items}
    finally:conn.close()

def imports(path=None):
    conn=connect(path)
    try:
        return {'items':[dict(r) for r in conn.execute('SELECT i.*,s.name AS source FROM imports i JOIN sources s ON s.id=i.source_id ORDER BY started_at DESC LIMIT 100')]}
    finally:conn.close()
