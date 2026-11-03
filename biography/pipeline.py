"""Serial dataset orchestration. Storage manifests remain the deletion authority."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import fcntl
import json
import os
import re
import time
import uuid
from .config import settings
from .db import init_db,transaction,identity,now,register_source,add_events

SKIP_INDEX_DIRS={'.git','.obsidian','node_modules','__pycache__'}

def dataset_inventory(export_root=None):
    root=Path(export_root or settings.export_root)
    preflight=settings.state_dir/'discovery'/'meta-preflight.json'
    if export_root is None and preflight.exists():
        evidence=json.loads(preflight.read_text())
        if evidence.get('status') in ('complete','preflight_complete_header_only') and evidence.get('root')==str(root):
            datasets=[{'name':name,'root':d['path'],'bytes':d['bytes'],'files':d['files']}
                for name,d in evidence['categories'].items() if Path(d['path']).is_dir()
                and not any(p.casefold() in settings.excluded_directory_names for p in Path(d['path']).parts)]
            return sorted(datasets,key=lambda d:(d['name'].casefold()=='meta',d['bytes'],d['name']))
    datasets=[]
    if not root.exists():return datasets
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or folder.name.casefold() in settings.excluded_directory_names:continue
        files=[]
        for base,dirs,names in os.walk(folder,followlinks=False):
            dirs[:]=sorted(d for d in dirs if d.casefold() not in settings.excluded_directory_names and not Path(base,d).is_symlink())
            for name in sorted(names):
                p=Path(base,name)
                if p.is_file() and not p.is_symlink():files.append((p,p.stat().st_size))
        if files:datasets.append({'name':folder.name,'root':str(folder),'bytes':sum(n for _,n in files),'files':len(files)})
    return sorted(datasets,key=lambda d:(d['name'].casefold()=='meta',d['bytes'],d['name']))

def register_queue(datasets,db_path=None):
    init_db(db_path)
    for order,d in enumerate(datasets):
        d['id']=register_source(d['name'],[d['root']],d['bytes'],order,db_path)
    return datasets

@contextmanager
def worker_lock(data_dir=None):
    root=Path(data_dir or settings.data_dir);root.mkdir(parents=True,exist_ok=True)
    with (root/'import.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise RuntimeError('An import worker is already running') from error
        yield

def index_stored(stored,source_id,import_id,source_name,db_path=None,*,index_content=True):
    from .parsers import parse_file
    if stored.source_path:
        try:
            relative=Path(stored.source_path).relative_to(settings.export_root/source_name)
            stored.original_path=relative.as_posix()+''.join('!'+item['path'] for item in stored.archive_chain)
        except ValueError:pass
    chain=json.dumps(stored.archive_chain,sort_keys=True)
    fid=identity(source_id,stored.original_path,chain,stored.sha256)
    with transaction(db_path) as conn:
        conn.execute('INSERT OR IGNORE INTO objects(sha256,bytes,path,verified_at) VALUES(?,?,?,?)',(stored.sha256,stored.size,str(stored.path),now()))
        existing=conn.execute('SELECT parse_status FROM files WHERE id=?',(fid,)).fetchone()
        if existing and existing['parse_status'] not in ('pending','error'):return 0
        conn.execute('''INSERT OR IGNORE INTO files(id,source_id,import_id,original_path,object_hash,bytes,locator,metadata,created_at)
          VALUES(?,?,?,?,?,?,?,?,?)''',(fid,source_id,import_id,stored.original_path,stored.sha256,stored.size,chain,json.dumps({'archive_chain':stored.archive_chain,'disposition':stored.disposition,'media_type':stored.media_type,'index_content':index_content}),now()))
    parts=set(re.split(r'[/\\!]',stored.original_path))
    native_document=Path(stored.original_path).suffix.lower() in {'.docx','.odt','.epub','.xlsx','.pptx'}
    supporting_history=(source_name in {'GitHub & local Git','Ethereum mainnet'} and Path(stored.original_path).name not in {'commits.jsonl','transactions.jsonl','token-transfers.jsonl','internal-transactions.jsonl'})
    if not index_content or (stored.disposition!='retained_leaf' and not native_document) or parts & SKIP_INDEX_DIRS or supporting_history:
        with transaction(db_path) as conn:conn.execute("UPDATE files SET parse_status='retained',notes=? WHERE id=?",(json.dumps(['Retained source bytes; not indexed as personal activity.']),fid))
        return 0
    try:
        result=parse_file(Path(stored.path),stored.original_path,source_name,excluded_directory_names=settings.excluded_directory_names)
        if result.status=='needs_fallback':
            from .adaptive import adapt_file
            result=adapt_file(Path(stored.path),stored.original_path,source_name,result,
                settings.state_dir/'agent-jobs')
        with transaction(db_path) as conn:
            count=add_events(conn,source_id,fid,stored.sha256,result)
            conn.execute('UPDATE files SET parser=?,parse_status=?,notes=? WHERE id=?',(result.parser,result.status,json.dumps(result.notes),fid))
        return count
    except Exception as error:
        with transaction(db_path) as conn:
            conn.execute("UPDATE files SET parse_status='error',notes=? WHERE id=?",(json.dumps([f'{type(error).__name__}: {str(error)[:500]}']),fid))
        return 0

def run_dataset(dataset, *,move=False,data_dir=None,db_path=None,progress=None,cleanup_root=None):
    from .importer import ingest_path,StoredFile
    from .storage import ImportPaused
    data_dir=Path(data_dir or settings.data_dir)
    sid=dataset['id'];iid=uuid.uuid4().hex;root=Path(dataset['root'])
    if any(p.casefold() in settings.excluded_directory_names for p in root.parts):
        raise ValueError('Dataset is inside an excluded directory')
    paths=[]
    for base,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d.casefold() not in settings.excluded_directory_names and not Path(base,d).is_symlink())
        for name in names:
            p=Path(base,name)
            if p.is_file() and not p.is_symlink():paths.append(p)
    paths.sort(key=lambda p:(p.stat().st_size,str(p)))
    preflight=settings.state_dir/'discovery'/'meta-preflight.json'
    outer_exports=set()
    if preflight.exists():
        outer_exports={a['path'] for a in json.loads(preflight.read_text()).get('archives',[]) if a.get('category')=='Meta'}
    with transaction(db_path) as conn:
        conn.execute('INSERT INTO imports(id,source_id,status,total_files,total_bytes,started_at) VALUES(?,?,?,?,?,?)',(iid,sid,'running',len(paths),sum(p.stat().st_size for p in paths),now()))
        conn.execute("UPDATE sources SET status='importing',updated_at=? WHERE id=?",(now(),sid))
    completed=byte_count=events=failures=0
    def index_item(stored):
        allowed=dataset.get('index_files')
        return index_stored(stored,sid,iid,dataset['name'],db_path,index_content=allowed is None or stored.original_path in allowed)
    try:
        for source in paths:
            source_size=source.stat().st_size
            heartbeat=0.0
            phase='starting'
            def storage_progress(update):
                nonlocal events,heartbeat,phase
                phase=update.get('phase',update.get('event',phase))
                if update.get('event')=='object_verified' and update.get('file'):
                    values=dict(update['file']);values['path']=Path(values['path'])
                    events+=index_item(StoredFile(**values))
                if time.monotonic()-heartbeat>2:
                    heartbeat=time.monotonic()
                    with transaction(db_path) as conn:
                        conn.execute('UPDATE imports SET events=?,checkpoint=? WHERE id=?',(events,json.dumps({'current_source':str(source),'phase':phase}),iid))
            report=ingest_path(source,data_dir,delete_source=move,allowed_root=Path(cleanup_root or settings.export_root),
                reserve_bytes=settings.reserve_bytes,windows_reserve_bytes=50*1024**3,progress=storage_progress,
                excluded_directory_names=settings.excluded_directory_names,
                logical_path=str(source.relative_to(root)),discard_container=str(source) in outer_exports)
            for stored in report.files:events+=index_item(stored)
            if report.status!='complete':failures+=max(1,len(report.exceptions))
            completed+=1;byte_count+=source_size
            with transaction(db_path) as conn:
                conn.execute('UPDATE imports SET completed_files=?,completed_bytes=?,events=?,checkpoint=? WHERE id=?',(completed,byte_count,events,json.dumps({'last_source':str(source),'last_manifest':str(report.manifest_path),'storage_status':report.status}),iid))
            if progress:progress({'source':dataset['name'],'completed_files':completed,'total_files':len(paths),'events':events,'status':report.status})
        with transaction(db_path) as conn:
            parse_errors=conn.execute("SELECT count(*) FROM files WHERE source_id=? AND parse_status IN ('error','needs_fallback','partial')",(sid,)).fetchone()[0]
            remaining=sum(p.exists() for p in paths) if move else 0
            state='partial' if failures or parse_errors or remaining else 'ready'
            notes=[]
            if failures:notes.append(f'{failures} storage items need review; their source copies were retained.')
            if parse_errors:notes.append(f'{parse_errors} retained files need further content extraction.')
            if remaining:notes.append(f'{remaining} Windows source files remain, including exclusions or unresolved archives.')
            conn.execute('UPDATE sources SET status=?,updated_at=?,notes=? WHERE id=?',(state,now(),json.dumps(notes),sid))
            conn.execute('UPDATE imports SET status=?,finished_at=? WHERE id=?',(state,now(),iid))
        # Empty export directories are removed only after their individual files were verified.
        if move:
            for base,dirs,names in os.walk(root,topdown=False,followlinks=False):
                try:Path(base).rmdir()
                except OSError:pass
        from .enrichment import reapply
        from .locations import reconcile_locations
        from .media import reconcile_media
        reapply(db_path,source_id=sid)
        reconcile_locations(db_path,source_id=sid)
        reconcile_media(db_path,source_id=sid)
        return {'id':iid,'source':dataset['name'],'status':state,'files':completed,'events_added':events,'storage_exceptions':failures,'parse_errors':parse_errors}
    except Exception as error:
        state='paused' if isinstance(error,ImportPaused) else 'failed'
        with transaction(db_path) as conn:
            conn.execute('UPDATE sources SET status=?,updated_at=? WHERE id=?',(state,now(),sid))
            conn.execute('UPDATE imports SET status=?,error=?,finished_at=? WHERE id=?',(state,f'{type(error).__name__}: {error}',now(),iid))
        raise

def reindex(source_name=None,db_path=None):
    """Rebuild derived records from the canonical bytes, never from deleted exports."""
    from .importer import StoredFile
    from .storage import ObjectStore
    with transaction(db_path) as conn:
        chosen=conn.execute('SELECT * FROM sources'+(' WHERE name=?' if source_name else ''),([source_name] if source_name else [])).fetchall()
    results=[]
    for source in chosen:
        sid=source['id']
        with transaction(db_path) as conn:
            rows=conn.execute('SELECT f.*,o.path FROM files f JOIN objects o ON o.sha256=f.object_hash WHERE f.source_id=?',(sid,)).fetchall()
            conn.execute('DELETE FROM evidence WHERE event_id IN (SELECT id FROM events WHERE source_id=?)',(sid,))
            conn.execute('DELETE FROM events WHERE source_id=?',(sid,))
            conn.execute("UPDATE files SET parse_status='pending' WHERE source_id=?",(sid,))
        count=0
        for row in rows:
            metadata=json.loads(row['metadata'])
            stored=StoredFile(row['object_hash'],row['bytes'],Path(row['path']),row['original_path'],'',metadata.get('archive_chain',[]),metadata.get('media_type'),metadata.get('disposition','retained_leaf'))
            # Reindexing is derived work; retain any failed record's original file.
            count+=index_stored(stored,sid,row['import_id'],source['name'],db_path,index_content=metadata.get('index_content',True))
        results.append({'source':source['name'],'files':len(rows),'events':count})
        from .enrichment import reapply
        from .locations import reconcile_locations
        from .media import reconcile_media
        reapply(db_path,source_id=sid)
        reconcile_locations(db_path,source_id=sid)
        reconcile_media(db_path,source_id=sid)
    return results

def reindex_files(file_ids,db_path=None):
    """Refresh selected retained files, preserving records with other evidence."""
    from .importer import StoredFile
    from .enrichment import reapply
    from .locations import reconcile_locations
    from .media import reconcile_media
    results=[];sources=set()
    for file_id in dict.fromkeys(file_ids):
        with transaction(db_path) as conn:
            row=conn.execute('''SELECT f.*,o.path,s.name source FROM files f
                JOIN objects o ON o.sha256=f.object_hash JOIN sources s ON s.id=f.source_id WHERE f.id=?''',(file_id,)).fetchone()
            if row is None:raise ValueError('Retained file not found')
            event_ids=[r[0] for r in conn.execute('SELECT event_id FROM evidence WHERE file_id=?',(file_id,))]
            conn.execute('DELETE FROM evidence WHERE file_id=?',(file_id,))
            for start in range(0,len(event_ids),800):
                batch=event_ids[start:start+800]
                conn.execute('DELETE FROM events WHERE id IN ('+','.join('?' for _ in batch)+') AND NOT EXISTS (SELECT 1 FROM evidence WHERE event_id=events.id)',batch)
            conn.execute("UPDATE files SET parse_status='pending' WHERE id=?",(file_id,))
        meta=json.loads(row['metadata'])
        stored=StoredFile(row['object_hash'],row['bytes'],Path(row['path']),row['original_path'],'',meta.get('archive_chain',[]),meta.get('media_type'),meta.get('disposition','retained_leaf'))
        count=index_stored(stored,row['source_id'],row['import_id'],row['source'],db_path,index_content=meta.get('index_content',True))
        results.append({'file_id':file_id,'events_added':count});sources.add(row['source_id'])
    for sid in sources:
        reapply(db_path,source_id=sid)
        reconcile_locations(db_path,source_id=sid)
        reconcile_media(db_path,source_id=sid)
    return results

def replay_manifests(data_dir=None,db_path=None):
    """Recover catalog entries after a crash between retained bytes and index commit."""
    from .importer import StoredFile,read_manifest
    from .storage import ObjectStore
    root=Path(data_dir or settings.data_dir);store=ObjectStore(root)
    with transaction(db_path) as conn:known=[dict(r) for r in conn.execute('SELECT * FROM sources')]
    result={'manifests':0,'objects':0,'events_added':0,'unmatched':0}
    for manifest in sorted((root/'manifests').glob('*.jsonl')):
        rows=read_manifest(manifest)
        if not rows:continue
        header=rows[0];source_path=Path(header.get('source',''))
        source=next((s for s in known if any(source_path.is_relative_to(Path(p)) for p in json.loads(s['roots']))),None)
        if source is None:result['unmatched']+=1;continue
        result['manifests']+=1
        for row in rows:
            if row.get('kind')!='object':continue
            payload={k:v for k,v in row.items() if k not in ('kind','at')}
            payload['path']=store.object_path(payload['sha256']);stored=StoredFile(**payload)
            store.verify_object(stored.sha256,stored.size)
            result['events_added']+=index_stored(stored,source['id'],None,source['name'],db_path)
            result['objects']+=1
    return result

def enrich_pending(limit=10,db_path=None):
    from .importer import StoredFile
    with transaction(db_path) as conn:
        rows=conn.execute("""SELECT f.*,o.path,s.name source FROM files f JOIN objects o ON o.sha256=f.object_hash
            JOIN sources s ON s.id=f.source_id WHERE parse_status IN ('needs_fallback','error') ORDER BY f.bytes LIMIT ?""",(limit,)).fetchall()
    results=[]
    for row in rows:
        metadata=json.loads(row['metadata'])
        stored=StoredFile(row['object_hash'],row['bytes'],Path(row['path']),row['original_path'],'',metadata.get('archive_chain',[]),metadata.get('media_type'),metadata.get('disposition','retained_leaf'))
        with transaction(db_path) as conn:conn.execute("UPDATE files SET parse_status='pending' WHERE id=?",(row['id'],))
        count=index_stored(stored,row['source_id'],row['import_id'],row['source'],db_path)
        with transaction(db_path) as conn:state=conn.execute('SELECT parse_status FROM files WHERE id=?',(row['id'],)).fetchone()[0]
        results.append({'file_id':row['id'],'source':row['source'],'status':state,'events_added':count})
    return results
