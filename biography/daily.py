"""Rebuildable daily projections in a separate, incrementally committed sidecar.

Catalog rows and source files are never rewritten. A build fixes a committed
rowid ceiling, releases its catalog read snapshot after each bounded batch, and
commits projections and checkpoint together. Source edits require invalidation
and an explicit rebuild; append checkpoints are not a mutation audit.
"""
from __future__ import annotations

from collections import OrderedDict,defaultdict
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from .config import settings
from .storage import ImportPaused,fsync_directory

SCHEMA_VERSION=2
REBUILD_AGE_HOURS=24
LIMITATIONS=[
    "Projection covers committed catalog rows through its checkpoint, not all possible source history.",
    "Append indexing does not detect every in-place edit or deletion. Invalidate and rebuild after enrichment, reparsing, deletion, or VACUUM.",
    "Anchor/file-identity checks detect some replacements; a daily age limit also requests a fresh rebuild.",
    "UTC instants, source date-only values, and timezone-unknown wall dates use separate aggregation bases.",
    "Measurement ranges and means describe observed samples; means are unweighted, not time averages.",
]

SCHEMA="""
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS builds(id TEXT PRIMARY KEY,watermark_rowid INTEGER NOT NULL,start_rowid INTEGER NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS day_counts(id TEXT PRIMARY KEY,day TEXT NOT NULL,source_id TEXT NOT NULL,source TEXT NOT NULL,category TEXT NOT NULL,display_class TEXT NOT NULL,date_basis TEXT NOT NULL,observed_records INTEGER NOT NULL DEFAULT 0,raw_samples INTEGER NOT NULL DEFAULT 0,substantive_records INTEGER NOT NULL DEFAULT 0,technical_records INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS day_counts_day ON day_counts(day,source_id,category);
CREATE TABLE IF NOT EXISTS display_groups(id TEXT PRIMARY KEY,day TEXT NOT NULL,source_id TEXT NOT NULL,source TEXT NOT NULL,category TEXT NOT NULL,display_class TEXT NOT NULL,date_basis TEXT NOT NULL,representative_rowid INTEGER NOT NULL,record_count INTEGER NOT NULL,title TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS display_groups_day ON display_groups(day,source_id,category,display_class,representative_rowid);
CREATE TABLE IF NOT EXISTS group_members(catalog_rowid INTEGER PRIMARY KEY,group_id TEXT NOT NULL REFERENCES display_groups(id));
CREATE TABLE IF NOT EXISTS rollups(id INTEGER PRIMARY KEY,stable_id TEXT UNIQUE NOT NULL,day TEXT NOT NULL,source_id TEXT NOT NULL,source TEXT NOT NULL,category TEXT NOT NULL,date_basis TEXT NOT NULL,metric TEXT NOT NULL,unit TEXT NOT NULL,device TEXT NOT NULL,stream_key TEXT NOT NULL,aggregation TEXT NOT NULL,source_record_count INTEGER NOT NULL DEFAULT 0,sample_count INTEGER NOT NULL DEFAULT 0,variant_count INTEGER NOT NULL DEFAULT 0,conflicting_occurrences INTEGER NOT NULL DEFAULT 0,numeric_count INTEGER NOT NULL DEFAULT 0,total_value REAL NOT NULL DEFAULT 0,min_value REAL,max_value REAL,min_rowid INTEGER,max_rowid INTEGER,first_at TEXT,last_at TEXT,first_rowid INTEGER,last_rowid INTEGER,latest_value REAL,latest_at TEXT,latest_conflicted INTEGER NOT NULL DEFAULT 0,sum_conflicted INTEGER NOT NULL DEFAULT 0,interval_max_end TEXT,numeric_overflow INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS rollups_day ON rollups(day,source_id,category,metric);
CREATE TABLE IF NOT EXISTS sample_refs(catalog_rowid INTEGER NOT NULL,rollup_id INTEGER NOT NULL REFERENCES rollups(id),value REAL,at TEXT,dedup_hash BLOB,is_duplicate INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(catalog_rowid,rollup_id));
CREATE INDEX IF NOT EXISTS sample_refs_rollup ON sample_refs(rollup_id,catalog_rowid);
CREATE UNIQUE INDEX IF NOT EXISTS sample_refs_occurrence ON sample_refs(rollup_id,dedup_hash) WHERE dedup_hash IS NOT NULL AND is_duplicate=0;
CREATE TABLE IF NOT EXISTS source_occurrences(stream_key TEXT NOT NULL,occurrence_hash BLOB NOT NULL,signature BLOB NOT NULL,conflicted INTEGER NOT NULL DEFAULT 0,first_rowid INTEGER NOT NULL,conflict_rowid INTEGER,PRIMARY KEY(stream_key,occurrence_hash));
CREATE TABLE IF NOT EXISTS rollup_occurrences(rollup_id INTEGER NOT NULL,stream_key TEXT NOT NULL,occurrence_hash BLOB NOT NULL,first_numeric INTEGER NOT NULL DEFAULT 0,conflicted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(rollup_id,occurrence_hash));
CREATE INDEX IF NOT EXISTS rollup_occurrence_lookup ON rollup_occurrences(stream_key,occurrence_hash);
CREATE VIRTUAL TABLE IF NOT EXISTS projection_fts USING fts5(kind UNINDEXED,id UNINDEXED,day UNINDEXED,source_id UNINDEXED,category UNINDEXED,label,tokenize='unicode61 remove_diacritics 2');
"""


