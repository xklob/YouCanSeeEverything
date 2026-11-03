"""Exact catalog counters with a bounded, resumable historical backfill.

The counted population while building is rowid <= checkpoint OR rowid > the
fixed initial watermark. Triggers and each backfill batch share the source
transaction. Numeric boundaries remain valid after deleted/reused rowids; no
rowid is used as a count or as an assumed surviving anchor. Metadata snapshots
omit this derived state and initialize a fresh backfill after restore.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from .config import settings

VERSION = 1
TABLES = ('catalog_count_state', 'catalog_event_counts')
TRIGGERS = ('catalog_counts_insert', 'catalog_counts_delete',
            'catalog_counts_update', 'catalog_counts_replace_guard', 'catalog_counts_update_guard')
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS catalog_count_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('counting','current')),
        watermark_rowid INTEGER NOT NULL, checkpoint_rowid INTEGER,
        scanned_records INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        schema_cookie INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS catalog_event_counts (
        scope TEXT NOT NULL, key TEXT NOT NULL,
        total INTEGER NOT NULL CHECK(total>=0),
        undated INTEGER NOT NULL CHECK(undated>=0 AND undated<=total),
        PRIMARY KEY(scope,key))""",
)
_CLOCK = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _eligible(alias):
    return f"""EXISTS (SELECT 1 FROM catalog_count_state WHERE singleton=1 AND
        (phase='current' OR {alias}.rowid>watermark_rowid OR
        (checkpoint_rowid IS NOT NULL AND {alias}.rowid<=checkpoint_rowid)))"""


def _delta(alias, increase):
    statements = []
    for scope, key in (('all', "''"), ('source', f'{alias}.source_id'),
                       ('category', f'{alias}.category')):
        if increase:
            statements.append(f"""INSERT INTO catalog_event_counts(scope,key,total,undated)
                SELECT '{scope}',{key},1,({alias}.occurred_at IS NULL) WHERE {_eligible(alias)}
                ON CONFLICT(scope,key) DO UPDATE SET total=total+1,
                    undated=undated+excluded.undated;""")
        else:
            statements.append(f"""UPDATE catalog_event_counts SET total=total-1,
                undated=undated-({alias}.occurred_at IS NULL)
                WHERE scope='{scope}' AND key={key} AND {_eligible(alias)};""")
    return '\n'.join(statements)


_TRIGGER_SQL = (
    f"""CREATE TRIGGER catalog_counts_insert AFTER INSERT ON events BEGIN
        {_delta('new', True)}
        UPDATE catalog_count_state SET updated_at={_CLOCK} WHERE singleton=1;
    END""",
    f"""CREATE TRIGGER catalog_counts_delete AFTER DELETE ON events BEGIN
        {_delta('old', False)}
        UPDATE catalog_count_state SET updated_at={_CLOCK} WHERE singleton=1;
    END""",
    f"""CREATE TRIGGER catalog_counts_update AFTER UPDATE ON events
        WHEN old.rowid IS NOT new.rowid OR old.source_id IS NOT new.source_id
          OR old.category IS NOT new.category OR old.occurred_at IS NOT new.occurred_at
        BEGIN
        {_delta('old', False)}
        {_delta('new', True)}
        UPDATE catalog_count_state SET updated_at={_CLOCK} WHERE singleton=1;
    END""",
    # SQLite suppresses implicit REPLACE deletes unless recursive_triggers is
    # enabled on that writer. Reject conflicting writes from unconfigured
    # writers, including IGNORE, rather than silently overcount a replacement.
    """CREATE TRIGGER catalog_counts_update_guard BEFORE UPDATE ON events
        WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
          AND (new.id IS NOT old.id OR new.rowid IS NOT old.rowid)
          AND EXISTS (SELECT 1 FROM events WHERE rowid<>old.rowid AND (id=new.id OR rowid=new.rowid))
        BEGIN SELECT RAISE(ABORT,'Catalog writes require PRAGMA recursive_triggers=ON for conflicting updates'); END""",
    """CREATE TRIGGER catalog_counts_replace_guard BEFORE INSERT ON events
        WHEN (SELECT recursive_triggers FROM pragma_recursive_triggers)=0
          AND EXISTS (SELECT 1 FROM events WHERE id=new.id OR rowid=new.rowid)
        BEGIN SELECT RAISE(ABORT,'Catalog writes require PRAGMA recursive_triggers=ON for conflicting inserts'); END""",
)


