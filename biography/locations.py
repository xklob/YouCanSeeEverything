"""Rebuildable indexes of coordinates explicitly present in source records.

No geocoding, interpolated paths, or inferred connections are created here.
Each point retains its source metadata locator and time basis.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import math
import re
import sqlite3
import time

from .db import connect
from . import db
from .queries import event_filters

INDEX_VERSION = 2
LOCATION_CATEGORIES = ("location", "place_visit", "travel")


@contextmanager
def _connection(value=None):
    if isinstance(value, sqlite3.Connection):
        yield value
        return
    conn = connect(value)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def ensure_schema(conn_or_path=None):
    with _connection(conn_or_path) as conn:
        statements = [
            """CREATE TABLE IF NOT EXISTS event_locations (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                point_index INTEGER NOT NULL,
                latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
                longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
                occurred_at TEXT,
                kind TEXT NOT NULL,
                locator TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(event_id,point_index))""",
            "CREATE INDEX IF NOT EXISTS event_locations_event ON event_locations(event_id)",
            "CREATE INDEX IF NOT EXISTS event_locations_time ON event_locations(occurred_at,id)",
            "CREATE INDEX IF NOT EXISTS event_locations_coordinates ON event_locations(latitude,longitude)",
            """CREATE TABLE IF NOT EXISTS location_index_state (
                event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                metadata_sha256 TEXT NOT NULL,
                point_count INTEGER NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS location_discovery_state (
                scope TEXT PRIMARY KEY,source_id TEXT,version INTEGER NOT NULL,
                watermark_rowid INTEGER NOT NULL,watermark_event_id TEXT,
                checkpoint_rowid INTEGER NOT NULL,scanned_records INTEGER NOT NULL,
                candidate_records INTEGER NOT NULL,updated_at TEXT NOT NULL)""",
        ]
        for statement in statements:
            conn.execute(statement)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _coordinates(value):
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(?:geo:)?\s*([+-]?[\d.]+)\s*°?\s*,\s*([+-]?[\d.]+)\s*°?\s*", value)
        if not match:
            return None
        latitude, longitude = map(_number, match.groups())
    elif isinstance(value, dict):
        latitude = longitude = None
        for lat_key, lon_key, divisor in (
            ("latitudeE7", "longitudeE7", 1e7), ("latE7", "lngE7", 1e7),
            ("latitude", "longitude", 1), ("lat", "lng", 1), ("lat", "lon", 1),
        ):
            if lat_key in value and lon_key in value:
                lat, lon = _number(value[lat_key]), _number(value[lon_key])
                if lat is not None and lon is not None:
                    latitude, longitude = lat / divisor, lon / divisor
                break
        if latitude is None and "latLng" in value:
            return _coordinates(value["latLng"])
        if latitude is None and "point" in value:
            return _coordinates(value["point"])
        if latitude is None and value.get("type") == "Point":
            coordinates = value.get("coordinates")
            if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
                longitude, latitude = _number(coordinates[0]), _number(coordinates[1])
    else:
        return None
    if (latitude is None or longitude is None or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _point_time(value, parent_time):
    if isinstance(value, dict):
        for key in ("timestamp", "time", "startTime"):
            timestamp = value.get(key)
            if isinstance(timestamp, str):
                try:
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "explicit_point_timestamp"
                except ValueError:
                    pass
        timestamp_ms = _number(value.get("timestampMs"))
        if timestamp_ms is not None:
            try:
                parsed = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
                return parsed.isoformat().replace("+00:00", "Z"), "explicit_point_timestamp"
            except (ValueError, OverflowError, OSError):
                pass
        offset = _number(value.get("durationMinutesOffsetFromStartTime"))
        if offset is not None and parent_time and "T" in parent_time:
            try:
                parsed = datetime.fromisoformat(parent_time.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return (parsed + timedelta(minutes=offset)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "explicit_offset_from_event_start"
            except (ValueError, OverflowError):
                pass
    return parent_time, "parent_event_start" if parent_time else "unknown"


def extract_points(metadata: dict, occurred_at=None, *, category="location", end_at=None) -> list[dict]:
    """Read recognized source coordinate structures, retaining their locators."""
    if not isinstance(metadata, dict):
        return []
    candidates = [("$", metadata, "observation")]
    single_paths = (
        ("location", "place"), ("startLocation", "activity_start"),
        ("endLocation", "activity_end"),
        ("visit.topCandidate.placeLocation", "place"),
        ("activity.start", "activity_start"), ("activity.end", "activity_end"),
        ("activity.startLocation", "activity_start"),
        ("activity.endLocation", "activity_end"),
        ("coordinates", "source_point"), ("venue", "venue"),
        ("event_data.venue", "venue"),
        ("enrichment.location", "reviewed_location"),
        ("reviewed_enrichment.location", "reviewed_location"),
    )
    for path, kind in single_paths:
        value = metadata
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            candidates.append(("$." + path, value, kind))
    for path in ("timelinePath", "waypointPath.waypoints", "simplifiedRawPath.points",
                 "rawPath.points", "transitPath.transitStops"):
        value = metadata
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, list):
            candidates.extend((f"$.{path}[{index}]", point, "path_point")
                              for index, point in enumerate(value))
    result = []
    for locator, value, kind in candidates:
        coordinates = _coordinates(value)
        if coordinates is None:
            continue
        parent_time = end_at if kind == "activity_end" and end_at else occurred_at
        timestamp, basis = _point_time(value, parent_time)
        if kind == "activity_end" and end_at and basis == "parent_event_start":
            basis = "parent_event_end"
        extra = {"coordinate_basis": "explicit_source_coordinate", "time_basis": basis}
        if kind == "reviewed_location":
            enrichment = metadata.get("reviewed_enrichment" if locator.startswith("$.reviewed_enrichment") else "enrichment")
            if isinstance(enrichment,dict) and "provenance" in enrichment:
                extra["enrichment_provenance"] = enrichment["provenance"]
        if isinstance(value, dict):
            for key in ("accuracy", "accuracyMeters", "placeId", "placeID", "name"):
                if key in value:
                    extra[key] = value[key]
        result.append({"latitude": coordinates[0], "longitude": coordinates[1],
                       "occurred_at": timestamp, "kind": kind, "locator": locator,
                       "metadata": extra})
    return result


def _scope(source_id):
    return 'source:'+source_id if source_id else '*'


def _location_busy(error):
    code=getattr(error,'sqlite_errorcode',None)
    return isinstance(error,sqlite3.OperationalError) and (code&255 in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED}
        if isinstance(code,int) else str(error).startswith(('database is locked','database table is locked','database schema is locked')))


@contextmanager
def _location_writer(path):
    owned=not isinstance(path,sqlite3.Connection);conn=connect(path) if owned else path
    saved=False
    try:
        if owned:conn.execute('PRAGMA busy_timeout=250')
        if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
        conn.execute('SAVEPOINT location_batch');saved=True
        yield conn
        conn.execute('RELEASE location_batch');saved=False
        if owned:conn.commit()
    except BaseException:
        if saved:
            conn.execute('ROLLBACK TO location_batch');conn.execute('RELEASE location_batch')
        if owned:conn.rollback()
        raise
    finally:
        if owned:conn.close()


def _discovery_status(conn,source_id):
    head=conn.execute('SELECT rowid,id FROM events ORDER BY rowid DESC LIMIT 1').fetchone()
    current=head[0] if head else 0
    exists=conn.execute("SELECT 1 FROM sqlite_schema WHERE name='location_discovery_state' AND type='table'").fetchone()
    row=conn.execute('SELECT * FROM location_discovery_state WHERE scope=?',(_scope(source_id),)).fetchone() if exists else None
    result={'status':'not_started','version':INDEX_VERSION,'source_id':source_id,'watermark_rowid':0,
        'checkpoint_rowid':0,'scanned_records':0,'candidate_records':0,'current_rowid':current,'has_more':current>0}
    if row:
        result.update(dict(row));anchor=conn.execute('SELECT id FROM events WHERE rowid=?',(row['watermark_rowid'],)).fetchone() if row['watermark_rowid'] else None
        invalid=row['version']!=INDEX_VERSION or bool(row['watermark_rowid'] and (not anchor or anchor[0]!=row['watermark_event_id'])) or current<row['checkpoint_rowid']
        result['status']='rebuild_required' if invalid else 'complete' if row['checkpoint_rowid']>=row['watermark_rowid'] else 'paused'
        result['has_more']=invalid or row['checkpoint_rowid']<row['watermark_rowid'] or current>row['watermark_rowid']
    result['append_backlog_rowid_span']=max(0,current-result['watermark_rowid'])
    result['limitations']=['Discovery covers inserts through its checkpoint; refresh touched IDs or reset after old-row edits, full reindex or VACUUM. Unindexed source media/history is outside this derived point index.']
    return result


@contextmanager
def _location_reader(path):
    if isinstance(path,sqlite3.Connection):
        yield path
        return
    conn=sqlite3.connect(Path(path or db.settings.database).resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)
    try:
        conn.row_factory=sqlite3.Row;conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        yield conn
    finally:conn.close()


def location_discovery_status(path=None,*,source_id=None):
    if not isinstance(path,sqlite3.Connection) and not Path(path or db.settings.database).exists():
        return {'status':'not_started','source_id':source_id,'watermark_rowid':0,'checkpoint_rowid':0,
                'scanned_records':0,'candidate_records':0,'has_more':False}
    with _location_reader(path) as conn:return _discovery_status(conn,source_id)


def _discover_location_rows(path,checkpoint,watermark,batch_size,max_batch_bytes):
    # Physical LIMIT precedes source filtering. The rowid range is the driving
    # access path, never source-time/category indexes or a full JSON predicate.
    with _location_reader(path) as conn:
        cursor=conn.execute('SELECT e.rowid AS catalog_rowid,e.id,e.source_id,e.metadata,e.occurred_at,e.end_at,e.category\n            FROM events e NOT INDEXED WHERE e.rowid>? AND e.rowid<=? ORDER BY e.rowid LIMIT ?',
            (checkpoint,watermark,batch_size))
        rows=[];size=0
        try:
            for row in cursor:
                added=len((row['metadata'] or '').encode())
                if rows and size+added>max_batch_bytes:break
                rows.append(dict(row));size+=added
        finally:cursor.close()
    return rows


_COORDINATE_ROOTS={'latitudeE7','latE7','latitude','lat','latLng','point','type','location','startLocation','endLocation',
    'visit','activity','coordinates','venue','event_data','enrichment','reviewed_enrichment',
    'timelinePath','waypointPath','simplifiedRawPath','rawPath','transitPath'}
_COORDINATE_TEXT=re.compile('|'.join(re.escape('"'+key+'"') for key in sorted(_COORDINATE_ROOTS)))


def _prepare_location(row):
    raw=row['metadata'] or '{}'
    # Escaped JSON keys must still be decoded. Ordinary non-coordinate records
    # avoid JSON parsing, while literal/nested nulls never become candidates.
    if isinstance(raw,str) and not _COORDINATE_TEXT.search(raw) and '\\' not in raw:
        return []
    try:metadata=json.loads(raw) if isinstance(raw,str) else raw
    except (ValueError,TypeError):return []
    if not isinstance(metadata,dict) or not any(metadata.get(key) is not None for key in _COORDINATE_ROOTS):return []
    return extract_points(metadata,row['occurred_at'],category=row['category'],end_at=row['end_at'])


def _current_location_rows(conn,values,*,rowids=False):
    result=[]
    for start in range(0,len(values),800):
        batch=values[start:start+800]
        access='NOT INDEXED' if rowids else 'INDEXED BY sqlite_autoindex_events_1'
        column='rowid' if rowids else 'id'
        result.extend(dict(row) for row in conn.execute('SELECT e.rowid AS catalog_rowid,e.id,e.source_id,e.metadata,e.occurred_at,e.end_at,e.category FROM events e '+access+
            ' WHERE e.'+column+' IN ('+','.join('?' for _ in batch)+')',batch))
    return sorted(result,key=lambda row:row['catalog_rowid'])


def _write_location_rows(conn,rows,*,source_id=None,prepared=None,force=False):
    events=points=0;prepared=prepared or {}
    states={}
    for start in range(0,len(rows),800):
        ids=[row['id'] for row in rows[start:start+800]]
        if ids:states.update({state['event_id']:state for state in conn.execute('SELECT * FROM location_index_state WHERE event_id IN ('+','.join('?' for _ in ids)+')',ids)})
    for row in rows:
        if source_id and row['source_id']!=source_id:continue
        prior=states.get(row['id']);raw=row['metadata'] or '{}'
        cached=prepared.get(row['catalog_rowid']);extracted=cached[1] if cached and cached[0]==row else _prepare_location(row)
        candidate=row['category'] in LOCATION_CATEGORIES or bool(extracted)
        if not candidate and prior is None:
            if force:conn.execute('DELETE FROM event_locations WHERE event_id=?',(row['id'],))
            continue
        digest=hashlib.sha256(json.dumps([raw,row['occurred_at'],row['end_at'],row['category']],separators=(',',':')).encode()).hexdigest()
        if not force and prior and prior['version']==INDEX_VERSION and prior['metadata_sha256']==digest:continue
        conn.execute('DELETE FROM event_locations WHERE event_id=?',(row['id'],))
        if not candidate:
            conn.execute('DELETE FROM location_index_state WHERE event_id=?',(row['id'],))
            events+=1
            continue
        inserts=[]
        for index,point in enumerate(extracted):
            point_id=hashlib.sha256(f"{row['id']}:{point['locator']}".encode()).hexdigest()
            inserts.append((point_id,row['id'],index,point['latitude'],point['longitude'],point['occurred_at'],point['kind'],point['locator'],json.dumps(point['metadata'],ensure_ascii=True)))
        conn.executemany('INSERT INTO event_locations(id,event_id,point_index,latitude,longitude,occurred_at,kind,locator,metadata) VALUES(?,?,?,?,?,?,?,?,?)',inserts)
        conn.execute('INSERT INTO location_index_state(event_id,version,metadata_sha256,point_count) VALUES(?,?,?,?)\n            ON CONFLICT(event_id) DO UPDATE SET version=excluded.version,metadata_sha256=excluded.metadata_sha256,point_count=excluded.point_count',(row['id'],INDEX_VERSION,digest,len(extracted)))
        events+=1;points+=len(extracted)
    return {'events':events,'points':points}


def reconcile_locations(path=None, *, event_ids=None, source_id=None, limit=None,refresh=False,
                        batch_size=500,max_batches=1,max_seconds=2,max_batch_bytes=8*1024**2) -> dict:
    """Commit bounded location batches; source discovery resumes from a durable cut.

    Owned-path discovery closes its read connection before parsing and before a
    short writer transaction rechecks every scanned row. Explicit IDs refresh
    their whole bounded input and preserve caller-owned transaction control.
    """
    if event_ids is not None:
        selected=list(dict.fromkeys(event_ids));selected=selected[:max(0,int(limit))] if limit is not None else selected
        total={'events':0,'points':0}
        for start in range(0,len(selected),800):
            with _location_writer(path) as conn:
                ensure_schema(conn)
                result=_write_location_rows(conn,_current_location_rows(conn,selected[start:start+800]),source_id=source_id,force=True)
                for key in total:total[key]+=result[key]
        return total
    batch_size=max(1,min(10000,int(batch_size)));max_batches=max(0,int(max_batches));max_batch_bytes=max(1,int(max_batch_bytes))
    if limit is not None:batch_size=min(batch_size,max(1,int(limit)));max_batches=min(max_batches,1) if int(limit)>0 else 0
    deadline=time.monotonic()+max(0,float(max_seconds));total={'events':0,'points':0,'batches':0,'scanned_in_run':0,'paused':None,'reset_applied':False}
    try:
        with _location_writer(path) as conn:
            ensure_schema(conn)
            if refresh:conn.execute('DELETE FROM location_discovery_state WHERE scope=?',(_scope(source_id),))
            state=_discovery_status(conn,source_id)
            if state['status']=='rebuild_required':raise ValueError('Location discovery requires refresh after version or rowid change')
            if state['status']=='not_started' or state['status']=='complete' and state['append_backlog_rowid_span']:
                head=conn.execute('SELECT rowid,id FROM events ORDER BY rowid DESC LIMIT 1').fetchone()
                conn.execute('INSERT OR REPLACE INTO location_discovery_state VALUES(?,?,?,?,?,?,?,?,?)',
                    (_scope(source_id),source_id,INDEX_VERSION,head[0] if head else 0,head[1] if head else None,state['checkpoint_rowid'],state['scanned_records'],state['candidate_records'],datetime.now(timezone.utc).isoformat()))
        total['reset_applied']=bool(refresh)
        while total['batches']<max_batches and time.monotonic()<deadline:
            state=location_discovery_status(path,source_id=source_id)
            if state['checkpoint_rowid']>=state['watermark_rowid']:break
            rows=_discover_location_rows(path,state['checkpoint_rowid'],state['watermark_rowid'],batch_size,max_batch_bytes)
            prepared={row['catalog_rowid']:(row,_prepare_location(row)) for row in rows if not source_id or row['source_id']==source_id}
            with _location_writer(path) as conn:
                current=_discovery_status(conn,source_id)
                if current['status']=='rebuild_required':raise ValueError('Location discovery requires refresh after version or rowid change')
                if (current['checkpoint_rowid'],current['watermark_rowid'])!=(state['checkpoint_rowid'],state['watermark_rowid']):
                    total['paused']='discovery_advanced_by_other_worker';break
                # Re-read ALL scanned physical rowids, including noncandidates
                # which another writer may have given coordinates meanwhile.
                fresh=_current_location_rows(conn,[row['catalog_rowid'] for row in rows],rowids=True)
                result=_write_location_rows(conn,fresh,source_id=source_id,prepared=prepared)
                checkpoint=rows[-1]['catalog_rowid'] if rows else state['watermark_rowid']
                conn.execute('UPDATE location_discovery_state SET checkpoint_rowid=?,scanned_records=scanned_records+?,candidate_records=candidate_records+?,updated_at=? WHERE scope=?',
                    (checkpoint,len(rows),result['events'],datetime.now(timezone.utc).isoformat(),_scope(source_id)))
            for key in ('events','points'):total[key]+=result[key]
            total['batches']+=1;total['scanned_in_run']+=len(rows)
    except sqlite3.OperationalError as error:
        if not _location_busy(error):raise
        total['paused']='catalog_busy'
    discovery=location_discovery_status(path,source_id=source_id)
    return {**total,'has_more':discovery['has_more'],'discovery':discovery}


def drain_locations(path=None,*,source_id=None,refresh=False,batch_size=5000,progress=None,busy_timeout_seconds=60):
    """Import completion drains bounded commits, yielding to concurrent writers."""
    total={'events':0,'points':0,'scanned_in_run':0};busy=0;busy_started=None;target=None
    while True:
        result=reconcile_locations(path,source_id=source_id,refresh=refresh,batch_size=batch_size)
        reset_pending=refresh and not result['reset_applied']
        if result['reset_applied']:refresh=False
        if target is None and not reset_pending and result['discovery'].get('status')!='not_started':target=result['discovery'].get('watermark_rowid')
        for key in total:total[key]+=result[key]
        if progress:progress(result)
        if result['paused']=='catalog_busy':
            busy+=1
            if busy_started is None:busy_started=time.monotonic()
            if time.monotonic()-busy_started>=busy_timeout_seconds:
                from .storage import ImportPaused
                raise ImportPaused('location_catalog_busy',discovery=result['discovery'])
            time.sleep(min(.05*2**busy,.5));continue
        busy=0;busy_started=None
        if not result['has_more'] or target is not None and result['discovery']['checkpoint_rowid']>=target:
            return {**result,**total,'drained_watermark_rowid':target}
        time.sleep(.01)


def map_points(path=None, *, q="", source="", category="", start="", end="",
               undated=False, limit=2000, offset=0, bounds=None, direction='desc') -> dict:
    """Return indexed explicit points under the timeline's event filters."""
    if direction not in {'asc','desc'}:raise ValueError('Invalid map sort direction')
    order=direction.upper()
    with _connection(path) as conn:
        ensure_schema(conn)
        where, args = event_filters(q,source,category,start,end,undated)
        if bounds is not None:
            west,south,east,north = map(float,bounds)
            if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= north <= 90):
                raise ValueError("Invalid map bounds")
            where += (" AND " if where else " WHERE ") + "p.latitude BETWEEN ? AND ? AND "
            args.extend([south,north])
            if west <= east:
                where += "p.longitude BETWEEN ? AND ?"
            else:
                where += "(p.longitude>=? OR p.longitude<=?)"
            args.extend([west,east])
        joins = " FROM event_locations p JOIN events e ON e.id=p.event_id JOIN sources s ON s.id=e.source_id"
        total = conn.execute("SELECT count(*)" + joins + where,args).fetchone()[0]
        limit,offset = min(max(int(limit),1),10000),max(int(offset),0)
        rows = conn.execute("""SELECT p.id,p.event_id,p.latitude,p.longitude,p.occurred_at,
            p.kind,p.locator,p.metadata,e.title,e.category,e.time_precision,e.source_id,s.name AS source""" + joins + where +
            f" ORDER BY p.occurred_at {order} NULLS LAST,p.id {order} LIMIT ? OFFSET ?",
            args + [limit,offset]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            item["event_time_precision"] = item["time_precision"]
            if item["metadata"].get("time_basis") in {"explicit_point_timestamp","explicit_offset_from_event_start"}:
                item["time_precision"] = "second"
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}


__all__ = ["ensure_schema", "extract_points", "reconcile_locations", "drain_locations", "location_discovery_status", "map_points"]
