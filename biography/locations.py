"""Rebuildable indexes of coordinates explicitly present in source records.

No geocoding, interpolated paths, or inferred connections are created here.
Each point retains its source metadata locator and time basis.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import sqlite3

from .db import connect
from .queries import event_filters

INDEX_VERSION = 1
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


def reconcile_locations(path=None, *, event_ids=None, source_id=None, limit=None,
                        refresh=False) -> dict:
    """Index new location events; explicit IDs also refresh existing points.

    Run after imports or at startup, not for each map request. An explicit ID
    batch keeps ongoing import work proportional to the batch size.
    """
    with _connection(path) as conn:
        ensure_schema(conn)
        # JSON predicates select only explicit coordinate holders outside known
        # map categories. Names, text, and arbitrary candidate arrays never count.
        coordinate_paths = ("latitudeE7","latitude","lat","location","coordinates",
                            "venue","event_data.venue","enrichment.location","reviewed_enrichment.location")
        explicit = " OR ".join("json_type(e.metadata,'$." + value + "') IS NOT NULL" for value in coordinate_paths)
        clauses = ["(e.category IN ('location','place_visit','travel') OR CASE WHEN json_valid(e.metadata) THEN (" + explicit + ") ELSE 0 END)"]
        args = []
        if event_ids is not None:
            event_ids = list(dict.fromkeys(event_ids))
            if not event_ids:
                return {"events": 0, "points": 0}
            if len(event_ids) > 800:
                totals = {"events": 0, "points": 0}
                for start in range(0, len(event_ids), 800):
                    batch = reconcile_locations(conn, event_ids=event_ids[start:start + 800], source_id=source_id)
                    for key in totals:
                        totals[key] += batch[key]
                return totals
            clauses.append("e.id IN (" + ",".join("?" for _ in event_ids) + ")")
            args.extend(event_ids)
        elif not refresh:
            clauses.append("(st.event_id IS NULL OR st.version<>?)")
            args.append(INDEX_VERSION)
        if source_id:
            clauses.append("e.source_id=?")
            args.append(source_id)
        sql = ("SELECT e.id,e.metadata,e.occurred_at,e.end_at,e.category FROM events e "
               "LEFT JOIN location_index_state st ON st.event_id=e.id WHERE " + " AND ".join(clauses) + " ORDER BY e.rowid")
        if limit is not None:
            sql += " LIMIT ?"
            args.append(max(0, int(limit)))
        events = points = 0
        for row in conn.execute(sql, args):
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (ValueError, TypeError):
                metadata = {}
            extracted = extract_points(metadata, row["occurred_at"], category=row["category"],end_at=row["end_at"])
            conn.execute("DELETE FROM event_locations WHERE event_id=?", (row["id"],))
            for index, point in enumerate(extracted):
                point_id = hashlib.sha256(f"{row['id']}:{point['locator']}".encode()).hexdigest()
                conn.execute("""INSERT INTO event_locations
                    (id,event_id,point_index,latitude,longitude,occurred_at,kind,locator,metadata)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (point_id,row["id"],index,
                    point["latitude"],point["longitude"],point["occurred_at"],point["kind"],
                    point["locator"],json.dumps(point["metadata"],ensure_ascii=True)))
            digest = hashlib.sha256((row["metadata"] or "{}").encode()).hexdigest()
            conn.execute("""INSERT INTO location_index_state(event_id,version,metadata_sha256,point_count)
                VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET version=excluded.version,
                metadata_sha256=excluded.metadata_sha256,point_count=excluded.point_count""",
                (row["id"],INDEX_VERSION,digest,len(extracted)))
            events += 1
            points += len(extracted)
        return {"events": events, "points": points}


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


__all__ = ["ensure_schema", "extract_points", "reconcile_locations", "map_points"]
