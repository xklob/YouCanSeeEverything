import sqlite3

import pytest
from fastapi.testclient import TestClient

from biography import api, db, queries
from biography.config import Settings


def seed(path):
    db.init_db(path)
    with db.transaction(path) as conn:
        conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES('source','Synthetic source','now','now')")
        conn.executemany("""INSERT INTO events(id,source_id,title,body,occurred_at,category,created_at)
            VALUES(?,'source','Shared word','Fictional searchable text',?,'note','now')""",
            [(f"event-{i}", f"2024-01-{i % 28 + 1:02d}") for i in range(2000)])


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    seed(path)
    return path


@pytest.mark.parametrize("filters", [
    {"offset": 5000},
    {"q": "shared", "offset": 1500},
    {"source": "source", "category": "note", "offset": 1500},
])
def test_expensive_reads_interrupt_and_release_the_connection(catalog, monkeypatch, filters):
    opened = []
    original = queries.connect

    def tracked(path):
        conn = original(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(queries, "connect", tracked)
    monkeypatch.setattr(queries, "RECORD_QUERY_MAX_OPCODES", 1000)
    with pytest.raises(queries.RecordQueryTimeout) as caught:
        queries.list_events(catalog, **filters)
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert caught.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    with db.transaction(catalog) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2000


def test_elapsed_deadline_is_checked_before_sql_and_closes_snapshot(catalog, monkeypatch):
    monkeypatch.setattr(queries, "RECORD_QUERY_SECONDS", 0)
    with pytest.raises(queries.RecordQueryTimeout):
        queries.list_events(catalog)
    with pytest.raises(queries.RecordQueryTimeout):
        queries.event_detail("event-0", catalog)


@pytest.mark.parametrize("failure", ["syntax", "authorization", "external_interrupt"])
def test_unrelated_sqlite_errors_are_not_relabelled_timeouts(catalog, monkeypatch, failure):
    original = queries.connect
    if failure == "syntax":
        monkeypatch.setattr(queries, "event_filters", lambda *args: (" WHERE missing_column=?", [1]))
    else:
        def failing(path):
            conn = original(path)
            if failure == "authorization":
                conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
            else:
                conn.set_trace_callback(lambda sql: conn.interrupt() if sql.startswith("SELECT e.*") else None)
            return conn
        monkeypatch.setattr(queries, "connect", failing)
    with pytest.raises(sqlite3.DatabaseError) as caught:
        queries.list_events(catalog)
    assert not isinstance(caught.value, queries.RecordQueryTimeout)
    if failure == "external_interrupt":
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT


def test_api_timeout_is_retryable_503_and_other_errors_keep_their_status(tmp_path, monkeypatch):
    local = Settings(data_dir=tmp_path / "vault", state_dir=tmp_path / "state", google_maps_api_key="")
    monkeypatch.setattr(api, "settings", local)
    monkeypatch.setattr(db, "settings", local)
    seed(local.database)
    with TestClient(api.app, base_url="http://127.0.0.1:8000") as client:
        monkeypatch.setattr(queries, "RECORD_QUERY_MAX_OPCODES", 1000)
        response = client.get("/api/events?offset=5000")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "2"
        assert "Narrow the date range or source" in response.json()["detail"]
        assert "SELECT" not in response.text and str(local.database) not in response.text
        assert client.get("/api/events?to=2024-99-01").status_code == 400
        assert client.get("/api/events?offset=-1").status_code == 422
        assert client.get("/api/events", headers={"host": "untrusted.example"}).status_code == 403
        monkeypatch.setattr(queries, "RECORD_QUERY_SECONDS", 0)
        assert client.get("/api/events/event-0").status_code == 503


def test_media_reads_use_the_same_remaining_request_budget(catalog, monkeypatch):
    from biography import media
    original = media.media_for_events
    observed = []

    def checked(ids, connection, **kwargs):
        observed.append(connection.execute("PRAGMA query_only").fetchone()[0])
        # Exceed the deadline during enrichment: the caller checks again before
        # publishing the response and closes this second read connection too.
        monkeypatch.setattr(queries, "RECORD_QUERY_MAX_OPCODES", 0)
        return original(ids, connection, **kwargs)

    monkeypatch.setattr(media, "media_for_events", checked)
    monkeypatch.setattr(queries, "get_count", lambda *args: {"total": None, "total_status": "pending", "counted_at": None})
    with pytest.raises(queries.RecordQueryTimeout):
        queries.list_events(catalog, limit=1)
    assert observed == [1]
