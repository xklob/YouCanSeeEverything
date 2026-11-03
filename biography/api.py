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

def content_security_policy():
    directives={
        'default-src':"'self'",'script-src':"'self'",'style-src':"'self' 'unsafe-inline'",
        'img-src':"'self' data: blob: https://tile.openstreetmap.org",'font-src':"'self' data:",
        'connect-src':"'self'",'media-src':"'self' blob:",'object-src':"'none'",
        'frame-ancestors':"'none'",'base-uri':"'self'",'form-action':"'self'",
    }
    if settings.google_maps_api_key:
        # Official Maps JS domains. No inline scripts or broad script-host wildcard.
        directives['script-src']+=" https://maps.googleapis.com https://maps.gstatic.com 'unsafe-eval'"
        directives['style-src']+=' https://fonts.googleapis.com'
        directives['img-src']+=' https://*.googleapis.com https://*.gstatic.com https://*.google.com https://*.googleusercontent.com'
        directives['connect-src']+=' https://*.googleapis.com https://*.gstatic.com https://*.google.com data: blob:'
        directives['font-src']+=' https://fonts.gstatic.com'
        directives['worker-src']="'self' blob:"
        directives['frame-src']='https://www.google.com https://maps.google.com'
    return '; '.join(f'{name} {value}' for name,value in directives.items())

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
    response.headers['Content-Security-Policy']=content_security_policy()
    return response

@app.get('/api/session')
def session():return {'token':TOKEN}

@app.get('/api/status')
def status():
    return {'status':'ok','local_only':True,'ai_billing':'existing_codex_sign_in_only','data_dir':str(settings.data_dir)}

@app.get('/api/map-config')
def map_configuration():
    # A website-restricted Maps key is necessarily delivered to its browser.
    # A configured key selects Google. Returning config itself performs no map requests.
    result={'provider':'google' if settings.google_maps_api_key else 'openstreetmap','google_maps_configured':bool(settings.google_maps_api_key)}
    if settings.google_maps_api_key:result['google_maps_api_key']=settings.google_maps_api_key
    return result

@app.get('/api/overview')
def overview():return queries.overview(settings.database)

@app.get('/api/sources')
def sources():return queries.sources(settings.database)

@app.get('/api/imports')
def imports():return queries.imports()

def backup_status():
    path=settings.state_dir/'backup-status.json'
    try:
        if path.stat().st_size>100_000:return {'status':'unavailable'}
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else {'status':'unavailable'}
    except FileNotFoundError:return {'status':'not_configured'}
    except (OSError,ValueError):return {'status':'unavailable'}

@app.get('/api/coverage')
def coverage(include_months:bool=False):
    from .coverage import coverage_overview
    from .daily import daily_status
    return coverage_overview(settings.database,include_months=include_months,
        index_status=daily_status(settings.database,settings.daily_database),backup_status=backup_status())

@app.get('/api/daily/status')
def daily_index_status():
    from .daily import daily_status
    return daily_status(settings.database,settings.daily_database)

@app.get('/api/daily/categories')
def daily_category_options():
    from .daily import daily_categories
    return daily_categories(settings.database,settings.daily_database)

@app.get('/api/days')
def days(q:str='',source:str='',category:str='',start:str=Query('',alias='from'),end:str=Query('',alias='to'),
         limit:int=Query(90,ge=1,le=366),offset:int=Query(0,ge=0),undated:bool=False):
    from .daily import daily_days
    try:return daily_days(settings.database,settings.daily_database,q=q,source=source,category=category,
                          start=start,end=end,limit=limit,offset=offset,undated=undated)
    except ValueError as error:raise HTTPException(400,'Invalid day filter.') from error

@app.get('/api/days/{day}')
def day_detail(day:str,q:str='',source:str='',category:str='',limit:int=Query(50,ge=1,le=200),
               offset:int=Query(0,ge=0),include_technical:bool=False):
    from .daily import daily_detail
    if day!='undated' and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):raise HTTPException(400,'Choose a day in YYYY-MM-DD format.')
    try:return daily_detail(day,settings.database,settings.daily_database,q=q,source=source,category=category,
                            limit=limit,offset=offset,include_technical=include_technical)
    except ValueError as error:raise HTTPException(400,'Invalid day filter.') from error

