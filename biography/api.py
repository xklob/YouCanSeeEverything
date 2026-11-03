"""Loopback-only application API. Data never enters the frontend build."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
import json
import mimetypes
import re
import secrets
import subprocess
import uuid
from fastapi import FastAPI,HTTPException,Request,Query
from fastapi.responses import FileResponse,JSONResponse
from pydantic import BaseModel,Field
from .config import settings
from .db import init_db,transaction,connect,now
from . import queries

TOKEN=secrets.token_urlsafe(32)
EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='biography-codex')
ALLOWED_HOSTS={'localhost','127.0.0.1','[::1]'}

@asynccontextmanager
async def lifespan(app):
    init_db()
    from .locations import ensure_schema
    ensure_schema()
    from .media import ensure_schema as ensure_media
    ensure_media()
    with transaction() as conn:
        conn.execute("UPDATE jobs SET status='interrupted',error='Application stopped before this task completed.',updated_at=? WHERE status IN ('running','queued')",(now(),))
    yield

app=FastAPI(title='Biography of You',lifespan=lifespan,docs_url=None,redoc_url=None)

@app.middleware('http')
async def local_boundary(request:Request,call_next):
    hostname=request.url.hostname or ''
    if hostname not in {'localhost','127.0.0.1','::1'}:
        return JSONResponse({'detail':'This application is available only on localhost.'},status_code=403)
    if request.method not in {'GET','HEAD','OPTIONS'}:
        origin=request.headers.get('origin')
        allowed={f'http://{host}:{port}' for host in ('localhost','127.0.0.1','[::1]') for port in (8000,5173)}
        if origin and origin not in allowed:
            return JSONResponse({'detail':'Origin is not allowed.'},status_code=403)
        if not secrets.compare_digest(request.headers.get('x-biography-token',''),TOKEN):
            return JSONResponse({'detail':'Refresh the page to renew the local session.'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    response.headers['Cache-Control']='no-store'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://tile.openstreetmap.org; font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response

@app.get('/api/session')
def session():return {'token':TOKEN}

@app.get('/api/status')
def status():
    return {'status':'ok','local_only':True,'ai_billing':'existing_codex_sign_in_only','data_dir':str(settings.data_dir)}

@app.get('/api/overview')
def overview():return queries.overview()

@app.get('/api/sources')
def sources():return queries.sources()

@app.get('/api/imports')
def imports():return queries.imports()

@app.get('/api/events')
def events(q:str='',source:str='',category:str='',start:str=Query('',alias='from'),end:str=Query('',alias='to'),limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0),sort:str='desc',undated:bool=False):
    try:return queries.list_events(q=q,source=source,category=category,start=start,end=end,limit=limit,offset=offset,sort=sort,undated=undated)
    except ValueError as e:raise HTTPException(400,'Invalid date filter.') from e

@app.get('/api/map')
def map_locations(q:str='',source:str='',category:str='',start:str=Query('',alias='from'),end:str=Query('',alias='to'),limit:int=Query(2000,ge=1,le=5000),offset:int=Query(0,ge=0),undated:bool=False,bounds:str=''):
    from .locations import map_points
    try:
        bbox=tuple(float(v) for v in bounds.split(',')) if bounds else None
        if bbox is not None and len(bbox)!=4:raise ValueError('Invalid map bounds')
        return map_points(q=q,source=source,category=category,start=start,end=end,limit=limit,offset=offset,undated=undated,bounds=bbox)
    except ValueError as error:raise HTTPException(400,'Invalid map filter.') from error

@app.get('/api/events/{event_id}')
def event_detail(event_id:str):
    event=queries.event_detail(event_id)
    if event is None:raise HTTPException(404,'Event not found.')
    return event

@app.get('/api/files/{file_id}/download')
def download_file(file_id:str):
    conn=connect()
    try:
        row=conn.execute('SELECT f.original_path,o.path FROM files f JOIN objects o ON o.sha256=f.object_hash WHERE f.id=?',(file_id,)).fetchone()
    finally:conn.close()
    if not row:raise HTTPException(404,'File not found.')
    path=Path(row['path']).resolve()
    try:path.relative_to((settings.data_dir/'objects').resolve())
    except ValueError:raise HTTPException(403,'Invalid object path.')
    if not path.is_file():raise HTTPException(404,'The retained object is unavailable.')
    filename=Path(row['original_path'].replace('\\','/')).name.replace('\n','').replace('\r','') or 'source'
    return FileResponse(path,media_type='application/octet-stream',filename=filename)

@app.get('/api/files/{file_id}/media')
def embedded_file(file_id:str):
    from .media import media_file_info
    info=media_file_info(file_id,objects_root=settings.data_dir/'objects')
    if not info:raise HTTPException(404,'Supported retained media not found.')
    return FileResponse(info['path'],media_type=info['mime'],filename=info['filename'],content_disposition_type='inline')

def job_value(row):
    value=dict(row)
    if value.get('result'):
        try:value['result']=json.loads(value['result'])
        except ValueError:pass
    value['can_undo']=bool(value['can_undo'])
    return value

@app.get('/api/jobs')
def jobs():
    conn=connect()
    try:return {'items':[job_value(r) for r in conn.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 40')]}
    finally:conn.close()

@app.get('/api/jobs/{job_id}')
def get_job(job_id:str):
    conn=connect()
    try:row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    finally:conn.close()
    if not row:raise HTTPException(404,'Task not found.')
    return job_value(row)

def retrieve_evidence(prompt):
    stop={'what','when','where','which','have','with','from','about','that','this','were','would','could','please','tell','show','summarize','summary','give','does','into','been','your','my','the','and','for','you','how','did','was','are'}
    tokens=[t for t in re.findall(r'[\w-]+',prompt.lower()) if len(t)>2 and t not in stop][:15]
    conn=connect()
    try:
        if tokens:
            match=' OR '.join('"'+t+'"' for t in tokens)
            rows=conn.execute('''SELECT e.*,s.name source FROM events_fts JOIN events e ON e.rowid=events_fts.rowid
              JOIN sources s ON s.id=e.source_id WHERE events_fts MATCH ? ORDER BY rank LIMIT 60''',(match,)).fetchall()
        else:rows=[]
        if not rows:
            rows=conn.execute('''SELECT * FROM (SELECT e.*,s.name source,ROW_NUMBER() OVER(PARTITION BY source_id ORDER BY occurred_at DESC) n
              FROM events e JOIN sources s ON s.id=e.source_id) WHERE n<=6 ORDER BY occurred_at DESC LIMIT 60''').fetchall()
        return [{k:r[k] for k in ('id','title','body','occurred_at','source','category')} for r in rows]
    finally:conn.close()

def assistant_worker(job_id,prompt,mode):
    try:
        with transaction() as conn:conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?",(now(),job_id))
        from . import agents,revisions
        if mode=='ask':
            result=agents.ask(prompt,retrieve_evidence(prompt),settings.state_dir/'agent-jobs')
        else:
            result=agents.change_dashboard(prompt,settings.project_root,settings.revisions_dir)
            checker=settings.project_root/'scripts'/'check_dashboard.py'
            check_dir=settings.state_dir/'revision-checks'/result['revision_id']
            checked=subprocess.run([str(settings.project_root/'.venv'/'bin'/'python'),str(checker),
                '--dist',result['dist_path'],'--fixture','--output',str(check_dir)],
                cwd=settings.project_root,capture_output=True,text=True,timeout=120)
            if checked.returncode:
                raise RuntimeError('The candidate dashboard failed its browser checks. The current dashboard was kept.')
            result=revisions.mark_browser_verified(settings.revisions_dir,result['revision_id'],check_dir/'result.json',project_root=settings.project_root)
            revisions.activate(settings.revisions_dir,result['revision_id'])
        with transaction() as conn:
            if mode=='change':conn.execute('UPDATE jobs SET can_undo=0')
            conn.execute("UPDATE jobs SET status='completed',result=?,updated_at=?,revision_id=?,can_undo=? WHERE id=?",(json.dumps(result),now(),result.get('revision_id'),int(mode=='change'),job_id))
    except Exception as error:
        with transaction() as conn:conn.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",(f'{type(error).__name__}: {str(error)[:2000]}',now(),job_id))

class AssistantRequest(BaseModel):
    prompt:str=Field(min_length=1,max_length=10000)
    mode:str='ask'

@app.post('/api/assistant',status_code=202)
def assistant(request:AssistantRequest):
    if request.mode not in {'ask','change'}:raise HTTPException(400,'Unknown task mode.')
    jid=uuid.uuid4().hex
    with transaction() as conn:
        pending=conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if pending>=3:raise HTTPException(409,'The local assistant queue is full.')
        conn.execute('INSERT INTO jobs(id,kind,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)',(jid,request.mode,'queued',request.prompt,now(),now()))
    EXECUTOR.submit(assistant_worker,jid,request.prompt,request.mode)
    return {'id':jid,'status':'queued'}

@app.post('/api/jobs/{job_id}/undo')
def undo(job_id:str):
    from . import revisions
    job=get_job(job_id)
    active=revisions.current(settings.revisions_dir)
    if not job['can_undo'] or not active or active.get('revision_id',active.get('id'))!=job.get('revision_id'):
        raise HTTPException(409,'Only the currently applied dashboard change can be undone.')
    result=revisions.undo(settings.revisions_dir)
    with transaction() as conn:
        conn.execute('UPDATE jobs SET can_undo=0,updated_at=? WHERE id=?',(now(),job_id))
    return result

@app.get('/{path:path}')
def frontend(path:str):
    if path.startswith('api/'):raise HTTPException(404,'Endpoint not found.')
    fallback=settings.project_root/'web'/'dist'
    try:
        from .revisions import active_dist
        dist=active_dist(settings.revisions_dir,fallback).resolve()
    except ImportError:dist=fallback.resolve()
    requested=(dist/path).resolve()
    try:requested.relative_to(dist)
    except ValueError:raise HTTPException(404)
    if requested.is_file():return FileResponse(requested)
    if Path(path).suffix:raise HTTPException(404)
    if (dist/'index.html').is_file():return FileResponse(dist/'index.html')
    return JSONResponse({'status':'frontend_not_built','message':'Build web/ to open the timeline.'},status_code=503)