def ensure_schema(conn):
    """Create empty derived tables only; safe for metadata restore schemas."""
    for statement in _SCHEMA:
        conn.execute(statement)


def _installed(conn):
    present = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE name IN (" + ",".join("?" for _ in TABLES + TRIGGERS) + ")", TABLES + TRIGGERS)}
    return present == set(TABLES + TRIGGERS)


def ensure_counts(conn, *, reset=False):
    """Install atomically, without scanning events. Existing healthy state is cheap.

    Caller transactions are preserved. Normal writers must enable recursive
    triggers; this function does so before any transaction it starts.
    """
    conn.execute('PRAGMA recursive_triggers=ON')
    if not reset and _installed(conn):
        state = conn.execute('SELECT version,phase,schema_cookie FROM catalog_count_state WHERE singleton=1').fetchone()
        global_row = conn.execute("SELECT 1 FROM catalog_event_counts WHERE scope='all' AND key=''").fetchone()
        if state and state[0] == VERSION and global_row and (state[1] == 'current' or
                state[2] == conn.execute('PRAGMA schema_version').fetchone()[0]):
            return
    owned = not conn.in_transaction
    conn.execute('BEGIN IMMEDIATE' if owned else 'SAVEPOINT count_install')
    try:
        ensure_schema(conn)
        # Recheck under the writer lock: another process may have installed it.
        state = conn.execute('SELECT version,phase,schema_cookie FROM catalog_count_state WHERE singleton=1').fetchone()
        global_row = conn.execute("SELECT 1 FROM catalog_event_counts WHERE scope='all' AND key=''").fetchone()
        if reset or not (_installed(conn) and state and state[0] == VERSION and global_row and
                (state[1] == 'current' or state[2] == conn.execute('PRAGMA schema_version').fetchone()[0])):
            for name in TRIGGERS:
                conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            conn.execute('DELETE FROM catalog_event_counts')
            conn.execute('DELETE FROM catalog_count_state')
            maximum = conn.execute('SELECT rowid FROM events ORDER BY rowid DESC LIMIT 1').fetchone()
            conn.execute("INSERT INTO catalog_event_counts VALUES('all','',0,0)")
            conn.execute('INSERT INTO catalog_count_state VALUES(1,?,?,?,?,0,?,0)',
                         (VERSION, 'counting' if maximum else 'current', maximum[0] if maximum else 0,
                          None, datetime.now(timezone.utc).isoformat()))
            for statement in _TRIGGER_SQL:
                conn.execute(statement)
            conn.execute('UPDATE catalog_count_state SET schema_cookie=? WHERE singleton=1',
                         (conn.execute('PRAGMA schema_version').fetchone()[0],))
        conn.execute('COMMIT' if owned else 'RELEASE count_install')
    except BaseException:
        if owned:
            conn.rollback()
        else:
            conn.execute('ROLLBACK TO count_install')
            conn.execute('RELEASE count_install')
        raise


def _unknown(error):
    return {'total_events': None, 'undated_count': None,
            'date_range': {'start': None, 'end': None}, 'activity': None,
            'categories': None, 'by_source': None,
            'stats': {'status': 'unavailable', 'counted_events': None,
                      'measured_at': None, 'watermark': None, 'checkpoint_rowid': None,
                      'scanned_records': None, 'refreshing': False,
                      'categories_status': 'unknown', 'activity_status': 'not_computed',
                      'error': error}}


def _bounds(conn, source=None):
    where = 'occurred_at IS NOT NULL'
    index, args = 'events_time', ()
    if source is not None:
        where = 'source_id=? AND ' + where
        index, args = 'events_source', (source,)
    sql = f'SELECT occurred_at FROM events INDEXED BY {index} WHERE {where} ORDER BY occurred_at '
    first = conn.execute(sql + 'ASC LIMIT 1', args).fetchone()
    last = conn.execute(sql + 'DESC LIMIT 1', args).fetchone()
    return {'start': first[0] if first else None, 'end': last[0] if last else None}


