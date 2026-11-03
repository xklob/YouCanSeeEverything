import pytest
from fastapi.testclient import TestClient

from biography import agents, api, db, synthesis
from biography.config import Settings


class DeferredExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))

    def run(self):
        function, args = self.calls.pop(0)
        function(*args)


@pytest.fixture
def client(tmp_path, monkeypatch):
    local = Settings(state_dir=tmp_path / '.local', data_dir=tmp_path / 'vault')
    executor = DeferredExecutor()
    monkeypatch.setattr(api, 'settings', local)
    monkeypatch.setattr(db, 'settings', local)
    monkeypatch.setattr(api, 'EXECUTOR', executor)
    with TestClient(api.app, base_url='http://127.0.0.1:8000') as session:
        yield session, executor, local


def test_library_routes_only_read_saved_results_and_do_not_queue_inference(client, monkeypatch):
    session, executor, local = client
    observed = []
    key = 'b' * 64
    entry = {'key': key, 'title': 'A fictional month', 'kind': 'period_summary', 'freshness': 'not_checked'}
    saved = {'status': 'saved', 'freshness': 'not_checked', 'result': {'answer': 'A cited account of the month.'}}
    def listed(root, **kwargs):
        observed.append(('list', root, kwargs))
        return {'items': [entry], 'has_more': False}
    def read(identity, root):
        observed.append(('read', identity, root))
        return saved if identity == key else None
    def no_model(*args, **kwargs):
        raise AssertionError('Saved summary browsing must not invoke a model')
    monkeypatch.setattr(synthesis, 'list_saved', listed)
    monkeypatch.setattr(synthesis, 'saved_summary', read)
    monkeypatch.setattr(agents, '_run', no_model)
    assert session.get('/api/summaries/library?limit=7&offset=2').json()['items'] == [entry]
    assert session.get('/api/summaries/saved/' + key).json() == saved
    assert session.get('/api/summaries/saved/' + 'c' * 64).status_code == 404
    assert observed[0] == ('list', local.summaries_dir, {'limit': 7, 'offset': 2})
    assert not executor.calls
    with db.connect(local.database) as conn:
        assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_trip_cache_lookup_does_not_resolve_or_generate_a_trip(client, monkeypatch):
    from biography import trip_synthesis
    session, executor, local = client
    observed = {}
    identity = 'a' * 64
    def status(trip_id, root, path, **kwargs):
        observed.update(trip_id=trip_id, root=root, path=path, **kwargs)
        return {'status': 'missing', 'result': None, 'freshness': 'not_checked'}
    def forbidden(*args, **kwargs):
        raise AssertionError('GET may only inspect the saved cache')
    monkeypatch.setattr(trip_synthesis, 'trip_summary_status', status)
    monkeypatch.setattr(trip_synthesis, 'summarize_trip', forbidden)
    response = session.get('/api/summaries/trip', params={'trip_id': identity, 'source': 'notes', 'focus': 'Friends', 'model': 'gpt-6-astra'})
    assert response.status_code == 200 and response.json()['status'] == 'missing'
    assert observed == {'trip_id': identity, 'root': local.summaries_dir, 'path': local.database,
                        'source_ids': ['notes'], 'focus': 'Friends', 'model': 'gpt-6-astra'}
    assert not executor.calls


@pytest.mark.parametrize('payload', [
    {'mode': 'summarize_trip'},
    {'mode': 'summarize_trip', 'trip_id': '../outside'},
    {'mode': 'summarize_trip', 'trip_id': 'a' * 64, 'period': '2024-01'},
    {'mode': 'summarize_trip', 'trip_id': 'a' * 64, 'source_ids': [str(i) for i in range(33)]},
    {'mode': 'summarize_trip', 'trip_id': 'a' * 64, 'prompt': 'x' * 4001},
])
def test_invalid_trip_requests_do_not_queue_jobs(client, payload):
    session, executor, _ = client
    session.headers['x-biography-token'] = session.get('/api/session').json()['token']
    assert session.post('/api/assistant', json=payload).status_code == 400
    assert not executor.calls


def test_trip_generation_is_explicit_and_forwards_identity_not_client_dates(client, monkeypatch):
    from biography import trip_synthesis
    session, executor, local = client
    observed = {}
    identity = 'a' * 64
    payload = {'mode': 'summarize_trip', 'trip_id': identity, 'source_ids': ['notes'],
               'prompt': 'What is supported by the records?', 'model': 'gpt-6.1-sol'}
    assert session.post('/api/assistant', json=payload).status_code == 403
    session.headers['x-biography-token'] = session.get('/api/session').json()['token']
    def summarize(trip_id, root, path, **kwargs):
        observed.update(trip_id=trip_id, root=root, path=path, **kwargs)
        return {'kind': 'trip_summary', 'answer': 'A source-backed trip account.', 'citations': []}
    monkeypatch.setattr(trip_synthesis, 'summarize_trip', summarize)
    response = session.post('/api/assistant', json=payload)
    assert response.status_code == 202 and observed == {}
    executor.run()
    job = session.get('/api/jobs/' + response.json()['id']).json()
    assert job['status'] == 'completed' and job['result']['kind'] == 'trip_summary'
    assert not job['can_undo']
    assert observed == {'trip_id': identity, 'root': local.summaries_dir, 'path': local.database,
                        'source_ids': ['notes'], 'focus': payload['prompt'], 'model': 'gpt-6.1-sol'}


def test_media_status_route_is_read_only_and_uses_the_configured_catalog(client, monkeypatch):
    from biography import media
    session, executor, local = client
    observed = []
    def status(path):
        observed.append(path)
        return {'status': 'discovering', 'processed_records': 10, 'has_more': True}
    monkeypatch.setattr(media, 'media_candidate_status', status)
    assert session.get('/api/media/status').json()['has_more'] is True
    assert observed == [local.database] and not executor.calls
