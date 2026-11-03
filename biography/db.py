"""Durable catalog; source bytes are immutable and indexes can be rebuilt."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
from .config import settings

def now():
    return datetime.now(timezone.utc).isoformat()

def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, account_key TEXT NOT NULL DEFAULT 'owner',
 roots TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'queued',
 snapshot_bytes INTEGER NOT NULL DEFAULT 0, queue_order INTEGER NOT NULL DEFAULT 0,
 notes TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), status TEXT NOT NULL,
 total_files INTEGER NOT NULL DEFAULT 0, completed_files INTEGER NOT NULL DEFAULT 0,
 total_bytes INTEGER NOT NULL DEFAULT 0, completed_bytes INTEGER NOT NULL DEFAULT 0,
 events INTEGER NOT NULL DEFAULT 0, error TEXT, started_at TEXT NOT NULL, finished_at TEXT,
 checkpoint TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS objects (
 sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL, path TEXT NOT NULL, verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), import_id TEXT REFERENCES imports(id),
 original_path TEXT NOT NULL, object_hash TEXT NOT NULL REFERENCES objects(sha256),
 bytes INTEGER NOT NULL, locator TEXT NOT NULL DEFAULT '', parser TEXT,
 parse_status TEXT NOT NULL DEFAULT 'pending', notes TEXT NOT NULL DEFAULT '[]',
 metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_source ON files(source_id);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), native_id TEXT,
 title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', occurred_at TEXT, end_at TEXT,
 category TEXT NOT NULL, time_precision TEXT NOT NULL DEFAULT 'unknown',
 metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events(occurred_at DESC,id);
CREATE INDEX IF NOT EXISTS events_source ON events(source_id,occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_category ON events(category,occurred_at DESC);
CREATE TABLE IF NOT EXISTS evidence (
 event_id TEXT NOT NULL REFERENCES events(id), file_id TEXT NOT NULL REFERENCES files(id),
 locator TEXT NOT NULL, parser TEXT NOT NULL,
 PRIMARY KEY(event_id,file_id,locator)
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(title,body,content='events',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
 INSERT INTO events_fts(rowid,title,body) VALUES(new.rowid,new.title,new.body);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
 INSERT INTO events_fts(events_fts,rowid,title,body) VALUES('delete',old.rowid,old.title,old.body);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
 INSERT INTO events_fts(events_fts,rowid,title,body) VALUES('delete',old.rowid,old.title,old.body);
 INSERT INTO events_fts(rowid,title,body) VALUES(new.rowid,new.title,new.body);
END;
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
 result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 revision_id TEXT, can_undo INTEGER NOT NULL DEFAULT 0
);
PRAGMA user_version=1;
"""

def connect(path: Path | str | None = None):
    p = Path(path or settings.database)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn

def init_db(path: Path | str | None = None):
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)

@contextmanager
def transaction(path=None):
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def register_source(name: str, roots: list[str], size: int, order: int, path=None):
    sid = identity("source", settings.account_key, name)
    with transaction(path) as conn:
        previous=conn.execute('SELECT roots FROM sources WHERE id=?',(sid,)).fetchone()
        if previous:roots=list(dict.fromkeys(json.loads(previous['roots'])+roots))
        conn.execute("""INSERT INTO sources(id,name,account_key,roots,snapshot_bytes,queue_order,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          roots=excluded.roots,snapshot_bytes=excluded.snapshot_bytes,queue_order=excluded.queue_order,updated_at=excluded.updated_at""",
          (sid,name,settings.account_key,json.dumps(roots),size,order,now(),now()))
    return sid

def add_events(conn, source_id, file_id, object_hash, result):
    count = 0
    for index, event in enumerate(result.events):
        title = str(event.get("title") or "Untitled record")
        body = str(event.get("body") or "")
        metadata = event.get("metadata") or {}
        native = event.get("source_native_id")
        locator = str(event.get("locator") or f"record:{index}")
        category = str(event.get("category") or "document")
        payload = [title,body,event.get("occurred_at"),event.get("end_at"),category,event.get("time_precision"),metadata]
        # Native identities coalesce repeated exports. Distinct content remains a variant.
        fingerprint = metadata.get('record_fingerprint')
        key = (["native",str(native),category] if native is not None else
               ["fingerprint",fingerprint,category] if fingerprint else ["occurrence",object_hash,locator])
        eid = identity(source_id,key,payload)
        git_identity = bool(native and str(native).startswith('git:') and category in ('git_commit','project_context'))
        if git_identity:
            # The commit hash identifies immutable Git content across worktrees/providers.
            eid = identity(source_id,'git_commit',str(native))
        cursor = conn.execute("""INSERT OR IGNORE INTO events(id,source_id,native_id,title,body,occurred_at,end_at,category,time_precision,metadata,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(eid,source_id,str(native) if native is not None else None,title,body,event.get("occurred_at"),event.get("end_at"),category,event.get("time_precision") or "unknown",json.dumps(metadata,ensure_ascii=False),now()))
        count += cursor.rowcount
        if git_identity and category=='git_commit':
            conn.execute("UPDATE events SET title=?,body=?,category=?,metadata=? WHERE id=? AND category='project_context'",(title,body,category,json.dumps(metadata,ensure_ascii=False),eid))
        conn.execute("INSERT OR IGNORE INTO evidence(event_id,file_id,locator,parser) VALUES(?,?,?,?)",(eid,file_id,locator,result.parser))
    return count
