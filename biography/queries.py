from datetime import datetime, timedelta
from contextlib import contextmanager
import json
import re
import sqlite3
import time
from .db import connect
from .query_stats import get_catalog_stats, get_count

RECORD_QUERY_SECONDS = 8
RECORD_QUERY_MAX_OPCODES = 100_000_000
_RECORD_PROGRESS_INTERVAL = 1000


class RecordQueryTimeout(RuntimeError):
    """The record request exhausted its own execution budget."""


class _RecordBudget:
    def __init__(self):
        self.deadline = time.monotonic() + RECORD_QUERY_SECONDS
        self.opcodes = 0
        self.exhausted = False

    def _expired(self):
        self.exhausted |= (self.opcodes >= RECORD_QUERY_MAX_OPCODES or time.monotonic() >= self.deadline)
        return self.exhausted

    def progress(self):
        self.opcodes += _RECORD_PROGRESS_INTERVAL
        return int(self._expired())

    def check(self):
        if self._expired():
            raise RecordQueryTimeout("Record query exceeded its execution budget")


@contextmanager
def _record_connection(path, budget):
    conn = None
    try:
        conn = connect(path)
        conn.execute("PRAGMA query_only=ON")
        # Lock waits do not call SQLite's progress handler. Keep each wait short
        # on this read connection, without changing the writer's durability.
        conn.execute("PRAGMA busy_timeout=1000")
        conn.set_progress_handler(budget.progress, _RECORD_PROGRESS_INTERVAL)
        budget.check()
        yield conn
        budget.check()
    except sqlite3.OperationalError as error:
        if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT and budget.exhausted:
            raise RecordQueryTimeout("Record query exceeded its execution budget") from error
        raise
    finally:
        if conn is not None:
            conn.close()

def decode_event(row):
    value = dict(row)
    value['metadata'] = json.loads(value.get('metadata') or '{}')
    return value


def _presentation(conn, items, provenance_by_event=None, *, budget=None):
    """Classify only returned rows; raw catalog values and filters stay intact."""
    if not items:
        return
    if provenance_by_event is None:
        ids = [item['id'] for item in items]
        provenance_by_event = {identity: [] for identity in ids}
        placeholders = ','.join('?' for _ in ids)
        rows = conn.execute('''SELECT v.event_id,f.id AS file_id,
            f.original_path,f.object_hash,v.locator,v.parser
            FROM evidence v JOIN files f ON f.id=v.file_id
            WHERE v.event_id IN (''' + placeholders + ')', ids)
        for row in rows:
            if budget is not None:budget.check()
            provenance_by_event[row['event_id']].append(dict(row))
    from .classification import classify_event
    for item in items:
        if budget is not None:budget.check()
        hints = classify_event({**item, 'source_name': item.get('source')}, provenance_by_event.get(item['id'], []))
        item['classification'] = hints
        item['source_label'] = hints['source_label']
        item['display_category'] = hints['category']
        for key in ('display_class','is_raw_sample','metric','metrics','date_basis','sample_family'):
            item[key] = hints[key]

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

def list_events(path=None, *,q='',source='',category='',start='',end='',limit=50,offset=0,sort='date',direction='desc',undated=False):
    where,args=event_filters(q,source,category,start,end,undated)
    if direction not in {'asc','desc'}:raise ValueError('Invalid record sort direction')
    if sort in {'asc','desc'}:sort,direction='date',sort
    if sort not in {'date','imported'} or direction not in {'asc','desc'}:
        raise ValueError('Invalid record sort')
    order=direction.upper()
    # events_time is (occurred_at DESC,id ASC). Scan that index in either
    # direction; an "occurred_at IS NULL" expression forces a full-table sort.
    # Explicit NULLS LAST preserves undated placement for ascending pages too.
    tie_order='DESC' if order=='ASC' else 'ASC'
    tie='e.rowid' if source or category else 'e.id'
    ordering=f'e.occurred_at {order} NULLS LAST,{tie} {tie_order}' if sort=='date' else f'e.rowid {order}'
    event_table='events e NOT INDEXED' if sort=='imported' else 'events e'
    limit,offset=min(max(limit,1),200),max(offset,0)
    budget=_RecordBudget()
    with _record_connection(path,budget) as conn:
        conn.execute('BEGIN')
        rows=conn.execute('SELECT e.*,s.name AS source FROM '+event_table+' JOIN sources s ON s.id=e.source_id'+where+
            f' ORDER BY {ordering} LIMIT ? OFFSET ?',args+[limit+1,offset]).fetchall()
        has_more=len(rows)>limit
        budget.check()
        items=[decode_event(r) for r in rows[:limit]]
        _presentation(conn, items, budget=budget)
    # A complete page gives an exact count for its own snapshot. An empty page
    # beyond the beginning cannot distinguish the last offset from an overrun.
    if not has_more and (offset==0 or items):
        count={'total':offset+len(items),'total_status':'current','counted_at':None}
    else:
        count=get_count(path,where,args)
    from .media import media_for_events
    with _record_connection(path,budget) as conn:
        attachments=media_for_events([item['id'] for item in items],conn)
    for item in items:item['media']=attachments.get(item['id'],[])
    return {'items':items,**count,'has_more':has_more,'limit':limit,'offset':offset}