@app.get('/api/rollups/{rollup_id}')
def rollup_detail(rollup_id:str,limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
    from .daily import rollup_records
    result=rollup_records(rollup_id,settings.database,settings.daily_database,limit=limit,offset=offset)
    if result['rollup'] is None:raise HTTPException(404,'Measurement group not found.')
    return result

@app.get('/api/events')
def events(q:str='',source:str='',category:str='',start:str=Query('',alias='from'),end:str=Query('',alias='to'),limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0),sort:str='desc',undated:bool=False):
    try:return queries.list_events(settings.database,q=q,source=source,category=category,start=start,end=end,limit=limit,offset=offset,sort=sort,undated=undated)
    except queries.RecordQueryTimeout as error:
        raise HTTPException(503,'This record search took too long. Narrow the date range or source, or retry shortly.',headers={'Retry-After':'2'}) from error
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
    try:event=queries.event_detail(event_id)
    except queries.RecordQueryTimeout as error:
        raise HTTPException(503,'This record took too long to load. Retry shortly.',headers={'Retry-After':'2'}) from error
    if event is None:raise HTTPException(404,'Event not found.')
    return event

@app.get('/api/trips')
def inferred_trips(q:str='',source:str='',category:str='',start:str=Query('',alias='from'),end:str=Query('',alias='to'),limit:int=Query(2000,ge=1,le=5000),offset:int=Query(0,ge=0),undated:bool=False):
    from .trips import trip_connections
    try:
        return trip_connections(settings.database,q=q,source=source,category=category,start=start,end=end,limit=limit,offset=offset,undated=undated)
    except ValueError as error:raise HTTPException(400,'Invalid trip filter.') from error

@app.get('/api/journeys')
def journeys(source:str='',start:str=Query('',alias='from'),end:str=Query('',alias='to'),
             kind:str='all',limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0)):
    from .journeys import list_journeys
    try:return list_journeys(settings.database,source=source,start=start,end=end,kind=kind,limit=limit,offset=offset)
    except ValueError as error:raise HTTPException(400,'Invalid trip filter.') from error

@app.get('/api/journeys/{journey_id}')
def journey(journey_id:str,limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    from .journeys import journey_detail
    result=journey_detail(journey_id,settings.database,limit=limit,offset=offset)
    if result is None:raise HTTPException(404,'Trip not found.')
    return result

@app.get('/api/summaries')
def saved_summary(period:str,source:str='',model:str='gpt-6.1-sol',focus:str=Query('',max_length=4000)):
    from .synthesis import summary_status
    from .agents import AgentError
    try:return summary_status(period,settings.summaries_dir,settings.database,
                              source_ids=[source] if source else [],focus=focus,model=model)
    except (ValueError,AgentError) as error:raise HTTPException(400,str(error)) from error

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

def assistant_worker(job_id,prompt,mode,period=None,source_ids=None,model='gpt-6.1-sol'):
    try:
        with transaction() as conn:conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?",(now(),job_id))
        from . import agents,revisions,retrieval,synthesis
        if mode=='ask':
            bundle=retrieval.retrieve(prompt,settings.database,period=period,source_ids=source_ids)
            context={key:value for key,value in bundle.items() if key!='evidence'}
            result=agents.ask(prompt,bundle['evidence'],settings.state_dir/'agent-jobs',model=model,retrieval_context=context)
            result.update(context)
        elif mode=='summarize':
            result=synthesis.summarize(period,settings.summaries_dir,settings.database,
                                       source_ids=source_ids,focus=prompt,model=model)
        else:
            result=agents.change_dashboard(prompt,settings.project_root,settings.revisions_dir,model=model)
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
    prompt:str=Field(default='',max_length=8000)
    mode:str='ask'
    period:str|None=None
    source_ids:list[str]=Field(default_factory=list,max_length=100)
    model:str='gpt-6.1-sol'

@app.post('/api/assistant',status_code=202)
def assistant(request:AssistantRequest):
    from . import agents,retrieval,synthesis
    if request.mode not in {'ask','change','summarize'}:raise HTTPException(400,'Unknown task mode.')
    try:
        agents._model(request.model)
        if request.mode=='summarize':
            synthesis._period(request.period)
            if len(request.prompt)>4000:raise ValueError('A summary focus must be at most 4,000 characters.')
        elif not request.prompt.strip():raise ValueError('Enter a question or change.')
        if request.mode!='change':retrieval.selection(request.prompt,period=request.period,source_ids=request.source_ids)
    except (ValueError,agents.AgentError) as error:raise HTTPException(400,str(error)) from error
    jid=uuid.uuid4().hex
    with transaction() as conn:
        pending=conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if pending>=3:raise HTTPException(409,'The local assistant queue is full.')
        conn.execute('INSERT INTO jobs(id,kind,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)',(jid,request.mode,'queued',request.prompt,now(),now()))
    EXECUTOR.submit(assistant_worker,jid,request.prompt,request.mode,request.period,request.source_ids,request.model)
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
