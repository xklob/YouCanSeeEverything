"""Sparse media discovery and bounded, restartable legacy-catalog backfill.

The event stream records candidates while metadata is already parsed. Native
keys are an independent lookup for manifests arriving later; a native key alone
never creates a media candidate or an empty media-resolution state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import time

from .locations import _connection
from .config import settings

CANDIDATE_VERSION = 1
EXPLICIT = 1
DIRECT = 2
MANIFEST = 4


def ensure_candidate_schema(conn):
    for statement in (
        """CREATE TABLE IF NOT EXISTS media_candidates (
            event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL,native_id TEXT,reason INTEGER NOT NULL,
            dirty INTEGER NOT NULL DEFAULT 1)""",
        "CREATE INDEX IF NOT EXISTS media_candidates_source ON media_candidates(source_id,event_id)",
        """CREATE TABLE IF NOT EXISTS media_native_keys (
            event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL,native_id TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS media_native_lookup ON media_native_keys(source_id,native_id,event_id)",
        """CREATE TABLE IF NOT EXISTS media_candidate_backfill (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL,
            watermark_rowid INTEGER NOT NULL,watermark_event_id TEXT,
            checkpoint_rowid INTEGER NOT NULL,processed_records INTEGER NOT NULL,
            updated_at TEXT NOT NULL)""",
    ):
        conn.execute(statement)


def _metadata(value):
    if isinstance(value,dict):return value
    try:value=json.loads(value or '{}')
    except (ValueError,TypeError):return {}
    return value if isinstance(value,dict) else {}


def _direct_events(conn,event_ids):
    if not event_ids:return set()
    placeholders=','.join('?' for _ in event_ids)
    # The category index can be much larger than this bounded ID list. Force
    # primary-key lookup and join order, including after ANALYZE/live growth.
    rows=conn.execute("""SELECT DISTINCT e.id FROM events e INDEXED BY sqlite_autoindex_events_1
        CROSS JOIN evidence v ON v.event_id=e.id
        CROSS JOIN media_file_index m ON m.file_id=v.file_id
        WHERE e.id IN ("""+placeholders+") AND e.category IN ('media','document')",event_ids)
    return {row[0] for row in rows}


def record_media_candidate(conn,event_id,source_id,native_id,file_id,category,metadata,*,direct_media=None,is_new=False):
    """Record one parsed event in its caller's transaction, without schema DDL.

    Pass direct_media from the file's one-time byte probe and is_new from the
    event INSERT. The common new native-null/no-ref record performs no SQL.
    Shared evidence and explicit mutation refreshes must use is_new=False.
    """
    from .media import explicit_references
    native=str(native_id) if native_id is not None else None
    if native is not None:
        conn.execute("""INSERT INTO media_native_keys(event_id,source_id,native_id) VALUES(?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET source_id=excluded.source_id,native_id=excluded.native_id""",
            (event_id,source_id,native))
    elif not is_new:
        conn.execute('DELETE FROM media_native_keys WHERE event_id=?',(event_id,))
    reason=EXPLICIT if explicit_references(_metadata(metadata)) else 0
    if category=='media' or direct_media:
        reason|=DIRECT
    elif category=='document' and (direct_media is None or not is_new):
        if event_id in _direct_events(conn,[event_id]):reason|=DIRECT
    if native is not None and conn.execute('SELECT 1 FROM media_manifest_refs WHERE source_id=? AND native_id=? LIMIT 1',(source_id,native)).fetchone():
        reason|=MANIFEST
    if reason:
        conn.execute("""INSERT INTO media_candidates(event_id,source_id,native_id,reason,dirty) VALUES(?,?,?,?,1)
            ON CONFLICT(event_id) DO UPDATE SET source_id=excluded.source_id,native_id=excluded.native_id,
            reason=excluded.reason,dirty=1""",(event_id,source_id,native,reason))
    elif not is_new:
        for table in ('media_candidates','event_media_refs','media_event_state'):
            conn.execute(f'DELETE FROM {table} WHERE event_id=?',(event_id,))
    return bool(reason)


def refresh_media_candidates(conn,event_ids):
    """Re-evaluate bounded touched IDs, including removed/shared evidence."""
    event_ids=list(dict.fromkeys(event_ids));candidates=set()
    for start in range(0,len(event_ids),800):
        batch=event_ids[start:start+800]
        rows=conn.execute('SELECT id,source_id,native_id,category,metadata FROM events WHERE id IN ('+
                          ','.join('?' for _ in batch)+')',batch).fetchall()
        candidates.update(_record_rows(conn,rows))
    return candidates