def event_detail(event_id,path=None):
    budget=_RecordBudget()
    with _record_connection(path,budget) as conn:
        row=conn.execute('SELECT e.*,s.name AS source FROM events e JOIN sources s ON s.id=e.source_id WHERE e.id=?',(event_id,)).fetchone()
        if not row:return None
        result=decode_event(row)
        result['provenance']=[dict(r) for r in conn.execute('''SELECT f.id AS file_id,f.original_path AS path,f.object_hash AS sha256,v.locator,v.parser
            FROM evidence v JOIN files f ON f.id=v.file_id WHERE v.event_id=? ORDER BY f.original_path''',(event_id,))]
        _presentation(conn, [result], {event_id: result['provenance']}, budget=budget)
        from .media import media_for_event
        result['media']=media_for_event(event_id,conn,include_unresolved=True)
        return result

def overview(path=None):
    conn=connect(path)
    try:
        light={'sources_count':conn.execute('SELECT count(*) FROM sources').fetchone()[0],
            'files_count':conn.execute('SELECT count(*) FROM files').fetchone()[0],
            'stored_bytes':conn.execute('SELECT coalesce(sum(bytes),0) FROM objects').fetchone()[0]}
    finally:conn.close()
    measured=get_catalog_stats(path)
    return {**light,**{key:measured[key] for key in ('total_events','undated_count','date_range','stats')},
            'activity':measured['activity'] or [],'categories':measured['categories'] or []}

def sources(path=None):
    conn=connect(path)
    try:
        rows=conn.execute('''SELECT s.*,
          (SELECT count(*) FROM files f WHERE f.source_id=s.id) file_count,
          (SELECT count(*) FROM files f WHERE f.source_id=s.id AND f.parse_status IN ('error','needs_fallback','partial')) review_file_count,
          (SELECT coalesce(sum(bytes),0) FROM files f WHERE f.source_id=s.id) bytes
          FROM sources s ORDER BY s.queue_order,s.name''').fetchall()
    finally:conn.close()
    measured=get_catalog_stats(path)
    by_source=measured['by_source'] or {}
    items=[]
    for row in rows:
        v=dict(row);v['roots']=json.loads(v['roots']);v['notes']=json.loads(v['notes'])
        source_stats=by_source.get(v['id'],{})
        v.update(event_count=source_stats.get('total_events'),
                 first_event=source_stats.get('date_range',{}).get('start'),
                 last_event=source_stats.get('date_range',{}).get('end'),
                 stats={**measured['stats'],'counted_events':source_stats.get('counted_events')})
        if v['review_file_count']:
            if v['status']=='ready':v['status']='partial'
            note=f"{v['review_file_count']} retained files need further content extraction."
            if note not in v['notes']:v['notes'].append(note)
        items.append(v)
    return {'items':items,'stats':measured['stats']}

def imports(path=None, *,sort='date',direction='desc',limit=100,offset=0):
    fields={'date':'i.started_at','name':'s.name COLLATE NOCASE','records':'i.events',
            'files':'i.total_files','bytes':'i.total_bytes','status':'i.status'}
    if sort not in fields or direction not in {'asc','desc'}:raise ValueError('Invalid import sort')
    limit,offset=min(max(int(limit),1),200),max(int(offset),0)
    conn=connect(path)
    try:
        rows=conn.execute('SELECT i.*,s.name AS source FROM imports i JOIN sources s ON s.id=i.source_id '+
            f'ORDER BY {fields[sort]} {direction.upper()} NULLS LAST,i.id ASC LIMIT ? OFFSET ?',
            (limit+1,offset)).fetchall()
        return {'items':[dict(r) for r in rows[:limit]],'limit':limit,'offset':offset,'has_more':len(rows)>limit}
    finally:conn.close()