def read_counts(conn, *, bounds=True, max_groups=10000):
    """Read only small aggregate tables, with optional covering-index seeks."""
    if not _installed(conn):
        return _unknown('counts_not_initialized')
    row = conn.execute('SELECT * FROM catalog_count_state WHERE singleton=1').fetchone()
    if row is None or row['version'] != VERSION:
        return _unknown('counts_rebuild_required')
    state = dict(row)
    # VACUUM may renumber implicit rowids without firing source triggers. Its
    # schema-cookie change invalidates only a partial boundary, conservatively
    # also restarting after unrelated DDL. Completed counters survive VACUUM.
    if state['phase'] == 'counting' and state['schema_cookie'] != conn.execute('PRAGMA schema_version').fetchone()[0]:
        return _unknown('counts_rebuild_required')
    entries = conn.execute('SELECT scope,key,total,undated FROM catalog_event_counts LIMIT ?',
                           (max_groups + 1,)).fetchall()
    if len(entries) > max_groups:
        return _unknown('summary_result_limit')
    groups = {(entry[0], entry[1]): (entry[2], entry[3]) for entry in entries}
    global_values = groups.get(('all', ''))
    if global_values is None or any(
        tuple(sum(value[i] for (kind, _), value in groups.items() if kind == scope) for i in (0, 1))
        != global_values for scope in ('source', 'category')):
        return _unknown('counts_rebuild_required')
    current = state['phase'] == 'current'
    result = _unknown(None)
    result.update(total_events=global_values[0] if current else None,
                  undated_count=global_values[1] if current else None)
    result['stats'].update(status=state['phase'], counted_events=global_values[0],
        measured_at=state['updated_at'], watermark=state['watermark_rowid'],
        checkpoint_rowid=state['checkpoint_rowid'], scanned_records=state['scanned_records'],
        categories_status='measured' if current else 'counting')
    sources = conn.execute('SELECT id FROM sources LIMIT ?', (max_groups + 1,)).fetchall()
    if len(sources) > max_groups:
        return _unknown('summary_result_limit')
    result['by_source'] = {}
    for source in sources:
        sid = source[0]
        total, undated = groups.get(('source', sid), (0, 0))
        result['by_source'][sid] = {'total_events': total if current else None,
            'counted_events': total, 'undated_count': undated if current else None,
            'date_range': {'start': None, 'end': None}}
    categories = {key for (scope, key), value in groups.items() if scope == 'category' and value[0]}
    def category_rows():
        return [{'name': name, 'count': groups.get(('category', name), (0, 0))[0] if current else None,
                 'counted_events': groups.get(('category', name), (0, 0))[0]}
                for name in sorted(categories, key=lambda name: (-groups.get(('category', name), (0, 0))[0], name))]
    result['categories'] = category_rows()
    result['stats']['date_bounds_status'] = 'not_requested'
    if bounds:
        # Totals have already been validated and are independent of optional
        # discovery/bounds. A failed seek must never hide an exact counter.
        result['stats']['date_bounds_status'] = 'unavailable'
        try:
            if not current:
                previous = None
                result['stats']['categories_status'] = 'names_partial'
                for _ in range(max_groups):
                    found = conn.execute('SELECT category FROM events INDEXED BY events_category ' +
                        ('WHERE category>? ' if previous is not None else '') +
                        'ORDER BY category LIMIT 1', (previous,) if previous is not None else ()).fetchone()
                    if found is None:
                        result['stats']['categories_status'] = 'counting'
                        break
                    previous = found[0]
                    categories.add(previous)
                result['categories'] = category_rows()
            result['date_range'] = _bounds(conn)
            result['stats']['date_bounds_status'] = 'partial'
            for sid, source in result['by_source'].items():
                source['date_range'] = _bounds(conn, sid)
            result['stats']['date_bounds_status'] = 'current'
        except (sqlite3.Error, OSError) as exc:
            result['categories'] = category_rows()
            code = getattr(exc, 'sqlite_errorcode', 0) & 255
            result['stats']['details_error'] = ('count_read_budget_exceeded' if code == sqlite3.SQLITE_INTERRUPT
                else 'catalog_busy' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                else 'optional_details_unavailable')
    return result


