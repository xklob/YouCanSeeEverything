"""Synthetic global sorting, paging, and incremental-count snapshots."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
from urllib.parse import parse_qs, urlsplit

from check_dashboard import fixture_api


def ordered(rows, field, direction, value, *, tie="id", tie_direction=False):
    def compare(a, b):
        x, y = value(a, field), value(b, field)
        missing_x, missing_y = x is None or x == "", y is None or y == ""
        if missing_x != missing_y:
            return 1 if missing_x else -1
        result = 0 if missing_x else (x > y)-(x < y)
        if result:
            return result if direction == "asc" else -result
        result = (a[tie] > b[tie])-(a[tie] < b[tie])
        return -result if tie_direction and direction == "desc" else result
    return sorted(rows, key=cmp_to_key(compare))


class SortingFixtures:
    def __init__(self):
        self.phase = "counting"
        self.requests = []
        epoch = datetime(2024, 1, 1, tzinfo=timezone.utc)
        stamp = lambda n: (epoch+timedelta(days=n)).isoformat().replace("+00:00", "Z")
        self.sources = [dict(id="qa-one", name="Zeta source", file_count=5, bytes=2048, status="importing"),
                        dict(id="qa-two", name="Alpha source", file_count=3, bytes=512, status="ready"),
                        dict(id="qa-empty", name="Empty source", file_count=0, bytes=0, status="queued")]
        self.records = [dict(id=f"qa-sort-{i:03}", title=f"Sort record {i:03}", body="Synthetic sorting evidence.",
            occurred_at=f"2024-01-05T{(i*7)%24:02}:{i%60:02}:00Z", source_id="qa-one" if i%2==0 else "qa-two",
            source="Zeta source" if i%2==0 else "Alpha source", category="note", time_precision="second", import_order=i)
            for i in range(84)]
        self.days = [dict(day=stamp(i)[:10], date=stamp(i)[:10], observed_records=1+(i*7)%17,
            raw_samples=0, substantive_records=1+(i*7)%17, technical_records=0, display_groups=1,
            measurement_groups=0, source_count=1+i%2, date_bases=[]) for i in range(64)]
        self.days.append(dict(day="undated", date=None, observed_records=11, raw_samples=0,
            substantive_records=11, technical_records=0, display_groups=1, measurement_groups=0, source_count=2, date_bases=[]))
        self.journeys = []
        for i in range(45):
            departure = stamp((i//2)*4)
            duration = 2+(i%7)*3
            arrival = (datetime.fromisoformat(departure.replace("Z", "+00:00"))+timedelta(hours=duration)).isoformat()
            stop = lambda end: dict(id=f"qa-stop-{i}-{end}", event_id="qa-alder" if end else "qa-cedar",
                latitude=37.54+end*.04, longitude=-77.44, occurred_at=arrival if end else departure,
                time_precision="second", title=f"{'End' if end else 'Start'} stop {i:03}", source="Zeta source")
            self.journeys.append(dict(id=f"{i+1:064x}", source_id="qa-one" if i%3!=2 else "qa-two",
                source="Zeta source" if i%3!=2 else "Alpha source", departure_at=departure, arrival_at=arrival,
                duration_hours=duration, distance_km=100+(i%9)*200, legs=1, represented_stops=2+i%5,
                max_gap_hours=duration, long_distance=i%9>=5, first_stop=stop(0), last_stop=stop(1), summary_complete=True))
        self.saved = [dict(key=f"{i+1:064x}", kind="trip_summary" if i%3==0 else "period_summary",
            title=f"Saved sort {(i*13)%45:03}", period=None if i%3==0 else f"2024-{1+i%12:02}",
            trip_id=f"{i+1:064x}" if i%3==0 else None, focus="", model="gpt-6.1-sol", source_ids=[],
            generated_at=stamp(i//2), citation_count=1, evidence_count=1, freshness="not_checked") for i in range(45)]
        self.imports = [dict(id=f"qa-import-{i:03}", source=f"Import source {(i*13)%105:03}", source_id="qa-one",
            status=("ready", "paused", "failed")[i%3], total_files=10+i%7, completed_files=5+i%5,
            total_bytes=1000+i*100, completed_bytes=500+i*20, events=(i*17)%101, started_at=stamp(i//2), error=None)
            for i in range(105)]
        self.jobs = [dict(id=f"qa-job-{i:03}", kind=("ask", "summarize", "summarize_trip")[i%3],
            status=("completed", "failed", "cancelled")[i%3], prompt=f"Question {(i*13)%85:03}",
            created_at=stamp(i//2), result=None, error=None) for i in range(85)]
        self.points = [dict(id=f"qa-point-{i:03}", event_id=f"qa-sort-{i%84:03}", title=f"Location {i:03}",
            latitude=37.54+i*.0001, longitude=-77.44, occurred_at=f"2024-01-05T{i//60:02}:{i%60:02}:00Z",
            source="Zeta source", source_id="qa-one", kind="explicit_coordinates", time_precision="second") for i in range(260)]

    def stats(self, source_id=None):
        measured = {None:37, "qa-one":17, "qa-two":20, "qa-empty":0}
        return dict(status="counting" if self.phase in {"counting", "subset"} else self.phase,
                    measured_at=None, counted_events=measured[source_id] if self.phase=="subset" else 0,
                    watermark_rowid=100, checkpoint_rowid=55, scanned_records=37)

    def page(self, rows, params, default_limit):
        offset, limit = int(params.get("offset", [0])[0]), int(params.get("limit", [default_limit])[0])
        return dict(items=rows[offset:offset+limit], total=len(rows), has_more=offset+limit<len(rows),
                    limit=limit, offset=offset)

    def __call__(self, path):
        parsed = urlsplit(path)
        params = parse_qs(parsed.query)
        self.requests.append((parsed.path, params))
        field, direction = params.get("sort", ["date"])[0], params.get("direction", ["desc"])[0]
        if parsed.path == "/api/overview":
            return dict(total_events=84 if self.phase=="current" else None, sources_count=3, files_count=8,
                stored_bytes=2560, date_range=dict(start=None,end=None), activity=[],
                categories=[dict(name="note",count=None)], undated_count=None, stats=self.stats())
        if parsed.path == "/api/sources":
            return dict(items=[{**s,"event_count":(0 if s["id"]=="qa-empty" else 42) if self.phase=="current" else None,
                "first_event":None,"last_event":None,"notes":[],"stats":self.stats(s["id"])} for s in self.sources],stats=self.stats())
        if parsed.path == "/api/events":
            rows=self.records
            for key,column in (("source","source_id"),("category","category")):
                if params.get(key):rows=[r for r in rows if r[column]==params[key][0]]
            if params.get("q"):rows=[r for r in rows if params["q"][0].casefold() in (r["title"]+r["body"]).casefold()]
            if params.get("undated")==["true"]:rows=[]
            for key in ("from","to"):
                if params.get(key):rows=[r for r in rows if (r['occurred_at'][:10]>=params[key][0] if key=='from' else r['occurred_at'][:10]<=params[key][0])]
            if field in {"asc","desc"}:direction,field=field,"date"
            rows=ordered(rows,field,direction,lambda r,f:r['import_order'] if f=='imported' else r['occurred_at'],tie_direction=True)
            return {**self.page(rows,params,40),'total':len(rows) if self.phase=='current' else None,
                    'total_status':'current' if self.phase=='current' else self.stats()['status'],'counted_at':None}
        if parsed.path == "/api/daily/categories":
            return dict(items=[dict(name='note',category='note',count=84)],projection={"status":"ready"})
        if parsed.path == "/api/days":
            rows=self.days
            if params.get('from'):rows=[r for r in rows if r['day']!='undated' and r['day']>=params['from'][0]]
            if params.get('to'):rows=[r for r in rows if r['day']!='undated' and r['day']<=params['to'][0]]
            rows=ordered(rows,field,direction,lambda r,f:None if f=='date' and r['day']=='undated' else r['day'] if f=='date' else r['observed_records' if f=='records' else 'source_count'],tie='day')
            return {**self.page(rows,params,31),'projection':{'status':'ready'}}
        if parsed.path.startswith('/api/days/'):
            day=parsed.path.rsplit('/',1)[-1]
            summary=next(r for r in self.days if r['day']==day)
            return dict(day=day,summary=summary,rollups=[],items=[],total=0,limit=20,offset=0,projection={'status':'ready'})
        if parsed.path == '/api/journeys':
            rows=self.journeys
            if params.get('source'):rows=[r for r in rows if r['source_id']==params['source'][0]]
            if params.get('from'):rows=[r for r in rows if r['departure_at'][:10]>=params['from'][0]]
            if params.get('to'):rows=[r for r in rows if r['arrival_at'][:10]<=params['to'][0]]
            if params.get('kind')==['long_distance']:rows=[r for r in rows if r['long_distance']]
            rows=ordered(rows,field,direction,lambda r,f:r[{'date':'departure_at','distance':'distance_km','duration':'duration_hours','stops':'represented_stops'}[f]],tie_direction=True)
            return {**self.page(rows,params,20),'coverage_complete':True,'sources':[dict(id=s['id'],name=s['name']) for s in self.sources[:2]]}
        if parsed.path.startswith('/api/journeys/'):
            row=next(r for r in self.journeys if r['id']==parsed.path.rsplit('/',1)[-1])
            leg=dict(id='qa-leg-'+row['id'],source=row['source'],**{'from':row['first_stop'],'to':row['last_stop']},distance_km=row['distance_km'],gap_hours=row['duration_hours'],inferred=True,geometry_type='straight_line')
            return dict(journey=row,items=[leg],total=1,limit=50,offset=0,truncated=False,complete=True,coverage_complete=True)
        if parsed.path == '/api/summaries/library':
            field=params.get('sort',['generated'])[0]
            rows=ordered(self.saved,field,direction,lambda r,f:r[{'generated':'generated_at','period':'period','title':'title'}[f]],tie='key')
            return self.page(rows,params,20)
        if parsed.path == '/api/imports':
            rows=ordered(self.imports,field,direction,lambda r,f:r[{'date':'started_at','name':'source','status':'status','records':'events','files':'total_files','bytes':'total_bytes'}[f]],tie_direction=True)
            return self.page(rows,params,100)
        if parsed.path == '/api/jobs':
            rows=ordered(self.jobs,field,direction,lambda r,f:r[{'date':'created_at','status':'status','kind':'kind','prompt':'prompt'}[f]],tie_direction=True)
            return self.page(rows,params,40)
        if parsed.path == '/api/map':
            rows=ordered(self.points,'date',direction,lambda r,f:r['occurred_at'],tie_direction=True)
            return self.page(rows,params,2000)
        if parsed.path == '/api/trips':
            rows=[dict(id=f'qa-loaded-leg-{i:03}',source='Zeta source',**{'from':{**self.points[i],'event_id':self.points[i]['event_id']},'to':{**self.points[i+1],'event_id':self.points[i+1]['event_id']}},distance_km=1,gap_hours=1,inferred=True,geometry_type='straight_line') for i in range(259)]
            return dict(items=rows,total=len(rows),truncated=False,gaps=[],gaps_total=0)
        if parsed.path == '/api/coverage':
            result=deepcopy(fixture_api(path))
            result['sources']=[dict(id=s['id'],name=s['name'],status=s['status'],queue_order=i,notes=[],
                files=dict(total=s['file_count'],bytes=s['bytes'],by_parse_status={},parsed=s['file_count'],partial=0,metadata_only=0,retained_only=0,empty=0,excluded=0,pending=0,needs_review=0),
                records=dict(total=(0 if s['id']=='qa-empty' else 42) if self.phase=='current' else None,dated=None,undated=None,first_observed=None,last_observed=None,measured_at=None,status=self.stats()['status'],counted_events=self.stats(s['id'])['counted_events']),
                extraction=dict(review_files=0,pending_files=0,history_absence_established=False),classification=dict(status='source_label_only',label=s['name'])) for i,s in enumerate(self.sources)]
            return result
        return fixture_api(path)


def run_sorting_acceptance(page, fixture, report, passed):
    """All sort controls share one isolated browser and synthetic snapshots."""
    import re
    from playwright.sync_api import expect

    modes=page.get_by_role('group',name='Archive view')
    nav=page.get_by_role('complementary',name='Main navigation')
    def endpoint(response,path):return urlsplit(response.url).path==path and response.request.method=='GET'
    def request(action,path,predicate=lambda p:True):
        with page.expect_response(lambda r:endpoint(r,path) and predicate(parse_qs(urlsplit(r.url).query))) as pending:action()
        response=pending.value
        assert response.status==200,'Synthetic sort request failed'
        return response.json(),parse_qs(urlsplit(response.url).query)
    def titles(locator,expected):
        expect(locator).to_have_text(expected)
    def server_sorts(label,fields,path,render,filters=None):
        for field in fields:
            current=page.get_by_role('combobox',name=label+' direction',exact=True).input_value()
            for direction in (('asc','desc') if current=='desc' else ('desc','asc')):
                def action():
                    page.get_by_role('combobox',name=label+' sort',exact=True).select_option(field)
                    page.get_by_role('combobox',name=label+' direction',exact=True).select_option(direction)
                payload,params=request(action,path,lambda p:p.get('sort',[('generated' if path.endswith('library') else 'date')])[0]==field and p.get('direction',['desc'])[0]==direction and p.get('offset',['0'])==['0'])
                for key,value in (filters or {}).items():assert params.get(key)==[value],f'{label} lost a filter'
                render(payload)
        passed(label.lower().replace(' ','_')+'_all_fields_directions_preserve_filters')
        return payload
    def pagination(path,next_button,previous_button,render):
        payload,_=request(lambda:next_button.click(),path,lambda p:int(p.get('offset',['0'])[0])>0)
        render(payload)
        payload,_=request(lambda:previous_button.click(),path,lambda p:p.get('offset',['0'])==['0'])
        render(payload)
        passed(path.rsplit('/',1)[-1]+'_next_previous_preserves_global_order')
    def client_sorts(label,rows,locator,value,title):
        for field in ('name','records','files','bytes','status'):
            for direction in ('asc','desc'):
                page.get_by_role('combobox',name=label+' sort',exact=True).select_option(field)
                page.get_by_role('combobox',name=label+' direction',exact=True).select_option(direction)
                known=[r for r in rows if value(r,field) is not None]
                missing=[r for r in rows if value(r,field) is None]
                expected=sorted(known,key=lambda r:value(r,field),reverse=direction=='desc')+missing
                titles(locator,[title(r) for r in expected])
        passed(label.lower().replace(' ','_')+'_all_local_sort_controls')

    expect(page.get_by_text('Counting…',exact=True).first).to_be_visible()
    expect(page.get_by_role('combobox',name='Map provider')).to_have_value('openstreetmap')
    for direction in ('asc','desc'):
        payload,_=request(lambda:page.get_by_role('combobox',name='Recorded locations direction').select_option(direction),'/api/map',lambda p:p.get('direction')==[direction])
        titles(page.locator('.recorded-location-list strong'),[r['title'] for r in payload['items'][:200]])
    before=sum(path in {'/api/map','/api/trips'} for path,_ in fixture.requests)
    page.get_by_role('button',name='Next loaded locations',exact=True).click()
    expect(page.locator('.recorded-location-list button')).to_have_count(60)
    page.get_by_role('button',name='Previous loaded locations',exact=True).click()
    page.get_by_text('Browse 259 inferred connections',exact=True).click()
    page.get_by_role('button',name='Next loaded connections',exact=True).click()
    page.get_by_role('button',name='Previous loaded connections',exact=True).click()
    assert before==sum(path in {'/api/map','/api/trips'} for path,_ in fixture.requests),'Loaded paging fetched source data'
    passed('map_direction_and_loaded_location_connection_pages_are_stable')
    same_day=page.get_by_role('region',name='Same-day context panel')
    same_render=lambda p:titles(same_day.locator('.same-day-event h4'),[r['title'] for r in p['items']])
    server_sorts('Same-day records',['date','imported'],'/api/events',same_render)
    pagination('/api/events',same_day.get_by_role('button',name='Next',exact=True),same_day.get_by_role('button',name='Previous',exact=True),same_render)

    request(lambda:modes.get_by_role('button',name='Records',exact=True).click(),'/api/events')
    filters=page.get_by_role('button',name=re.compile(r'^Filters',re.I))
    if filters.get_attribute('aria-expanded')!='true':filters.click()
    request(lambda:page.get_by_role('combobox',name='Filter by source').select_option('qa-one'),'/api/events',lambda p:p.get('source')==['qa-one'])
    request(lambda:page.get_by_role('searchbox').fill('Sort'),'/api/events',lambda p:p.get('q')==['Sort'])
    request(lambda:page.get_by_role('combobox',name='Filter by type').select_option('note'),'/api/events',lambda p:p.get('category')==['note'])
    request(lambda:page.get_by_label('From date',exact=True).fill('2024-01-05'),'/api/events',lambda p:p.get('from')==['2024-01-05'])
    request(lambda:page.get_by_label('To date',exact=True).fill('2024-01-05'),'/api/events',lambda p:p.get('to')==['2024-01-05'])
    # Article markup is stable while CSS presentation can change.
    record_render=lambda p:titles(page.get_by_role('region',name='Timeline results').locator('article h3'),[r['title'] for r in p['items']])
    server_sorts('Records',['date','imported'],'/api/events',record_render,dict(q='Sort',source='qa-one',category='note',**{'from':'2024-01-05','to':'2024-01-05'}))
    results=page.get_by_role('region',name='Timeline results')
    pagination('/api/events',results.get_by_role('button',name='Next',exact=True),results.get_by_role('button',name='Previous',exact=True),record_render)
    request(lambda:page.get_by_role('button',name='Clear',exact=True).click(),'/api/events',lambda p:not any(p.get(k) for k in ('q','source','category','from','to')))
    request(lambda:modes.get_by_role('button',name='Days',exact=True).click(),'/api/days')
    daily=page.get_by_role('region',name='Daily summaries and source evidence')
    def day_render(payload):
        expected=[datetime.fromisoformat(r['day']).strftime('%b %-d, %Y') for r in payload['items'] if r['day']!='undated']
        titles(daily.locator('.daily-date-list button>span:first-child'),expected)
    server_sorts('Days',['date','records','sources'],'/api/days',day_render)
    pagination('/api/days',daily.get_by_role('button',name='Next observed days'),daily.get_by_role('button',name='Previous observed days'),day_render)

    request(lambda:nav.get_by_role('button',name='Trips',exact=True).click(),'/api/journeys')
    trips=page.get_by_role('region',name='Trips',exact=True)
    request(lambda:trips.get_by_role('combobox',name='Trip source',exact=True).select_option('qa-one'),'/api/journeys',lambda p:p.get('source')==['qa-one'])
    def trip_render(payload):
        items=trips.locator('.trips-list>button')
        expect(items).to_have_count(len(payload['items']))
        for i,row in enumerate(payload['items']):expect(items.nth(i)).to_contain_text(row['last_stop']['title'])
    server_sorts('Trips',['date','distance','duration','stops'],'/api/journeys',trip_render,dict(source='qa-one',kind='all'))
    pagination('/api/journeys',trips.get_by_role('button',name='Next trips'),trips.get_by_role('button',name='Previous trips'),trip_render)

    nav.get_by_role('button',name='Sources',exact=True).click()
    expect(page.get_by_role('heading',name='Sources',level=1,exact=True)).to_be_visible()
    expect(page.locator('.source-card-count').filter(has_text='Records: 0')).to_have_count(0)
    source_values=lambda r,f:r['name'] if f=='name' else r['status'] if f=='status' else None if f=='records' else r['file_count'] if f=='files' else r['bytes']
    client_sorts('Sources',fixture.sources,page.locator('.source-card h3'),source_values,lambda r:r['name'])
    import_render=lambda p:titles(page.locator('.import-card .import-top strong'),[r['source'] for r in p['items']])
    server_sorts('Import activity',['date','name','records','files','bytes','status'],'/api/imports',import_render)
    pagination('/api/imports',page.get_by_role('button',name='Next imports'),page.get_by_role('button',name='Previous imports'),import_render)

    for phase,expected in [('subset','37+ (counting)'),('unavailable','Count unavailable'),('current','84')]:
        fixture.phase=phase
        request(lambda:page.get_by_role('button',name='Refresh archive',exact=True).click(),'/api/overview')
        # Source cards must use their own subset, not the global lower bound.
        if phase=='subset':
            expect(page.locator('.source-card-count').filter(has_text='17+ (counting)')).to_have_count(1)
            expect(page.locator('.source-card-count').filter(has_text='20+ (counting)')).to_have_count(1)
        elif phase=='unavailable':expect(page.locator('.source-card-count').first).to_contain_text(expected)
        else:expect(page.locator('.source-card-count').filter(has_text='Records: 0')).to_have_count(1)
        nav.get_by_role('button',name=re.compile(r'^Records\b')).click()
        expect(page.locator('.archive-summary')).to_contain_text(expected)
        nav.get_by_role('button',name='Sources',exact=True).click()
    passed('counted_subsets_unavailable_and_exact_zero_are_not_confused')

    request(lambda:nav.get_by_role('button',name=re.compile(r'^Ask\b')).click(),'/api/jobs')
    jobs_render=lambda p:titles(page.locator('.job-card h3'),[r['prompt'] for r in p['items']])
    server_sorts('Assistant activity',['date','status','kind','prompt'],'/api/jobs',jobs_render)
    pagination('/api/jobs',page.get_by_role('button',name='Next activity'),page.get_by_role('button',name='Previous activity'),jobs_render)
    request(lambda:page.get_by_text('Saved summaries',exact=True).click(),'/api/summaries/library')
    saved_render=lambda p:titles(page.locator('.saved-summary-list>button>strong'),[r['title'] for r in p['items']])
    server_sorts('Saved summaries',['generated','period','title'],'/api/summaries/library',saved_render)
    pagination('/api/summaries/library',page.get_by_role('button',name='Next saved summaries'),page.get_by_role('button',name='Previous saved summaries'),saved_render)
    nav.get_by_role('button',name='Imports',exact=True).click()
    expect(page.get_by_role('heading',name='Source coverage',exact=True)).to_be_visible()
    rows=fixture('/api/coverage')['sources']
    client_sorts('Source coverage',rows,page.locator('.coverage-source h3'),lambda r,f:r['name'] if f=='name' else r['status'] if f=='status' else r['records']['total'] if f=='records' else r['files']['total' if f=='files' else 'bytes'],lambda r:r['name'])
    passed('all_sorting_views_remain_read_only')