def _record_rows(conn,rows):
    direct_ids=[row['id'] for row in rows if row['category'] in {'media','document'}]
    direct=_direct_events(conn,direct_ids) if direct_ids else set()
    candidates=set();irrelevant=[];null_native=[]
    for row in rows:
        if record_media_candidate(conn,row['id'],row['source_id'],row['native_id'],None,row['category'],row['metadata'],
                                  direct_media=row['id'] in direct,is_new=True):
            candidates.add(row['id'])
        else:irrelevant.append(row['id'])
        if row['native_id'] is None:null_native.append(row['id'])
    # Legacy empty states disappear gradually with the committed backfill.
    # Preserve every original event/evidence row and every nonnull native key.
    for table,ids in [(name,irrelevant) for name in ('media_candidates','event_media_refs','media_event_state')]+[('media_native_keys',null_native)]:
        if ids:conn.execute(f'DELETE FROM {table} WHERE event_id IN ('+','.join('?' for _ in ids)+')',ids)
    return candidates


def enqueue_file_candidates(conn,file_id):
    """Newly classified bytes can make old direct-source evidence previewable."""
    conn.execute("""INSERT INTO media_candidates(event_id,source_id,native_id,reason,dirty)
        SELECT DISTINCT e.id,e.source_id,e.native_id,?,1 FROM evidence v INDEXED BY evidence_file
        JOIN events e ON e.id=v.event_id JOIN media_file_index m ON m.file_id=v.file_id
        WHERE v.file_id=? AND e.category IN ('media','document')
        ON CONFLICT(event_id) DO UPDATE SET reason=media_candidates.reason|excluded.reason,dirty=1""",
        [DIRECT,file_id])
    conn.execute("""UPDATE media_candidates SET dirty=1 WHERE event_id IN
        (SELECT event_id FROM event_media_refs INDEXED BY event_media_file WHERE file_id=?)""",(file_id,))


def enqueue_manifest_candidates(conn,source_id,native_id):
    conn.execute("""INSERT INTO media_candidates(event_id,source_id,native_id,reason,dirty)
        SELECT event_id,source_id,native_id,?,1 FROM media_native_keys WHERE source_id=? AND native_id=?
        ON CONFLICT(event_id) DO UPDATE SET reason=media_candidates.reason|excluded.reason,dirty=1""",
        (MANIFEST,source_id,native_id))


def _head(conn):
    row=conn.execute('SELECT rowid,id FROM events ORDER BY rowid DESC LIMIT 1').fetchone()
    return (row[0],row[1]) if row else (0,None)


def _status(conn):
    current,_=_head(conn)
    exists=conn.execute("SELECT 1 FROM sqlite_schema WHERE name='media_candidate_backfill' AND type='table'").fetchone()
    row=conn.execute('SELECT * FROM media_candidate_backfill WHERE singleton=1').fetchone() if exists else None
    result={'version':CANDIDATE_VERSION,'status':'not_started','watermark_rowid':0,'checkpoint_rowid':0,
        'processed_records':0,'current_rowid':current,'has_more':current>0,'discovery_complete_for_watermark':False,
        'limitations':['A rowid checkpoint covers observed inserts, not arbitrary unhooked event/evidence updates. Use explicit touched-ID refresh or reset backfill after such mutations.']}
    if row:
        result.update(dict(row));result.pop('singleton',None)
        anchor=conn.execute('SELECT id FROM events WHERE rowid=?',(row['watermark_rowid'],)).fetchone() if row['watermark_rowid'] else None
        changed=bool(row['watermark_rowid'] and (anchor is None or anchor[0]!=row['watermark_event_id'])) or current<row['checkpoint_rowid']
        complete=row['checkpoint_rowid']>=row['watermark_rowid']
        valid_version=row['version']==CANDIDATE_VERSION
        result.update(status='rebuild_required' if not valid_version or changed else 'complete' if complete else 'paused',
            has_more=not valid_version or changed or not complete or current>row['watermark_rowid'],discovery_complete_for_watermark=complete and not changed and valid_version)
    result['append_backlog_rowid_span']=max(0,current-result['watermark_rowid'])
    return result


