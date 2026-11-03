import pytest
from sorting_fixtures import SortingFixtures


@pytest.mark.parametrize('path,fields', [('/api/events',['date','imported']),('/api/days',['date','records','sources']),
    ('/api/journeys',['date','distance','duration','stops']),('/api/summaries/library',['generated','period','title']),
    ('/api/imports',['date','name','records','files','bytes','status']),('/api/jobs',['date','status','kind','prompt'])])
@pytest.mark.parametrize('direction',['asc','desc'])
def test_sort_is_global_before_single_item_pages(path,fields,direction):
    fixture=SortingFixtures()
    for field in fields:
        full=fixture(f'{path}?sort={field}&direction={direction}&limit=200')['items']
        first=[fixture(f'{path}?sort={field}&direction={direction}&limit=1&offset={i}')['items'][0] for i in range(3)]
        assert first==full[:3]
        assert len({str(row.get('id',row.get('day',row.get('key')))) for row in first})==3


def test_null_last_counting_and_filter_snapshots():
    fixture=SortingFixtures()
    for direction in ('asc','desc'):
        days=fixture(f'/api/days?sort=date&direction={direction}&limit=200')['items']
        assert days[-1]['day']=='undated'
        summaries=fixture(f'/api/summaries/library?sort=period&direction={direction}&limit=200')['items']
        assert all(row['period'] is None for row in summaries[-15:])
    first=fixture('/api/events?source=qa-one&q=Sort&sort=imported&direction=asc&limit=40')['items']
    second=fixture('/api/events?source=qa-one&q=Sort&sort=imported&direction=asc&limit=40&offset=40')['items']
    assert len(first)==40 and len(second)==2
    assert all(row['source_id']=='qa-one' for row in first+second)
    assert fixture('/api/overview')['total_events'] is None
    fixture.phase='subset'
    assert fixture('/api/overview')['stats']['counted_events']==37
    assert [row['stats']['counted_events'] for row in fixture('/api/sources')['items']]==[17,20,0]
    fixture.phase='unavailable'
    assert fixture('/api/overview')['total_events'] is None
    fixture.phase='current'
    assert fixture('/api/overview')['total_events']==84
    assert fixture('/api/sources')['items'][-1]['event_count']==0