def count_status(path=None, *, bounds=True, query_seconds=0.75, max_opcodes=250000):
    """Cheap read-only current snapshot; missing state is unknown, never zero."""
    target = Path(path or settings.database).expanduser().resolve()
    conn = None
    started, opcodes = time.monotonic(), 0
    def progress():
        nonlocal opcodes
        opcodes += 1000
        return int(opcodes >= max_opcodes or time.monotonic() - started >= query_seconds)
    try:
        conn = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True, timeout=0.1)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.set_progress_handler(progress, 1000)
        conn.execute('BEGIN')
        return read_counts(conn, bounds=bounds)
    except (OSError, sqlite3.Error) as exc:
        code = getattr(exc, 'sqlite_errorcode', 0) & 255
        return _unknown('catalog_busy' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        else 'count_read_budget_exceeded' if code == sqlite3.SQLITE_INTERRUPT
                        else 'catalog_unavailable')
    finally:
        if conn is not None:
            conn.close()


def build_counts(path=None, *, batch_size=2000, max_batches=1, max_seconds=2, reset=False):
    """Advance bounded rowid batches, releasing the writer lock after each batch.

    Returns the same truthful status plus batches/processed_in_run/paused. Busy
    writers or a per-batch deadline roll back the entire batch and leave its
    checkpoint unchanged. A separate worker should call again after yielding.
    """
    if not 1 <= batch_size <= 10000 or not 1 <= max_batches <= 10000 or max_seconds < 0:
        raise ValueError('Invalid count build limits')
    target = Path(path or settings.database).expanduser().resolve()
    started, batches, processed, paused = time.monotonic(), 0, 0, None
    conn = None
    try:
        conn = sqlite3.connect(target.as_uri() + '?mode=rw', uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA recursive_triggers=ON')
        ensure_counts(conn, reset=reset)
        while batches < max_batches and time.monotonic() - started < max_seconds:
            deadline = min(started + max_seconds, time.monotonic() + 2)
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute('BEGIN IMMEDIATE')
            state = conn.execute('SELECT * FROM catalog_count_state WHERE singleton=1').fetchone()
            if state['phase'] == 'counting' and state['schema_cookie'] != conn.execute('PRAGMA schema_version').fetchone()[0]:
                conn.rollback()
                paused = 'counts_rebuild_required'
                break
            if state['phase'] == 'current':
                conn.rollback()
                break
            checkpoint, high = state['checkpoint_rowid'], state['watermark_rowid']
            sql = 'SELECT rowid,source_id,category,occurred_at FROM events WHERE rowid<=?'
            args = [high]
            if checkpoint is not None:
                sql += ' AND rowid>?'
                args.append(checkpoint)
            rows = conn.execute(sql + ' ORDER BY rowid LIMIT ?', [*args, batch_size]).fetchall()
            delta = Counter()
            for row in rows:
                for scope, key in (('all', ''), ('source', row['source_id']), ('category', row['category'])):
                    delta[(scope, key, 'total')] += 1
                    delta[(scope, key, 'undated')] += int(row['occurred_at'] is None)
            keys = {(scope, key) for scope, key, _ in delta}
            conn.executemany('''INSERT INTO catalog_event_counts(scope,key,total,undated) VALUES(?,?,?,?)
                ON CONFLICT(scope,key) DO UPDATE SET total=total+excluded.total,undated=undated+excluded.undated''',
                [(scope, key, delta[(scope,key,'total')], delta[(scope,key,'undated')]) for scope,key in keys])
            next_checkpoint = rows[-1]['rowid'] if rows else high
            # No COUNT or OFFSET: this is one rowid seek, even when H was deleted.
            more = conn.execute('SELECT 1 FROM events WHERE rowid>? AND rowid<=? LIMIT 1',
                                (next_checkpoint, high)).fetchone()
            phase = 'counting' if more else 'current'
            conn.execute(f'''UPDATE catalog_count_state SET checkpoint_rowid=?,phase=?,
                scanned_records=scanned_records+?,updated_at={_CLOCK} WHERE singleton=1''',
                (next_checkpoint if more else high, phase, len(rows)))
            conn.commit()
            batches += 1
            processed += len(rows)
            if not more:
                break
    except sqlite3.OperationalError as exc:
        code = getattr(exc, 'sqlite_errorcode', 0) & 255
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT):
            paused = 'catalog_busy' if code != sqlite3.SQLITE_INTERRUPT else 'count_batch_budget_exceeded'
        else:
            raise
    finally:
        if conn is not None:
            conn.set_progress_handler(None, 0)
            conn.close()
    result = count_status(target, bounds=False)
    result.update(batches=batches, processed_in_run=processed, paused=paused)
    return result
