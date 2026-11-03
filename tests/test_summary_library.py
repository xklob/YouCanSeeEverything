import pytest

from biography import summary_library,synthesis


@pytest.fixture
def library(tmp_path):
    root=tmp_path/'private/.local/summaries';root.mkdir(parents=True)
    values=[
        ('a','alpha','2024-01','2024-06-01T12:00:00Z'),
        ('b','Beta','2024-03','2024-06-01T12:00:00.100000Z'),
        ('c','alpha',None,'2024-05-30T00:00:00Z'),
        ('d','', '2024-02',''),
        ('e','Gamma','2024-01','2024-06-01T14:00:00.050000+02:00'),
    ]
    for key,title,period,generated in values:
        summary_library.register(root,{'kind':'period_summary' if period else 'trip_summary','title':title,'period':period,
            'cache':{'key':key*64,'generated_at':generated},'citations':['event'],'evidence_ids':['event'],'selection':{'source_ids':[]}})
    return root


@pytest.mark.parametrize('sort,direction,expected',[
    ('generated','desc',['b','e','a','c','d']),('generated','asc',['c','a','e','b','d']),
    ('period','desc',['b','d','a','e','c']),('period','asc',['a','e','d','b','c']),
    ('title','desc',['e','b','a','c','d']),('title','asc',['a','c','b','e','d']),
])
def test_saved_summary_sort_is_global_stable_and_missing_last(library,sort,direction,expected):
    pages=[synthesis.list_saved(library,sort=sort,direction=direction,limit=1,offset=index) for index in range(5)]
    assert [page['items'][0]['key'][0] for page in pages]==expected
    assert [page['has_more'] for page in pages]==[True,True,True,True,False]
    assert all(page['sort']==sort and page['direction']==direction for page in pages)
    assert synthesis.list_saved(library)['items'][0]['key']=='b'*64


@pytest.mark.parametrize('options',[{'sort':'citations'},{'direction':'DESC'},{'sort':'title; DROP TABLE entries'}])
def test_saved_summary_sort_rejects_unknown_keys_before_cache_access(tmp_path,options):
    root=tmp_path/'private/.local/summaries'
    with pytest.raises(ValueError):synthesis.list_saved(root,**options)
    assert not root.exists()