def _now():return datetime.now(timezone.utc).isoformat()
def _hash(*parts):return hashlib.sha256(json.dumps(parts,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
def _paths(catalog_path=None,sidecar_path=None):
    catalog=Path(catalog_path or settings.database).resolve();sidecar=Path(sidecar_path or getattr(settings,'daily_database',settings.state_dir/'daily'/'projection.sqlite3')).resolve()
    if catalog==sidecar or catalog.exists() and sidecar.exists() and catalog.samefile(sidecar):raise ValueError('Daily sidecar must be a different file from the source catalog')
    return catalog,sidecar


def _metadata(value):
    try:value=json.loads(value) if isinstance(value,str) else value
    except (ValueError,TypeError):return {}
    return value if isinstance(value,dict) else {}


def _sqlite_lock(error):
    """Only SQLite's explicit contention codes/messages are retryable."""
    if not isinstance(error,sqlite3.OperationalError):return False
    code=getattr(error,'sqlite_errorcode',None)
    if isinstance(code,int):return code&0xff in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED}
    message=str(error).strip().casefold()
    return any(message==prefix or message.startswith(prefix+':') for prefix in (
        'database is locked','database table is locked','database schema is locked'))


def _active_mutations(catalog,sidecar):
    result=[]
    for path in (sidecar.parent/'mutations').glob('*.active.json'):
        try:
            value=json.loads(path.read_text())
            if value.get('catalog_path')==str(catalog):result.append(value)
        except (OSError,ValueError):result.append({'reason':'unreadable_mutation_guard'})
    return result


def _write_guard(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_suffix(path.suffix+'.part')
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(descriptor,'w') as handle:
        json.dump(value,handle,sort_keys=True);handle.flush();os.fsync(handle.fileno())
    os.replace(temporary,path)
    fsync_directory(path.parent)


def _mutation_target(catalog_path,sidecar_path):
    catalog,sidecar=_paths(catalog_path,sidecar_path)
    if sidecar_path is None and catalog!=Path(settings.database).resolve():return None
    return catalog,sidecar


def begin_daily_mutation(catalog_path=None,*,sidecar_path=None,reason='source_records_changed'):
    """Configured-catalog-only guard; supports independently nested tokens."""
    target=_mutation_target(catalog_path,sidecar_path)
    if target is None:return None
    catalog,sidecar=target;token=uuid.uuid4().hex
    marker=sidecar.parent/'mutations'/(token+'.active.json')
    _write_guard(marker,{'token':token,'catalog_path':str(catalog),'reason':str(reason),'started_at':_now()})
    invalidate_daily('active_source_mutation:'+str(reason),catalog,sidecar)
    return token


def end_daily_mutation(token,catalog_path=None,*,sidecar_path=None,reason=None):
    if not token:return
    target=_mutation_target(catalog_path,sidecar_path)
    if target is None:return
    catalog,sidecar=target
    if not re.fullmatch(r'[0-9a-f]{32}',str(token)):raise ValueError('Invalid daily mutation token')
    marker=sidecar.parent/'mutations'/(token+'.active.json')
    if not marker.exists():return
    value=json.loads(marker.read_text())
    if value.get('catalog_path')!=str(catalog):raise ValueError('Daily mutation belongs to another catalog')
    invalidate_daily('source_mutation_completed:'+str(reason or value.get('reason')),catalog,sidecar)
    value['completed_at']=_now();_write_guard(marker,value)
    marker.rename(marker.with_name(str(token)+'.completed.json'))
    fsync_directory(marker.parent)


@contextmanager
def _readonly(path,timeout=3):
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=timeout)
    try:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        yield conn
    finally:conn.close()


def _state(conn):return {row['key']:json.loads(row['value']) for row in conn.execute('SELECT key,value FROM state')}
def _set(conn,**values):conn.executemany('INSERT OR REPLACE INTO state(key,value) VALUES(?,?)',[(key,json.dumps(value,sort_keys=True)) for key,value in values.items()])
def _classifier():
    from .classification import CLASSIFIER_VERSION,classify_event
    return CLASSIFIER_VERSION,classify_event


def _catalog_head(path):
    started=time.monotonic()
    try:
        with _readonly(path) as conn:
            row=conn.execute('SELECT rowid,id FROM events ORDER BY rowid DESC LIMIT 1').fetchone()
    except sqlite3.OperationalError as error:
        error.daily_stage='catalog_head';error.daily_read_seconds=time.monotonic()-started;error.daily_busy_timeout=3
        raise
    stat=path.stat()
    return {'max_rowid':row['rowid'] if row else 0,'last_event_id':row['id'] if row else None,'file_identity':[stat.st_dev,stat.st_ino]}


def daily_status(catalog_path=None,sidecar_path=None):
    catalog,sidecar=_paths(catalog_path,sidecar_path)
    result={'status':'not_built','epoch':None,'schema_version':SCHEMA_VERSION,'classifier_version':None,
        'watermark_rowid':0,'checkpoint_rowid':0,'processed_records':0,'current_catalog_max_rowid':None,
        'caught_up_to_watermark':False,'append_backlog_rowid_span':None,'rebuild_required':False,
        'rebuild_reasons':[],'started_at':None,'updated_at':None,'completed_at':None,
        'coverage':{},'limitations':list(LIMITATIONS)}
    if sidecar.exists():
        try:
            with _readonly(sidecar) as conn:result.update(_state(conn))
        except sqlite3.Error:
            result.update(status='unavailable',rebuild_required=True,rebuild_reasons=['sidecar_unreadable'])
    reasons=list(result.get('rebuild_reasons') or [])
    active=_active_mutations(catalog,sidecar);result['source_mutation_active']=bool(active)
    if active:reasons.append('source_mutation_active')
    if result.get('epoch') and result.get('schema_version')!=SCHEMA_VERSION:reasons.append('projection_schema_changed')
    try:
        head=_catalog_head(catalog)
        result['current_catalog_max_rowid']=head['max_rowid']
        if result.get('catalog_file_identity') and result['catalog_file_identity']!=head['file_identity']:reasons.append('catalog_file_replaced')
        if head['max_rowid']<result['checkpoint_rowid']:reasons.append('catalog_rowid_rewound')
        if result.get('anchor_rowid'):
            with _readonly(catalog) as conn:
                anchor=conn.execute('SELECT id FROM events WHERE rowid=?',(result['anchor_rowid'],)).fetchone()
            if not anchor or anchor['id']!=result.get('anchor_event_id'):reasons.append('catalog_anchor_changed')
    except (OSError,sqlite3.Error):result['coverage']['catalog_head']='temporarily_unavailable'
    try:
        version,_=_classifier()
        if result.get('classifier_version') is not None and result['classifier_version']!=version:reasons.append('classifier_version_changed')
    except ImportError:pass
    age_origin=result.get('last_full_rebuild_at') or result.get('started_at')
    if age_origin:
        try:
            if datetime.now(timezone.utc)-datetime.fromisoformat(age_origin)>timedelta(hours=REBUILD_AGE_HOURS):reasons.append('projection_age_requires_rebuild')
        except ValueError:reasons.append('invalid_projection_clock')
    if result.get('invalidated_reason'):reasons.append(result['invalidated_reason'])
    result['rebuild_reasons']=list(dict.fromkeys(reasons))
    result['rebuild_required']=bool(reasons)
    result['caught_up_to_watermark']=result['checkpoint_rowid']>=result['watermark_rowid'] and result['status']!='not_built'
    if result['current_catalog_max_rowid'] is not None:result['append_backlog_rowid_span']=max(0,result['current_catalog_max_rowid']-result['checkpoint_rowid'])
    return result


def invalidate_daily(reason='source_records_changed',catalog_path=None,sidecar_path=None):
    """Explicit sidecar-only invalidation hook for enrichment/reparse/delete."""
    _,sidecar=_paths(catalog_path,sidecar_path)
    if sidecar.exists():
        conn=sqlite3.connect(sidecar,timeout=5)
        try:
            with conn:_set(conn,invalidated_reason=str(reason),invalidated_at=_now())
        finally:conn.close()
    return daily_status(catalog_path,sidecar)


def _capacity(sidecar,paths,reserve,additional):
    for position,root in enumerate([sidecar.parent,*paths]):
        root=Path(root)
        if position and not root.exists():raise ImportPaused('daily_capacity_path_unavailable',path=str(root))
        while not root.exists():root=root.parent
        free=shutil.disk_usage(root).free
        if free<reserve+additional:raise ImportPaused('daily_projection_disk_reserve',path=str(root),free_bytes=free,reserve_bytes=reserve,required_bytes=reserve+additional)


def _day(classified,event):
    value=classified.get('occurred_at',event.get('occurred_at'))
    precision=classified.get('time_precision',event.get('time_precision')) or 'unknown'
    if precision in {'year','month'}:return 'undated','coarse_'+precision,None
    if not isinstance(value,str) or not value:return 'undated','unknown',None
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            day=datetime.fromisoformat(value).date().isoformat()
            basis=str(classified.get('date_basis') or '')
            local=_metadata(event.get('metadata')).get('timezone_unknown') or 'wall' in basis or 'local' in basis
            return day,'source_wall_time' if local else 'source_date',value
        if 'T' in value:
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is not None:
                parsed=parsed.astimezone(timezone.utc)
                return parsed.date().isoformat(),'utc',parsed.isoformat(timespec='microseconds').replace('+00:00','Z')
            return parsed.date().isoformat(),'source_wall_time',parsed.isoformat(timespec='microseconds')
    except (ValueError,OverflowError):pass
    return 'undated','unknown',None


def _provenance(conn,rows,cache):
    ids=[row['id'] for row in rows];links=defaultdict(list);missing=set()
    for start in range(0,len(ids),800):
        batch=ids[start:start+800]
        for row in conn.execute('SELECT event_id,file_id,locator,parser FROM evidence WHERE event_id IN ('+','.join('?' for _ in batch)+')',batch):
            item=dict(row);links[item.pop('event_id')].append(item)
            if item['file_id'] not in cache:missing.add(item['file_id'])
    missing=list(missing)
    for start in range(0,len(missing),800):
        batch=missing[start:start+800]
        for row in conn.execute('SELECT id,original_path,object_hash,metadata FROM files WHERE id IN ('+','.join('?' for _ in batch)+')',batch):
            item=dict(row);identity=item.pop('id')
            item['metadata']=_metadata(item['metadata'])
            cache[identity]=item
    result={}
    for identity,entries in links.items():result[identity]=[{**cache.get(entry['file_id'],{}),**entry} for entry in entries]
    while len(cache)>4096:cache.popitem(last=False)
    return result


def _batch(catalog,after,watermark,size,max_bytes,cache,*,timings=None):
    started=time.monotonic();stage='catalog_open';stages={}
    try:
        with _readonly(catalog,timeout=5) as conn:
            stages[stage]=time.monotonic()-started;stage='source_names';part=time.monotonic()
            conn.execute('BEGIN')
            sources={row['id']:row['name'] for row in conn.execute('SELECT id,name FROM sources')}
            stages[stage]=time.monotonic()-part;stage='event_rows';part=time.monotonic()
            rows=[];bytes_read=0
            for row in conn.execute('SELECT rowid AS catalog_rowid,* FROM events WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',(after,watermark,size)):
                length=len(row['metadata'] or '')+len(row['body'] or '')+len(row['title'] or '')
                if rows and bytes_read+length>max_bytes:break
                rows.append(dict(row));bytes_read+=length
            stages[stage]=time.monotonic()-part;stage='source_evidence';part=time.monotonic()
            provenance=_provenance(conn,rows,cache)
            stages[stage]=time.monotonic()-part;stage='catalog_close';part=time.monotonic()
        stages[stage]=time.monotonic()-part
    except sqlite3.OperationalError as error:
        error.daily_stage=stage;error.daily_read_seconds=time.monotonic()-started;error.daily_busy_timeout=5
        raise
    if timings is not None:timings.update(source_read_seconds=round(time.monotonic()-started,6),source_stage_seconds={name:round(value,6) for name,value in stages.items()})
    for row in rows:
        row['metadata']=_metadata(row['metadata'])
        row['source_name']=sources.get(row['source_id'],'Unknown source')
    return rows,provenance,bytes_read


def _fts(conn,kind,identity,day,source_id,category,label):
    conn.execute('INSERT INTO projection_fts(kind,id,day,source_id,category,label) VALUES(?,?,?,?,?,?)',(kind,str(identity),day,source_id,category,label))


def _write_batch(conn,rows,provenance,classify):
    counts=defaultdict(lambda:[0,0,0,0]);rollup_changes={};rollup_cache={};rollup_by_id={};new_groups=0;new_rollups=0
    for event in rows:
        classified=classify(event,provenance.get(event['id'],[]))
        day,basis,at=_day(classified,event)
        category=str(classified.get('category') or event['category']);source=str(classified.get('source_label') or event['source_name'])
        display=str(classified.get('display_class') or 'document');sample=bool(classified.get('is_raw_sample',classified.get('is_sample',False)))
        dims=(day,event['source_id'],source,category,display,basis);values=counts[dims]
        values[0]+=1;values[1]+=int(sample);values[2]+=int(not sample and display!='technical');values[3]+=int(not sample and display=='technical')
        rowid=event['catalog_rowid'];dedup=classified.get('display_group_key') or classified.get('dedup_key')
        variant=classified.get('variant_key')
        if not sample:
            group_id=_hash(day,basis,event['source_id'],category,display,dedup or ['event',event['id']],variant)
            cursor=conn.execute('''INSERT OR IGNORE INTO display_groups(id,day,source_id,source,category,display_class,date_basis,representative_rowid,record_count,title) VALUES(?,?,?,?,?,?,?,?,0,?)''',(group_id,day,event['source_id'],source,category,display,basis,rowid,event['title'][:1000]))
            if cursor.rowcount:
                new_groups+=1;_fts(conn,'group',group_id,day,event['source_id'],category,source+' '+event['title'][:1000]+' '+event['body'][:400])
            cursor=conn.execute('INSERT OR IGNORE INTO group_members(catalog_rowid,group_id) VALUES(?,?)',(rowid,group_id))
            if cursor.rowcount:conn.execute('UPDATE display_groups SET record_count=record_count+1 WHERE id=?',(group_id,))
            continue
        metrics=classified.get('metrics')
        if not isinstance(metrics,list) or not metrics:metrics=[classified['metric']] if isinstance(classified.get('metric'),dict) else [{'name':classified.get('sample_family') or 'unclassified_health_measurement','value':None,'unit':'','aggregation':'none','device':classified.get('device'),'group_key':classified.get('sample_group_key')}]
        prepared={}
        for raw_metric in metrics:
            if not isinstance(raw_metric,dict):continue
            metric=dict(raw_metric)
            identity=(str(metric.get('name') or ''),str(metric.get('unit') or ''),str(metric.get('device') or classified.get('device') or ''),str(metric.get('aggregation') or 'none'),str(metric.get('group_key') or ''))
            previous=prepared.get(identity)
            if previous is not None:
                if previous.get('value')!=metric.get('value') or previous.get('interval_start')!=metric.get('interval_start') or previous.get('interval_end')!=metric.get('interval_end'):
                    previous['value']=None;previous['_conflicting_fields']=True
            else:prepared[identity]=metric
        for metric in prepared.values():
            name=str(metric.get('name') or 'Unclassified measurement');unit=str(metric.get('unit') or '');device=str(metric.get('device') or classified.get('device') or 'not recorded')
            aggregation=metric.get('aggregation') if metric.get('aggregation') in {'sum','latest','none'} else 'none'
            stream_key=str(metric.get('group_key') or _hash(event['source_id'],source,event.get('metadata',{}).get('account_id'),name,unit,device,aggregation))
            key=(day,basis,event['source_id'],category,name,unit,device,aggregation,stream_key)
            stable=_hash(*key)
            if stable not in rollup_cache:
                cursor=conn.execute('''INSERT OR IGNORE INTO rollups(stable_id,day,source_id,source,category,date_basis,metric,unit,device,stream_key,aggregation) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(stable,day,event['source_id'],source,category,basis,name,unit,device,stream_key,aggregation))
                if cursor.rowcount:
                    new_rollups+=1;_fts(conn,'rollup',stable,day,event['source_id'],category,source+' '+name+' '+unit+' '+device)
                rollup_cache[stable]=dict(conn.execute('SELECT * FROM rollups WHERE stable_id=?',(stable,)).fetchone())
                rollup_changes[stable]=rollup_cache[stable]
                rollup_by_id[rollup_cache[stable]['id']]=rollup_cache[stable]
            rollup=rollup_cache[stable];number=metric.get('value')
            try:number=float(number) if number is not None and not isinstance(number,bool) else None
            except (ValueError,TypeError,OverflowError):number=None
            if number is not None and not math.isfinite(number):number=None
            proof=hashlib.sha256(json.dumps([dedup,variant],sort_keys=True,default=str).encode()).digest() if dedup else None
            cursor=conn.execute('INSERT OR IGNORE INTO sample_refs(catalog_rowid,rollup_id,value,at,dedup_hash,is_duplicate) VALUES(?,?,?,?,?,0)',(rowid,rollup['id'],number,at,proof))
            duplicate=False
            if not cursor.rowcount:
                cursor=conn.execute('INSERT OR IGNORE INTO sample_refs(catalog_rowid,rollup_id,value,at,dedup_hash,is_duplicate) VALUES(?,?,?,?,?,1)',(rowid,rollup['id'],number,at,proof));duplicate=True
            if not cursor.rowcount:continue
            rollup['source_record_count']+=1
            if duplicate:continue
            rollup['variant_count']+=1
            if at and (rollup['first_at'] is None or at<rollup['first_at']):rollup['first_at'],rollup['first_rowid']=at,rowid
            if at and (rollup['last_at'] is None or at>rollup['last_at']):rollup['last_at'],rollup['last_rowid']=at,rowid
            if rollup['first_rowid'] is None:rollup['first_rowid']=rowid
            if rollup['last_rowid'] is None:rollup['last_rowid']=rowid
            occurrence=classified.get('occurrence_key') or ['unproven_catalog_record',event['id']]
            occurrence_hash=hashlib.sha256(json.dumps(occurrence,sort_keys=True,default=str).encode()).digest()
            signature=hashlib.sha256(json.dumps([day,basis,number,at,metric.get('interval_start'),metric.get('interval_end'),variant if number is None else None],sort_keys=True,default=str).encode()).digest()
            inserted=conn.execute('INSERT OR IGNORE INTO source_occurrences(stream_key,occurrence_hash,signature,conflicted,first_rowid) VALUES(?,?,?,0,?)',(stream_key,occurrence_hash,signature,rowid)).rowcount
            original=conn.execute('SELECT signature,conflicted FROM source_occurrences WHERE stream_key=? AND occurrence_hash=?',(stream_key,occurrence_hash)).fetchone() if not inserted else None
            local_new=conn.execute('INSERT OR IGNORE INTO rollup_occurrences(rollup_id,stream_key,occurrence_hash) VALUES(?,?,?)',(rollup['id'],stream_key,occurrence_hash)).rowcount
            if local_new:rollup['sample_count']+=1
            conflict=bool(metric.get('_conflicting_fields') or original and (original['conflicted'] or original['signature']!=signature))
            if conflict:
                conn.execute('UPDATE source_occurrences SET conflicted=1,conflict_rowid=coalesce(conflict_rowid,?) WHERE stream_key=? AND occurrence_hash=?',(rowid,stream_key,occurrence_hash))
                affected=conn.execute('SELECT rollup_id,first_numeric FROM rollup_occurrences WHERE stream_key=? AND occurrence_hash=? AND conflicted=0',(stream_key,occurrence_hash)).fetchall()
                for item in affected:
                    conn.execute('UPDATE rollup_occurrences SET conflicted=1 WHERE rollup_id=? AND occurrence_hash=?',(item['rollup_id'],occurrence_hash))
                    if item['rollup_id'] in rollup_by_id:
                        old=rollup_by_id[item['rollup_id']];old['conflicting_occurrences']+=1;old['numeric_count']-=item['first_numeric']
                    else:conn.execute('UPDATE rollups SET conflicting_occurrences=conflicting_occurrences+1,numeric_count=numeric_count-? WHERE id=?',(item['first_numeric'],item['rollup_id']))
            # Alternative source versions describe one proven occurrence. Keep
            # all refs, but never weight a second version as another reading.
            if not local_new or conflict:continue
            if aggregation=='sum':
                try:
                    begin=datetime.fromisoformat(str(metric.get('interval_start')).replace('Z','+00:00'))
                    finish=datetime.fromisoformat(str(metric.get('interval_end')).replace('Z','+00:00'))
                    if begin.tzinfo is None or finish.tzinfo is None:raise ValueError('Unzoned interval')
                    begin,finish=begin.astimezone(timezone.utc),finish.astimezone(timezone.utc)
                    begin_key,finish_key=begin.isoformat(),finish.isoformat()
                    if (finish<=begin or basis!='utc' or begin.date().isoformat()!=day or finish.date().isoformat()!=day
                            or rollup['interval_max_end'] and begin_key<rollup['interval_max_end']):rollup['sum_conflicted']=1
                    rollup['interval_max_end']=max(rollup['interval_max_end'] or finish_key,finish_key)
                except (ValueError,TypeError,OverflowError):rollup['sum_conflicted']=1
            if number is not None:
                conn.execute('UPDATE rollup_occurrences SET first_numeric=1 WHERE rollup_id=? AND occurrence_hash=?',(rollup['id'],occurrence_hash))
                rollup['numeric_count']+=1
                if not rollup['numeric_overflow']&1:
                    rollup['total_value']+=number
                    if not math.isfinite(rollup['total_value']):rollup['numeric_overflow']|=1;rollup['total_value']=0
                if rollup['min_value'] is None or number<rollup['min_value']:rollup['min_value'],rollup['min_rowid']=number,rowid
                if rollup['max_value'] is None or number>rollup['max_value']:rollup['max_value'],rollup['max_rowid']=number,rowid
                if aggregation=='latest' and at and 'T' in at:
                    if rollup['latest_at'] is None or at>rollup['latest_at']:rollup['latest_at'],rollup['latest_value'],rollup['latest_conflicted']=at,number,0
                    elif at==rollup['latest_at'] and number!=rollup['latest_value']:rollup['latest_conflicted']=1
    for dims,values in counts.items():
        day,sid,source,category,display,basis=dims
        conn.execute('''INSERT INTO day_counts(id,day,source_id,source,category,display_class,date_basis,observed_records,raw_samples,substantive_records,technical_records) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET observed_records=observed_records+excluded.observed_records,raw_samples=raw_samples+excluded.raw_samples,substantive_records=substantive_records+excluded.substantive_records,technical_records=technical_records+excluded.technical_records''',(_hash(day,sid,category,display,basis),*dims,*values))
    fields=('source_record_count','sample_count','variant_count','conflicting_occurrences','numeric_count','total_value','min_value','max_value','min_rowid','max_rowid','first_at','last_at','first_rowid','last_rowid','latest_value','latest_at','latest_conflicted','sum_conflicted','interval_max_end','numeric_overflow')
    for value in rollup_changes.values():conn.execute('UPDATE rollups SET '+','.join(field+'=?' for field in fields)+' WHERE id=?',[value[field] for field in fields]+[value['id']])
    return {'new_display_groups':new_groups,'new_rollups':new_rollups}


def build_daily(catalog_path=None,sidecar_path=None,*,batch_size=2000,max_batches=None,watermark=None,rebuild=False,progress=None,max_batch_bytes=16*1024**2,capacity_paths=None,reserve_bytes=20*1024**3,max_seconds=None):
    """Bounded append job. Rebuild resets only this disposable projection."""
    catalog,sidecar=_paths(catalog_path,sidecar_path);version,classify=_classifier()
    if _active_mutations(catalog,sidecar):raise ImportPaused('daily_source_mutation_active')
    batch_size=min(max(int(batch_size),1),10000)
    paths=list(capacity_paths if capacity_paths is not None else getattr(settings,'capacity_paths',()))
    _capacity(sidecar,paths,reserve_bytes,8*1024**2+batch_size*1024)
    sidecar.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=(sidecar.parent/(sidecar.name+'.lock')).open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:lock.close();raise RuntimeError('A daily projection builder is already running')
    conn=sqlite3.connect(sidecar,timeout=10);conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL');conn.execute('PRAGMA synchronous=FULL')
        if rebuild:
            tables=('sample_refs','rollup_occurrences','source_occurrences','group_members','projection_fts','rollups','display_groups','day_counts','builds','state')
            conn.executescript('BEGIN IMMEDIATE;'+''.join('DROP TABLE IF EXISTS '+table+';' for table in tables)+SCHEMA)
        else:conn.executescript(SCHEMA)
        state=_state(conn);head=_catalog_head(catalog)
        if state and not state.get('epoch'):
            with conn:conn.execute('DELETE FROM state')
            state={}
        if state and daily_status(catalog,sidecar)['rebuild_required']:raise RuntimeError('Daily projection requires an explicit rebuild')
        if state and (state.get('classifier_version')!=version or state.get('schema_version')!=SCHEMA_VERSION or state.get('invalidated_reason')):raise RuntimeError('Daily projection requires an explicit rebuild')
        if state and state.get('catalog_file_identity')!=head['file_identity']:raise RuntimeError('Catalog replacement requires an explicit daily rebuild')
        if not state:
            with conn:_set(conn,epoch=uuid.uuid4().hex,schema_version=SCHEMA_VERSION,classifier_version=version,catalog_path=str(catalog),catalog_file_identity=head['file_identity'],checkpoint_rowid=0,watermark_rowid=0,processed_records=0,started_at=_now(),last_full_rebuild_at=_now(),coverage={'model':'append-only committed rowid projection','mutation_audit':'anchor checks plus explicit invalidation; not exhaustive'})
            state=_state(conn)
        checkpoint=state['checkpoint_rowid']
        if checkpoint<state['watermark_rowid']:
            target=state['watermark_rowid'];job=state['build_id']
        else:
            target=min(int(watermark),head['max_rowid']) if watermark is not None else head['max_rowid']
            target=max(target,checkpoint);job=uuid.uuid4().hex
            with conn:
                conn.execute('INSERT INTO builds VALUES(?,?,?,?,?,?)',(job,target,checkpoint,_now(),None,'building'))
                _set(conn,build_id=job,watermark_rowid=target,status='building',updated_at=_now())
        cache=OrderedDict();batches=0;started=time.monotonic()
        while checkpoint<target and (max_batches is None or batches<max_batches) and (max_seconds is None or time.monotonic()-started<max_seconds):
            if _active_mutations(catalog,sidecar) or _state(conn).get('invalidated_reason'):raise ImportPaused('daily_source_mutation_active')
            _capacity(sidecar,paths,reserve_bytes,8*1024**2+batch_size*1024)
            timings={}
            rows,provenance,read_bytes=_batch(catalog,checkpoint,target,batch_size,max_batch_bytes,cache,timings=timings)
            if not rows:
                with conn:_set(conn,checkpoint_rowid=target)
                checkpoint=target;break
            projection_started=time.monotonic()
            with conn:
                changes=_write_batch(conn,rows,provenance,classify)
                if _active_mutations(catalog,sidecar):raise ImportPaused('daily_source_mutation_active')
                checkpoint=rows[-1]['catalog_rowid'];state=_state(conn)
                _set(conn,checkpoint_rowid=checkpoint,processed_records=state['processed_records']+len(rows),anchor_rowid=checkpoint,anchor_event_id=rows[-1]['id'],updated_at=_now(),status='building')
            batches+=1
            if progress:progress({'event':'daily_batch','rows':len(rows),'catalog_bytes':read_bytes,'checkpoint_rowid':checkpoint,'watermark_rowid':target,'seconds':round(time.monotonic()-started,3),'projection_seconds':round(time.monotonic()-projection_started,6),**timings,**changes})
        with conn:
            if _active_mutations(catalog,sidecar) or _state(conn).get('invalidated_reason'):raise ImportPaused('daily_source_mutation_active')
            complete=checkpoint>=target
            _set(conn,status='caught_up' if complete else 'paused',last_error=None,last_pause_reason=None,updated_at=_now(),**({'completed_at':_now()} if complete else {}))
            conn.execute('UPDATE builds SET status=?,finished_at=? WHERE id=?',('complete' if complete else 'paused',_now() if complete else None,job))
        conn.execute('PRAGMA wal_checkpoint(PASSIVE)')
    except BaseException as error:
        contention=ImportPaused('daily_database_locked',sqlite_errorcode=getattr(error,'sqlite_errorcode',None),sqlite_errorname=getattr(error,'sqlite_errorname',None),stage=getattr(error,'daily_stage','sidecar_or_unspecified'),read_seconds=round(getattr(error,'daily_read_seconds',0),6),busy_timeout_seconds=getattr(error,'daily_busy_timeout',None)) if _sqlite_lock(error) else None
        try:
            with conn:_set(conn,status='paused' if contention or isinstance(error,(ImportPaused,KeyboardInterrupt)) else 'failed',last_error=type(error).__name__,last_pause_reason=contention.reason if contention else getattr(error,'reason',None),updated_at=_now())
        except sqlite3.Error:pass
        if contention:raise contention from error
        raise
    finally:
        conn.close();lock.close()
    return daily_status(catalog,sidecar)


def _filters(source='',category='',start='',end='',undated=False,day=None):
    clauses,args=[],[]
    if day is not None:clauses.append('day=?');args.append(day)
    elif undated:clauses.append("day='undated'")
    else:
        if start or end:clauses.append("day<>'undated'")
        for value,operator in ((start,'>='),(end,'<=')):
            if value:
                datetime.fromisoformat(value[:10]);clauses.append('day'+operator+'?');args.append(value[:10])
    if source:clauses.append('source_id=?');args.append(source)
    if category:clauses.append('category=?');args.append(category)
    return (' WHERE '+' AND '.join(clauses) if clauses else ''),args


def _qdays(conn,q,source,category):
    if not q.strip():return None
    query=' AND '.join('"'+word.replace('"','""')+'"' for word in re.findall(r'\S+',q)[:30])
    clauses=['projection_fts MATCH ?'];args=[query]
    if source:clauses.append('source_id=?');args.append(source)
    if category:clauses.append('category=?');args.append(category)
    return {row[0] for row in conn.execute('SELECT DISTINCT day FROM projection_fts WHERE '+' AND '.join(clauses),args)}


def _summary(conn,day,source='',category=''):
    where,args=_filters(source,category,day=day)
    row=conn.execute('SELECT coalesce(sum(observed_records),0) observed_records,coalesce(sum(raw_samples),0) raw_samples,coalesce(sum(substantive_records),0) substantive_records,coalesce(sum(technical_records),0) technical_records,count(DISTINCT source_id) source_count FROM day_counts'+where,args).fetchone()
    value={'day':day,'date':None if day=='undated' else day,**dict(row)}
    value['date_bases']=[{'basis':row[0],'count':row[1]} for row in conn.execute('SELECT date_basis,sum(observed_records) FROM day_counts'+where+' GROUP BY date_basis ORDER BY date_basis',args)]
    value['display_groups']=conn.execute('SELECT count(*) FROM display_groups'+where,args).fetchone()[0]
    value['measurement_groups']=conn.execute('SELECT count(*) FROM rollups'+where,args).fetchone()[0]
    return value


def daily_days(catalog_path=None,sidecar_path=None,*,q='',source='',category='',start='',end='',undated=False,limit=90,offset=0,sort='date',direction='desc'):
    if sort not in {'date','records','sources'} or direction not in {'asc','desc'}:raise ValueError('Unknown daily sort or direction')
    catalog,sidecar=_paths(catalog_path,sidecar_path);projection=daily_status(catalog,sidecar)
    result={'items':[],'total':0,'limit':min(max(int(limit),1),366),'offset':max(int(offset),0),'projection':projection,'q_scope':'matching projected days; day counts remain complete selected source/category counts','sort':sort,'direction':direction}
    if not sidecar.exists() or not projection.get('epoch') or projection.get('schema_version')!=SCHEMA_VERSION:return result
    with _readonly(sidecar) as conn:
        where,args=_filters(source,category,start,end,undated)
        if q.strip():
            query=' AND '.join('"'+word.replace('"','""')+'"' for word in re.findall(r'\S+',q)[:30])
            matching=['projection_fts MATCH ?'];matching_args=[query]
            if source:matching.append('source_id=?');matching_args.append(source)
            if category:matching.append('category=?');matching_args.append(category)
            where+=(' AND ' if where else ' WHERE ')+'day IN (SELECT day FROM projection_fts WHERE '+' AND '.join(matching)+')';args+=matching_args
        result['total']=conn.execute('SELECT count(DISTINCT day) FROM day_counts'+where,args).fetchone()[0]
        order="day='undated',day "+direction.upper() if sort=='date' else ('observed_records' if sort=='records' else 'source_count')+' '+direction.upper()+',day ASC'
        rows=conn.execute('''SELECT day,sum(observed_records) observed_records,sum(raw_samples) raw_samples,
            sum(substantive_records) substantive_records,sum(technical_records) technical_records,
            count(DISTINCT source_id) source_count FROM day_counts'''+where+' GROUP BY day ORDER BY '+order+' LIMIT ? OFFSET ?',args+[result['limit'],result['offset']]).fetchall()
        result['items']=[{'day':row['day'],'date':None if row['day']=='undated' else row['day'],**dict(row),'date_bases':[],'display_groups':0,'measurement_groups':0} for row in rows]
        by_day={item['day']:item for item in result['items']}
        if by_day:
            # Five bounded sidecar queries for any page size, rather than
            # fetching every day or issuing four extra queries per returned day.
            page_where,page_args=_filters(source,category)
            page_where+=(' AND ' if page_where else ' WHERE ')+'day IN ('+','.join('?' for _ in by_day)+')';page_args+=list(by_day)
            for basis in conn.execute('SELECT day,date_basis,sum(observed_records) FROM day_counts'+page_where+' GROUP BY day,date_basis ORDER BY date_basis',page_args):
                by_day[basis[0]]['date_bases'].append({'basis':basis[1],'count':basis[2]})
            for table,key in (('display_groups','display_groups'),('rollups','measurement_groups')):
                for counts in conn.execute('SELECT day,count(*) FROM '+table+page_where+' GROUP BY day',page_args):by_day[counts[0]][key]=counts[1]
    return result


def daily_categories(catalog_path=None,sidecar_path=None):
    """Derived type choices counted as observed projected source records.

    Counts include undated records, source variants, and duplicate copies. They
    are not counts of unique activities or deduplicated measurement identities.
    """
    catalog,sidecar=_paths(catalog_path,sidecar_path);projection=daily_status(catalog,sidecar)
    result={'items':[],'projection':projection}
    if not sidecar.exists() or not projection.get('epoch') or projection.get('schema_version')!=SCHEMA_VERSION:return result
    with _readonly(sidecar) as conn:
        result['items']=[dict(row) for row in conn.execute('SELECT category AS name,category,sum(observed_records) AS count FROM day_counts GROUP BY category ORDER BY count DESC,category')]
    return result


def _events(catalog,rowids,*,provenance=True):
    if not rowids:return {}
    result={};cache=OrderedDict()
    with _readonly(catalog) as conn:
        for start in range(0,len(rowids),800):
            ids=rowids[start:start+800]
            rows=[dict(row) for row in conn.execute('SELECT e.rowid AS catalog_rowid,e.*,s.name AS source FROM events e JOIN sources s ON s.id=e.source_id WHERE e.rowid IN ('+','.join('?' for _ in ids)+')',ids)]
            links=_provenance(conn,rows,cache) if provenance else {}
            attachments={}
            available={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('event_media_refs','media_file_index')")}
            if len(available)==2:
                from .media import SAFE_MIME_TYPES,CONVERTIBLE_IMAGE_TYPES
                event_ids=[row['id'] for row in rows]
                media_rows=conn.execute('''SELECT r.event_id,r.file_id,r.caption,r.match_basis,m.mime,m.kind
                    FROM event_media_refs r JOIN media_file_index m ON m.file_id=r.file_id
                    WHERE r.status='resolved' AND r.event_id IN ('''+','.join('?' for _ in event_ids)+') ORDER BY r.event_id,r.ordinal',event_ids) if event_ids else []
                for media in media_rows:
                    if media['mime'] in SAFE_MIME_TYPES:attachments.setdefault(media['event_id'],[]).append({'file_id':media['file_id'],'url':f"/api/files/{media['file_id']}/media",'mime':'image/jpeg' if media['mime'] in CONVERTIBLE_IMAGE_TYPES else media['mime'],'original_mime':media['mime'],'kind':media['kind'],'caption':media['caption'],'status':'resolved','match_basis':media['match_basis']})
            for row in rows:
                row['metadata']=_metadata(row['metadata'])
                row['provenance']=[{**item,'path':item.get('original_path'),'sha256':item.get('object_hash')} for item in links.get(row['id'],[])]
                row['media']=attachments.get(row['id'],[]);result[row['catalog_rowid']]=row
    return result


def _rollup_values(conn,rows,catalog,projection):
    identifiers=list({row[field] for row in rows for field in ('first_rowid','last_rowid','min_rowid','max_rowid') if row[field] is not None})
    conflict_evidence={}
    for row in rows:
        if row['conflicting_occurrences']:
            pairs=conn.execute('''SELECT o.first_rowid,o.conflict_rowid FROM rollup_occurrences r
                JOIN source_occurrences o ON o.stream_key=r.stream_key AND o.occurrence_hash=r.occurrence_hash
                WHERE r.rollup_id=? AND o.conflicted=1 LIMIT 4''',(row['id'],)).fetchall()
            extra=[value for pair in pairs for value in pair if value is not None]
            conflict_evidence[row['id']]=extra;identifiers.extend(extra)
    identifiers=list(dict.fromkeys(identifiers))
    evidence={}
    if identifiers:
        with _readonly(catalog) as source:
            for start in range(0,len(identifiers),800):
                batch=identifiers[start:start+800]
                evidence.update({row[0]:row[1] for row in source.execute('SELECT rowid,id FROM events WHERE rowid IN ('+','.join('?' for _ in batch)+')',batch)})
    result=[]
    for row in rows:
        numeric=row['numeric_count'];count=row['sample_count'];raw=row['source_record_count'];conflicted=row['conflicting_occurrences']>0
        result.append({'id':row['stable_id'],'day':row['day'],'source_id':row['source_id'],'source':row['source'],'category':row['category'],'date_basis':row['date_basis'],
            'metric':row['metric'],'unit':row['unit'],'device':row['device'],'stream_key':row['stream_key'],'aggregation':row['aggregation'],'sample_count':count,'occurrence_count':count,'variant_count':row['variant_count'],'conflicting_occurrences':row['conflicting_occurrences'],'source_record_count':raw,'duplicate_count':raw-row['variant_count'],
            'numeric_count':numeric,'min':row['min_value'] if not conflicted else None,'max':row['max_value'] if not conflicted else None,'sample_mean':row['total_value']/numeric if numeric and not row['numeric_overflow']&1 and not conflicted else None,
            'sum':row['total_value'] if numeric and row['aggregation']=='sum' and not row['sum_conflicted'] and not row['numeric_overflow'] and not conflicted else None,'latest':row['latest_value'] if row['aggregation']=='latest' and not row['latest_conflicted'] and not conflicted else None,
            'first_at':row['first_at'],'last_at':row['last_at'],'evidence_ids':list(dict.fromkeys(evidence[value] for value in [row['first_rowid'],row['last_rowid'],row['min_rowid'],row['max_rowid'],*conflict_evidence.get(row['id'],[])] if value in evidence)),
            'watermark':{'epoch':projection.get('epoch'),'checkpoint_rowid':projection['checkpoint_rowid'],'watermark_rowid':projection['watermark_rowid']},
            'notes':['Sample count describes occurrence identities; variants and proven duplicate copies are counted separately. Unproven repeated records remain separate occurrences.','Sample mean is unweighted; additive sums are sums of source records, not reconciled life totals.']+
                (['Conflicting source versions or same-stream fields for one occurrence exist; numeric summary statistics are withheld.'] if conflicted else [])+
                (['Additive intervals overlap, arrive out of order, cross a day, or lack an exact UTC day; total withheld.'] if row['sum_conflicted'] else [])+
                (['Conflicting values share the latest recorded instant; latest value withheld.'] if row['latest_conflicted'] else [])+
                (['Numeric accumulation exceeded safe finite range; mean and sum withheld.'] if row['numeric_overflow']&1 else [])+
                (['Conflicting same-stream fields were retained without choosing a numeric value.'] if row['numeric_overflow']&2 else [])})
    return result


def daily_rollups(catalog_path=None,sidecar_path=None,*,start='',end='',source='',category='',metric='',q='',limit=50,offset=0):
    catalog,sidecar=_paths(catalog_path,sidecar_path);projection=daily_status(catalog,sidecar)
    result={'items':[],'total':0,'limit':min(max(int(limit),1),200),'offset':max(int(offset),0),'projection':projection}
    if not sidecar.exists() or not projection.get('epoch') or projection.get('schema_version')!=SCHEMA_VERSION:return result
    with _readonly(sidecar) as conn:
        where,args=_filters(source,category,start,end)
        if metric:where+=(' AND ' if where else ' WHERE ')+'metric=?';args.append(metric)
        if q.strip():
            query=' AND '.join('"'+word.replace('"','""')+'"' for word in re.findall(r'\S+',q)[:30])
            where+=(' AND ' if where else ' WHERE ')+"stable_id IN (SELECT id FROM projection_fts WHERE projection_fts MATCH ? AND kind='rollup')";args.append(query)
        result['total']=conn.execute('SELECT count(*) FROM rollups'+where,args).fetchone()[0]
        rows=conn.execute('SELECT * FROM rollups'+where+" ORDER BY day='undated',day DESC,metric,unit,device,stable_id LIMIT ? OFFSET ?",args+[result['limit'],result['offset']]).fetchall()
        result['items']=_rollup_values(conn,rows,catalog,projection)
    return result


def daily_detail(day,catalog_path=None,sidecar_path=None,*,q='',source='',category='',limit=50,offset=0,include_technical=False):
    if day!='undated':datetime.fromisoformat(day)
    catalog,sidecar=_paths(catalog_path,sidecar_path);projection=daily_status(catalog,sidecar)
    result={'day':day,'summary':{'day':day,'date':None if day=='undated' else day,'observed_records':0,'raw_samples':0,'substantive_records':0,'technical_records':0,'display_groups':0,'measurement_groups':0,'source_count':0,'date_bases':[]},'items':[],'total':0,'limit':min(max(int(limit),1),200),'offset':max(int(offset),0),'rollups':[],'projection':projection}
    if not sidecar.exists() or not projection.get('epoch') or projection.get('schema_version')!=SCHEMA_VERSION:return result
    with _readonly(sidecar) as conn:
        result['summary']=_summary(conn,day,source,category)
        where,args=_filters(source,category,day=day)
        if not include_technical:where+=" AND display_class<>'technical'"
        if q.strip():
            query=' AND '.join('"'+word.replace('"','""')+'"' for word in re.findall(r'\S+',q)[:30])
            where+=" AND id IN (SELECT id FROM projection_fts WHERE projection_fts MATCH ? AND kind='group')";args.append(query)
        result['total']=conn.execute('SELECT count(*) FROM display_groups'+where,args).fetchone()[0]
        rows=conn.execute('SELECT * FROM display_groups'+where+' ORDER BY representative_rowid DESC,id LIMIT ? OFFSET ?',args+[result['limit'],result['offset']]).fetchall()
        records=_events(catalog,[row['representative_rowid'] for row in rows])
        for row in rows:
            if row['representative_rowid'] not in records:continue
            item=records[row['representative_rowid']];item.update(group_id=row['id'],display_class=row['display_class'],date_basis=row['date_basis'],duplicate_count=row['record_count']-1,source_record_count=row['record_count'],source_label=row['source']);result['items'].append(item)
        rollup_where,rollup_args=_filters(source,category,day=day)
        if q.strip():rollup_where+=" AND stable_id IN (SELECT id FROM projection_fts WHERE projection_fts MATCH ? AND kind='rollup')";rollup_args.append(query)
        rollups=conn.execute('SELECT * FROM rollups'+rollup_where+' ORDER BY metric,unit,device,stable_id LIMIT 200',rollup_args).fetchall()
        result['rollups']=_rollup_values(conn,rollups,catalog,projection)
        result['rollups_total']=conn.execute('SELECT count(*) FROM rollups'+rollup_where,rollup_args).fetchone()[0]
        result['rollups_truncated']=result['rollups_total']>len(rollups)
    return result


def rollup_records(rollup_id,catalog_path=None,sidecar_path=None,*,limit=50,offset=0):
    catalog,sidecar=_paths(catalog_path,sidecar_path);projection=daily_status(catalog,sidecar)
    result={'items':[],'total':0,'limit':min(max(int(limit),1),200),'offset':max(int(offset),0),'rollup':None,'projection':projection}
    if not sidecar.exists() or not projection.get('epoch') or projection.get('schema_version')!=SCHEMA_VERSION:return result
    with _readonly(sidecar) as conn:
        row=conn.execute('SELECT * FROM rollups WHERE stable_id=?',(rollup_id,)).fetchone()
        if row is None:return result
        result['rollup']=_rollup_values(conn,[row],catalog,projection)[0]
        result['total']=row['source_record_count']
        refs=conn.execute('SELECT catalog_rowid,is_duplicate FROM sample_refs WHERE rollup_id=? ORDER BY catalog_rowid LIMIT ? OFFSET ?',(row['id'],result['limit'],result['offset'])).fetchall()
        records=_events(catalog,[ref['catalog_rowid'] for ref in refs])
        for ref in refs:
            if ref['catalog_rowid'] in records:
                item=records[ref['catalog_rowid']];item['display_duplicate']=bool(ref['is_duplicate']);item['date_basis']=row['date_basis'];result['items'].append(item)
        result['unavailable_records']=len(refs)-len(result['items'])
    return result


__all__=['build_daily','daily_days','daily_categories','daily_detail','daily_rollups','rollup_records','daily_status','invalidate_daily','begin_daily_mutation','end_daily_mutation']
