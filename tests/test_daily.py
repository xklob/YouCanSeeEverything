import json
from pathlib import Path
import sqlite3

import pytest

from biography import daily
from biography.db import init_db,transaction
from biography.storage import ImportPaused


@pytest.fixture
def catalog(tmp_path):
    catalog=tmp_path/'catalog.sqlite3';sidecar=tmp_path/'daily/projection.sqlite3'
    init_db(catalog)
    with transaction(catalog) as conn:
        for source in ('source-a','source-b'):
            conn.execute('INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)',(source,source,'now','now'))
    return catalog,sidecar


def event(paths,identity,*,when='2024-06-01T12:00:00Z',precision='second',source='source-a',category='record',metadata=None,native=None,body='fixture',title='Synthetic record'):
    with transaction(paths[0]) as conn:
        conn.execute('''INSERT INTO events(id,source_id,native_id,title,body,occurred_at,time_precision,category,metadata,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(identity,source,native,title,body,when,precision,category,json.dumps(metadata or {}),'now'))


def metric(name='heart_rate',value=70,unit='bpm',device='sensor-a',aggregation='none',**extra):
    return {'device_id':device,'metric':{'name':name,'value':value,'unit':unit,'aggregation':aggregation},**extra}


def build(paths,**kwargs):
    return daily.build_daily(*paths,reserve_bytes=0,capacity_paths=[],**kwargs)


def test_readonly_getters_do_not_create_sidecar(catalog):
    event(catalog,'one',category='post')
    with sqlite3.connect(catalog[0]) as conn:before=list(conn.iterdump())
    assert daily.daily_status(*catalog)['status']=='not_built'
    assert daily.daily_days(*catalog)['items']==[]
    assert daily.daily_categories(*catalog)['items']==[]
    assert daily.daily_detail('2024-06-01',*catalog)['items']==[]
    assert not catalog[1].exists()
    with sqlite3.connect(catalog[0]) as conn:assert list(conn.iterdump())==before


def test_fixed_watermark_checkpoint_resume_and_append_are_exactly_once(catalog):
    for index in range(5):event(catalog,str(index),category='post')
    first=build(catalog,batch_size=2,max_batches=1)
    assert first['checkpoint_rowid']==2 and first['watermark_rowid']==5
    assert first['processed_records']==2 and first['status']=='paused'
    event(catalog,'new',category='post')
    second=build(catalog,batch_size=2)
    assert second['checkpoint_rowid']==5 and second['processed_records']==5
    assert second['append_backlog_rowid_span']==1
    third=build(catalog)
    assert third['checkpoint_rowid']==6 and third['processed_records']==6
    build(catalog)
    day=daily.daily_detail('2024-06-01',*catalog)
    assert day['summary']['observed_records']==6 and day['total']==6
    with sqlite3.connect(catalog[0]) as conn:assert conn.execute('SELECT count(*) FROM events').fetchone()[0]==6


def test_metrics_are_separate_by_unit_device_source_account_and_day_basis(catalog):
    event(catalog,'a',metadata=metric('weight',70000,'g'))
    event(catalog,'b',metadata=metric('weight',70,'kg'))
    event(catalog,'c',metadata=metric('weight',70001,'g',device='sensor-b'))
    event(catalog,'d',metadata=metric('weight',70002,'g'),source='source-b')
    event(catalog,'e',metadata=metric('weight',70003,'g'),when='2024-06-01T13:00:00')
    event(catalog,'f',metadata=metric('weight',70004,'g',account_id='other-account'))
    build(catalog)
    result=daily.daily_detail('2024-06-01',*catalog)
    assert result['summary']['raw_samples']==6 and len(result['rollups'])==6
    assert result['items']==[]
    assert {row['date_basis'] for row in result['rollups']}=={'utc','source_wall_time'}
    assert len({row['id'] for row in result['rollups']})==6
    assert {row['unit'] for row in result['rollups']}=={'g','kg'}


def test_sample_statistics_do_not_invent_totals_and_cumulative_uses_time_not_rowid(catalog):
    event(catalog,'point-a',metadata=metric(value=60))
    event(catalog,'point-b',metadata=metric(value=80))
    event(catalog,'counter-late',metadata=metric('steps',1000,'steps',aggregation='latest'),when='2024-06-01T14:00:00Z')
    event(catalog,'counter-early',metadata=metric('steps',800,'steps',aggregation='latest'),when='2024-06-01T13:00:00Z')
    build(catalog)
    rows={row['metric']:row for row in daily.daily_detail('2024-06-01',*catalog)['rollups']}
    assert rows['heart_rate']['sample_mean']==70 and rows['heart_rate']['min']==60 and rows['heart_rate']['max']==80
    assert rows['heart_rate']['sum'] is None and rows['heart_rate']['latest'] is None
    assert rows['steps']['latest']==1000 and rows['steps']['sum'] is None
    assert len(rows['heart_rate']['evidence_ids'])>=2


def test_additive_intervals_with_overlap_or_cross_day_do_not_get_a_total(catalog):
    for identity,start,end,value in [('a','12:00','12:10',5),('b','12:10','12:20',7)]:
        event(catalog,identity,when='2024-06-01T'+start+':00Z',metadata=metric('steps',value,'steps',aggregation='sum',start_time='2024-06-01T'+start+':00Z',end_time='2024-06-01T'+end+':00Z'))
    build(catalog)
    assert daily.daily_detail('2024-06-01',*catalog)['rollups'][0]['sum']==12
    event(catalog,'overlap',when='2024-06-01T12:15:00Z',metadata=metric('steps',2,'steps',aggregation='sum',start_time='2024-06-01T12:15:00Z',end_time='2024-06-01T12:25:00Z'))
    build(catalog)
    row=daily.daily_detail('2024-06-01',*catalog)['rollups'][0]
    assert row['sample_count']==3 and row['sum'] is None


def test_proven_duplicates_group_but_variants_and_same_timestamp_other_ids_survive(catalog):
    event(catalog,'sample-a',native='reading-1',metadata=metric(value=70))
    event(catalog,'sample-copy',native='reading-1',metadata=metric(value=70))
    event(catalog,'sample-variant',native='reading-1',metadata=metric(value=75))
    event(catalog,'sample-other',native='reading-2',metadata=metric(value=70))
    event(catalog,'message-a',native='message-1',category='message',body='Original message')
    event(catalog,'message-copy',native='message-1',category='message',body='Original message')
    event(catalog,'message-variant',native='message-1',category='message',body='Edited message')
    build(catalog)
    detail=daily.daily_detail('2024-06-01',*catalog)
    assert detail['summary']['observed_records']==7 and detail['summary']['raw_samples']==4
    assert detail['total']==2
    assert sorted(row['duplicate_count'] for row in detail['items'])==[0,1]
    rollup=detail['rollups'][0]
    assert rollup['sample_count']==2 and rollup['variant_count']==3 and rollup['source_record_count']==4 and rollup['duplicate_count']==1
    assert rollup['conflicting_occurrences']==1 and rollup['numeric_count']==1
    assert all(rollup[field] is None for field in ('min','max','sample_mean','sum','latest'))
    assert {'sample-a','sample-variant'}<=set(rollup['evidence_ids'])
    raw=daily.rollup_records(rollup['id'],*catalog,limit=2)
    assert raw['total']==4 and len(raw['items'])==2
    assert {row['id'] for row in raw['items']}=={'sample-a','sample-copy'}
    assert sorted(row['display_duplicate'] for row in raw['items'])==[False,True]


def test_technical_and_unknown_dates_are_separate_readable_layers(catalog):
    event(catalog,'message',category='message')
    event(catalog,'trace',category='shell_command')
    event(catalog,'month',when='2024-06',precision='month')
    event(catalog,'unknown',when=None,precision='unknown')
    event(catalog,'wall',when='2024-06-01',precision='day',metadata={'timezone_unknown':True})
    build(catalog)
    days=daily.daily_days(*catalog)
    assert [row['day'] for row in days['items']]==['2024-06-01','undated']
    assert daily.daily_detail('undated',*catalog)['summary']['observed_records']==2
    detail=daily.daily_detail('2024-06-01',*catalog)
    assert detail['summary']['technical_records']==1 and detail['total']==2
    assert daily.daily_detail('2024-06-01',*catalog,include_technical=True)['total']==3
    assert {row['basis'] for row in detail['summary']['date_bases']}=={'utc','source_wall_time'}


def test_batch_failure_rolls_back_projection_and_checkpoint_then_resumes(catalog,monkeypatch):
    for identity in ('a','b','c'):event(catalog,identity,category='post')
    version,original=daily._classifier()
    def broken(event,provenance):
        if event['id']=='b':raise ValueError('Synthetic classifier failure')
        return original(event,provenance)
    monkeypatch.setattr(daily,'_classifier',lambda:(version,broken))
    with pytest.raises(ValueError):build(catalog,batch_size=3)
    state=daily.daily_status(*catalog)
    assert state['checkpoint_rowid']==0 and state['processed_records']==0
    assert daily.daily_days(*catalog)['items']==[]
    monkeypatch.setattr(daily,'_classifier',lambda:(version,original))
    assert build(catalog)['processed_records']==3


def test_capacity_pause_preserves_a_committed_checkpoint(catalog,monkeypatch):
    for identity in ('a','b','c'):event(catalog,identity,category='post')
    calls=0
    def guard(*args):
        nonlocal calls
        calls+=1
        if calls==3:raise ImportPaused('synthetic_reserve')
    original=daily._capacity;monkeypatch.setattr(daily,'_capacity',guard)
    with pytest.raises(ImportPaused):build(catalog,batch_size=1)
    assert daily.daily_status(*catalog)['checkpoint_rowid']==1
    monkeypatch.setattr(daily,'_capacity',original)
    assert build(catalog)['processed_records']==3


def test_invalidation_age_and_catalog_anchor_require_explicit_rebuild(catalog):
    event(catalog,'a',metadata=metric(value=70));build(catalog)
    with transaction(catalog[0]) as conn:conn.execute("UPDATE events SET metadata=? WHERE id='a'",(json.dumps(metric(value=90)),))
    daily.invalidate_daily('synthetic_enrichment',*catalog)
    assert daily.daily_status(*catalog)['rebuild_required']
    with pytest.raises(RuntimeError):build(catalog)
    build(catalog,rebuild=True)
    assert daily.daily_detail('2024-06-01',*catalog)['rollups'][0]['max']==90
    with sqlite3.connect(catalog[1]) as conn:conn.execute("UPDATE state SET value=? WHERE key='last_full_rebuild_at'",(json.dumps('2000-01-01T00:00:00+00:00'),))
    assert 'projection_age_requires_rebuild' in daily.daily_status(*catalog)['rebuild_reasons']
    with pytest.raises(RuntimeError):build(catalog)


def test_q_filters_projected_readable_labels_and_rollups_without_raw_sensor_search(catalog):
    event(catalog,'note',category='post',title='Synthetic excursion')
    event(catalog,'sample',metadata=metric(value=70))
    event(catalog,'other-day',when='2024-06-02T12:00:00Z',category='post',title='Other label')
    build(catalog)
    assert daily.daily_days(*catalog,q='excursion')['total']==1
    detail=daily.daily_detail('2024-06-01',*catalog,q='excursion')
    assert detail['total']==1 and detail['rollups']==[]
    assert daily.daily_rollups(*catalog,q='heart')['total']==1
    assert daily.daily_days(*catalog,start='2024-06-02',end='2024-06-02')['total']==1


@pytest.mark.parametrize('sort,direction,expected',[
    ('date','asc',['2024-06-01','2024-06-02','2024-06-03','undated']),
    ('date','desc',['2024-06-03','2024-06-02','2024-06-01','undated']),
    ('records','desc',['2024-06-01','2024-06-02','2024-06-03','undated']),
    ('records','asc',['undated','2024-06-02','2024-06-03','2024-06-01']),
    ('sources','desc',['2024-06-01','2024-06-02','2024-06-03','undated']),
    ('sources','asc',['2024-06-03','undated','2024-06-01','2024-06-02']),
])
def test_day_sort_is_global_before_pagination_with_stable_ties(catalog,sort,direction,expected):
    rows=[('2024-06-01','source-a')]*3+[('2024-06-01','source-b'),('2024-06-02','source-a'),('2024-06-02','source-b')]+[('2024-06-03','source-a')]*2+[(None,'source-a')]
    for index,(day,source) in enumerate(rows):
        event(catalog,str(index),when=day+'T12:00:00Z' if day else None,source=source,category='post',title='Matching label' if index in {0,6} else 'Other label')
    build(catalog)
    pages=[daily.daily_days(*catalog,sort=sort,direction=direction,limit=1,offset=index) for index in range(4)]
    assert [page['items'][0]['day'] for page in pages]==expected
    assert all(page['total']==4 for page in pages)
    filtered=daily.daily_days(*catalog,sort='records',direction='asc',q='Matching')
    assert [(item['day'],item['observed_records']) for item in filtered['items']]==[('2024-06-03',2),('2024-06-01',4)]
    assert [item['day'] for item in daily.daily_days(*catalog,sort=sort,direction=direction,source='source-b')['items']]==(['2024-06-02','2024-06-01'] if sort=='date' and direction=='desc' else ['2024-06-01','2024-06-02'])
    assert daily.daily_days(*catalog,sort=sort,direction=direction,start='2024-06-02',end='2024-06-02')['total']==1


def test_day_sort_uses_constant_bounded_sidecar_query_count(catalog,monkeypatch):
    from contextlib import contextmanager
    for index in range(50):event(catalog,str(index),when=f'2024-{index//28+1:02d}-{index%28+1:02d}T12:00:00Z',category='post')
    build(catalog);original=daily._readonly;statements=[]
    @contextmanager
    def traced(path,*args,**kwargs):
        with original(path,*args,**kwargs) as conn:
            if Path(path)==catalog[1]:conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(daily,'_readonly',traced)
    result=daily.daily_days(*catalog,sort='records',limit=50)
    assert len(result['items'])==50
    aggregate_queries=[sql for sql in statements if any('FROM '+table in sql for table in ('day_counts','display_groups','rollups'))]
    assert len(aggregate_queries)==5
    assert all(item['display_groups']==1 and item['measurement_groups']==0 for item in result['items'])


@pytest.mark.parametrize('options',[{'sort':'unknown'},{'direction':'DESC'},{'sort':'records; DELETE FROM events'}])
def test_day_sort_rejects_unknown_keys_before_opening_catalog(catalog,monkeypatch,options):
    def forbidden(*args,**kwargs):raise AssertionError('Invalid sorts must not read the catalog')
    monkeypatch.setattr(daily,'daily_status',forbidden)
    with pytest.raises(ValueError):daily.daily_days(*catalog,**options)


def test_multi_metric_row_counts_source_once_and_conflicting_same_stream_values_are_not_chosen(catalog):
    event(catalog,'multi',metadata={'metric':{'name':'weight','unit':'kg','value':70},'beats per minute':80})
    # The health source label makes the explicit source header eligible too.
    with transaction(catalog[0]) as conn:conn.execute("UPDATE sources SET name='Google Fit' WHERE id='source-a'")
    event(catalog,'conflict',metadata={'metric':{'name':'heart_rate','unit':'bpm','value':60},'beats per minute':90})
    build(catalog)
    detail=daily.daily_detail('2024-06-01',*catalog)
    assert detail['summary']['observed_records']==2 and detail['summary']['raw_samples']==2
    rows={row['metric']:row for row in detail['rollups']}
    assert set(rows)=={'weight','heart_rate'}
    assert rows['weight']['sample_count']==1
    assert rows['heart_rate']['sample_count']==2 and rows['heart_rate']['numeric_count']==1
    assert rows['heart_rate']['min'] is None and rows['heart_rate']['max'] is None
    assert rows['heart_rate']['conflicting_occurrences']==1
    assert any('Conflicting source versions or same-stream fields' in note for note in rows['heart_rate']['notes'])


@pytest.mark.parametrize('raw_metadata',['null','[]','"not an object"','not-json'])
def test_non_object_metadata_is_safe_and_catalog_bytes_remain_untouched(catalog,raw_metadata):
    event(catalog,'a',when='2024-06-01',precision='day',category='post')
    with transaction(catalog[0]) as conn:conn.execute("UPDATE events SET metadata=? WHERE id='a'",(raw_metadata,))
    build(catalog)
    assert daily.daily_detail('2024-06-01',*catalog)['items'][0]['metadata']=={}
    with sqlite3.connect(catalog[0]) as conn:assert conn.execute("SELECT metadata FROM events WHERE id='a'").fetchone()[0]==raw_metadata


def test_fractional_instants_sort_correctly_without_changing_original_precision(catalog):
    event(catalog,'fraction',when='2024-06-01T12:00:00.100Z',precision='millisecond',metadata=metric('steps',200,'steps',aggregation='latest'))
    event(catalog,'whole',when='2024-06-01T12:00:00Z',precision='second',metadata=metric('steps',100,'steps',aggregation='latest'))
    build(catalog)
    row=daily.daily_detail('2024-06-01',*catalog)['rollups'][0]
    assert row['latest']==200
    assert row['first_at']=='2024-06-01T12:00:00.000000Z' and row['last_at']=='2024-06-01T12:00:00.100000Z'
    original=daily.rollup_records(row['id'],*catalog)['items']
    assert {item['id']:item['time_precision'] for item in original}=={'fraction':'millisecond','whole':'second'}


def test_conflicting_occurrence_versions_propagate_across_batches_and_days(catalog):
    event(catalog,'first',native='reading',metadata=metric(value=70))
    build(catalog)
    event(catalog,'changed',native='reading',metadata=metric(value=75),when='2024-06-02T12:00:00Z')
    build(catalog)
    for day in ('2024-06-01','2024-06-02'):
        row=daily.daily_detail(day,*catalog)['rollups'][0]
        assert row['sample_count']==1 and row['conflicting_occurrences']==1
        assert row['min'] is None and row['sample_mean'] is None


def test_same_catalog_or_hardlink_sidecar_is_rejected_before_schema_writes(catalog,tmp_path):
    import os
    with sqlite3.connect(catalog[0]) as conn:before=list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name'))
    with pytest.raises(ValueError):daily.build_daily(catalog[0],catalog[0],reserve_bytes=0,capacity_paths=[])
    alias=tmp_path/'hardlink.sqlite3';os.link(catalog[0],alias)
    with pytest.raises(ValueError):daily.build_daily(catalog[0],alias,reserve_bytes=0,capacity_paths=[])
    with sqlite3.connect(catalog[0]) as conn:assert list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name'))==before


def test_missing_configured_capacity_path_pauses_before_creating_sidecar(catalog,tmp_path):
    with pytest.raises(ImportPaused):daily.build_daily(*catalog,reserve_bytes=0,capacity_paths=[tmp_path/'unmounted'])
    assert not catalog[1].exists()


def test_nested_mutation_guards_pause_builder_and_require_rebuild(catalog):
    event(catalog,'a',category='post');build(catalog)
    first=daily.begin_daily_mutation(catalog_path=catalog[0],sidecar_path=catalog[1],reason='outer')
    second=daily.begin_daily_mutation(catalog_path=catalog[0],sidecar_path=catalog[1],reason='inner')
    with pytest.raises(ImportPaused):build(catalog,rebuild=True)
    daily.end_daily_mutation(second,catalog_path=catalog[0],sidecar_path=catalog[1])
    assert daily.daily_status(*catalog)['source_mutation_active']
    daily.end_daily_mutation(first,catalog_path=catalog[0],sidecar_path=catalog[1])
    state=daily.daily_status(*catalog)
    assert not state['source_mutation_active'] and state['rebuild_required']
    assert not build(catalog,rebuild=True)['rebuild_required']


def test_custom_catalog_mutation_never_invalidates_configured_real_sidecar(catalog,tmp_path,monkeypatch):
    from types import SimpleNamespace
    configured=tmp_path/'configured'
    monkeypatch.setattr(daily,'settings',SimpleNamespace(database=configured/'catalog.sqlite3',state_dir=configured/'state'))
    assert daily.begin_daily_mutation(catalog_path=catalog[0],reason='synthetic_test') is None
    assert not configured.exists()


def test_mutation_started_during_classification_cannot_commit_a_fresh_batch(catalog,monkeypatch):
    event(catalog,'a',category='post');version,original=daily._classifier();tokens=[]
    def mutating(event,provenance):
        if not tokens:tokens.append(daily.begin_daily_mutation(catalog_path=catalog[0],sidecar_path=catalog[1],reason='during_batch'))
        return original(event,provenance)
    monkeypatch.setattr(daily,'_classifier',lambda:(version,mutating))
    with pytest.raises(ImportPaused):build(catalog)
    assert daily.daily_status(*catalog)['checkpoint_rowid']==0
    daily.end_daily_mutation(tokens[0],catalog_path=catalog[0],sidecar_path=catalog[1])


@pytest.mark.parametrize('filename,content,original_mime',[
    ('photo.png',b'\x89PNG\r\n\x1a\nsynthetic','image/png'),
    ('photo.data',b'II*\x00synthetic','image/tiff'),
    ('photo.data',b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1','image/heic'),
])
def test_daily_and_rollup_originals_include_readonly_existing_media_links(catalog,tmp_path,filename,content,original_mime):
    import hashlib
    from biography.media import reconcile_media
    event(catalog,'photo',category='media')
    event(catalog,'reading',metadata={**metric(),'photos':[{'uri':filename}]})
    for identity,name,content in [('photo-file','export.zip!'+filename,content),('document','export.zip!samples.json',b'{}')]:
        digest=hashlib.sha256(content).hexdigest();path=tmp_path/digest;path.write_bytes(content)
        with transaction(catalog[0]) as conn:
            conn.execute('INSERT INTO objects VALUES(?,?,?,?)',(digest,len(content),str(path),'now'))
            conn.execute('INSERT INTO files(id,source_id,original_path,object_hash,bytes,created_at) VALUES(?,?,?,?,?,?)',(identity,'source-a',name,digest,len(content),'now'))
    with transaction(catalog[0]) as conn:
        for identity,file in [('photo','photo-file'),('reading','document')]:conn.execute('INSERT INTO evidence VALUES(?,?,?,?)',(identity,file,'fixture:1','fixture'))
    reconcile_media(catalog[0],event_ids=['photo','reading']);build(catalog)
    detail=daily.daily_detail('2024-06-01',*catalog)
    assert detail['items'][0]['media'][0]['url']=='/api/files/photo-file/media'
    raw=daily.rollup_records(detail['rollups'][0]['id'],*catalog)
    assert raw['items'][0]['media'][0]['url']=='/api/files/photo-file/media'
    for item in (detail['items'][0],raw['items'][0]):
        assert item['media'][0]['original_mime']==original_mime
        assert item['media'][0]['mime']==('image/png' if original_mime=='image/png' else 'image/jpeg')
    assert raw['items'][0]['provenance'][0]['path']=='export.zip!samples.json'


@pytest.mark.parametrize('code',[sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_BUSY|0x200])
def test_transient_source_lock_pauses_at_committed_checkpoint_and_resumes(catalog,monkeypatch,code):
    for identity in ('a','b','c'):event(catalog,identity,category='post')
    before=build(catalog,batch_size=2,max_batches=1)
    assert before['checkpoint_rowid']==2 and before['processed_records']==2
    original=daily._batch
    def locked_source(*args,**kwargs):
        error=sqlite3.OperationalError('database is locked')
        error.sqlite_errorcode=code
        raise error
    monkeypatch.setattr(daily,'_batch',locked_source)
    with pytest.raises(ImportPaused) as paused:build(catalog,batch_size=2)
    assert paused.value.reason=='daily_database_locked'
    state=daily.daily_status(*catalog)
    assert state['status']=='paused' and state['last_pause_reason']=='daily_database_locked'
    assert state['checkpoint_rowid']==2 and state['processed_records']==2 and state['watermark_rowid']==3
    assert daily.daily_detail('2024-06-01',*catalog)['summary']['observed_records']==2
    monkeypatch.setattr(daily,'_batch',original)
    resumed=build(catalog,batch_size=2)
    assert resumed['status']=='caught_up' and resumed['checkpoint_rowid']==3 and resumed['processed_records']==3
    assert resumed['last_error'] is None and resumed['last_pause_reason'] is None
    assert daily.daily_detail('2024-06-01',*catalog)['summary']['observed_records']==3


def test_unrelated_source_operational_error_is_not_retried_as_a_pause(catalog,monkeypatch):
    for identity in ('a','b','c'):event(catalog,identity,category='post')
    build(catalog,batch_size=2,max_batches=1)
    def broken_source(*args,**kwargs):
        error=sqlite3.OperationalError('no such table: source_events')
        error.sqlite_errorcode=sqlite3.SQLITE_ERROR
        raise error
    monkeypatch.setattr(daily,'_batch',broken_source)
    with pytest.raises(sqlite3.OperationalError):build(catalog)
    state=daily.daily_status(*catalog)
    assert state['status']=='failed' and state['last_pause_reason'] is None
    assert state['checkpoint_rowid']==2 and state['processed_records']==2


def test_daily_categories_use_classified_types_and_observed_record_counts(catalog):
    with transaction(catalog[0]) as conn:conn.execute("UPDATE sources SET name='Google Fit' WHERE id='source-a'")
    event(catalog,'reading',native='reading-one',metadata=metric(value=70))
    event(catalog,'reading-copy',native='reading-one',metadata=metric(value=70))
    event(catalog,'post',category='post',when=None,precision='unknown')
    build(catalog)
    result=daily.daily_categories(*catalog)
    assert result['items']==[{'name':'health_sample','category':'health_sample','count':2},{'name':'post','category':'post','count':1}]
    assert result['projection']['processed_records']==3
    with sqlite3.connect(catalog[0]) as conn:
        assert dict(conn.execute('SELECT category,count(*) FROM events GROUP BY category'))=={'record':2,'post':1}


def test_read_connection_honors_timeout_and_closes_when_setup_is_locked(catalog,monkeypatch):
    with daily._readonly(catalog[0],timeout=.25) as conn:
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0]==250
        assert conn.execute('PRAGMA query_only').fetchone()[0]==1
    class LockedConnection:
        closed=False
        def execute(self,sql):raise sqlite3.OperationalError('database is locked')
        def close(self):self.closed=True
    fake=LockedConnection()
    monkeypatch.setattr(daily.sqlite3,'connect',lambda *args,**kwargs:fake)
    with pytest.raises(sqlite3.OperationalError):
        with daily._readonly(catalog[0]):pass
    assert fake.closed


def test_source_lock_preserves_extended_code_and_actual_query_stage(catalog,monkeypatch):
    from contextlib import contextmanager
    for identity in ('a','b','c'):event(catalog,identity,category='post')
    build(catalog,batch_size=2,max_batches=1)
    original=daily._readonly
    @contextmanager
    def blocked_names(path,timeout=3):
        with original(path,timeout=timeout) as conn:
            class Reader:
                def execute(self,sql,*args):
                    if str(path)==str(catalog[0]) and sql=='SELECT id,name FROM sources':
                        error=sqlite3.OperationalError('database is locked');error.sqlite_errorcode=sqlite3.SQLITE_BUSY|0x100;error.sqlite_errorname='SQLITE_BUSY_RECOVERY';raise error
                    return conn.execute(sql,*args)
            yield Reader()
    monkeypatch.setattr(daily,'_readonly',blocked_names)
    with pytest.raises(ImportPaused) as paused:build(catalog)
    details=paused.value.details
    assert details['stage']=='source_names' and details['sqlite_errorcode']==sqlite3.SQLITE_BUSY|0x100
    assert details['sqlite_errorname']=='SQLITE_BUSY_RECOVERY' and details['busy_timeout_seconds']==5
    assert details['read_seconds']>=0
    monkeypatch.setattr(daily,'_readonly',original)
    assert daily.daily_status(*catalog)['checkpoint_rowid']==2
    assert build(catalog)['processed_records']==3


def test_batch_progress_reports_read_and_projection_time_without_snapshot_pin(catalog):
    event(catalog,'a',category='post');progress=[]
    result=build(catalog,progress=progress.append)
    assert result['checkpoint_rowid']==1 and len(progress)==1
    assert progress[0]['source_read_seconds']>=0 and progress[0]['projection_seconds']>=0
    assert set(progress[0]['source_stage_seconds'])=={'catalog_open','source_names','event_rows','source_evidence','catalog_close'}