def _queue_status(conn):
    from .media import ASSOCIATION_VERSION
    exists=conn.execute("SELECT 1 FROM sqlite_schema WHERE name='media_candidates' AND type='table'").fetchone()
    if not exists:return {'pending_candidates':0,'queue_drained':True,'candidate_count':0}
    # Derived tables only. No metadata decoding or scan of the source events.
    pending=conn.execute("""SELECT count(*) FROM media_candidates c
        LEFT JOIN media_event_state st ON st.event_id=c.event_id
        LEFT JOIN media_source_generation g ON g.source_id=c.source_id
        WHERE c.dirty=1 OR st.event_id IS NULL OR st.version<>?
        OR (st.needs_refresh=1 AND st.generation<>coalesce(g.generation,0))""",(ASSOCIATION_VERSION,)).fetchone()[0]
    return {'pending_candidates':pending,'queue_drained':pending==0,
            'candidate_count':conn.execute('SELECT count(*) FROM media_candidates').fetchone()[0]}


def media_candidate_status(path=None):
    """Read-only status; no discovery, index creation, or count of all events."""
    if not isinstance(path,sqlite3.Connection) and not Path(path or settings.database).exists():
        return {'version':CANDIDATE_VERSION,'status':'not_started','watermark_rowid':0,'checkpoint_rowid':0,
            'processed_records':0,'current_rowid':0,'has_more':False,'discovery_complete_for_watermark':False,'append_backlog_rowid_span':0,
            'pending_candidates':0,'queue_drained':True,'candidate_count':0}
    with _connection(path) as conn:return {**_status(conn),**_queue_status(conn)}


def backfill_media_candidates(path=None,*,batch_size=500,max_batches=1,max_seconds=2,reset=False):
    """Discover old candidates in atomic batches, resuming a fixed high rowid.

    Newly appended rows become a new fixed target only after the previous
    target is committed. Caller-supplied connections retain transaction control.
    This function does not index files, reconcile attachments, or touch sources.
    """
    from .media import ensure_schema
    batch_size=max(1,min(800,int(batch_size)));max_batches=max(0,int(max_batches));deadline=time.monotonic()+max(0,float(max_seconds))
    owned=not isinstance(path,sqlite3.Connection)
    with _connection(path) as conn:
        ensure_schema(conn)
        if reset:conn.execute('DELETE FROM media_candidate_backfill')
        state=_status(conn)
        if state['status']=='rebuild_required':raise ValueError('Media candidate backfill requires reset after version/rowid change')
        if state['status']=='not_started' or (state['status']=='complete' and state['append_backlog_rowid_span']):
            head,anchor=_head(conn);checkpoint=state['checkpoint_rowid'];processed=state['processed_records']
            conn.execute('INSERT OR REPLACE INTO media_candidate_backfill VALUES(1,?,?,?,?,?,?)',
                (CANDIDATE_VERSION,head,anchor,checkpoint,processed,datetime.now(timezone.utc).isoformat()))
        if owned:conn.commit()
        batches=0;discovered=0
        while batches<max_batches and time.monotonic()<deadline:
            # Read current metadata under the same bounded writer transaction
            # as eligibility/checkpoint updates. A concurrent mutation hook
            # cannot be overwritten by a stale discovery decision.
            if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
            state=_status(conn)
            if state['status']=='rebuild_required':raise ValueError('Media candidate backfill requires reset after version/rowid change')
            if state['checkpoint_rowid']>=state['watermark_rowid']:
                if owned:conn.commit()
                break
            rows=conn.execute('SELECT rowid AS catalog_rowid,id,source_id,native_id,category,metadata FROM events WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',
                (state['checkpoint_rowid'],state['watermark_rowid'],batch_size)).fetchall()
            conn.execute('SAVEPOINT media_candidate_batch')
            try:
                discovered+=len(_record_rows(conn,rows))
                checkpoint=rows[-1]['catalog_rowid'] if rows else state['watermark_rowid']
                conn.execute('UPDATE media_candidate_backfill SET checkpoint_rowid=?,processed_records=processed_records+?,updated_at=? WHERE singleton=1',
                    (checkpoint,len(rows),datetime.now(timezone.utc).isoformat()))
                conn.execute('RELEASE media_candidate_batch')
            except BaseException:
                conn.execute('ROLLBACK TO media_candidate_batch');conn.execute('RELEASE media_candidate_batch');raise
            if owned:conn.commit()
            batches+=1
        return {**_status(conn),**_queue_status(conn),'batches':batches,'discovered_candidates':discovered}


__all__=['record_media_candidate','refresh_media_candidates','backfill_media_candidates','media_candidate_status']
