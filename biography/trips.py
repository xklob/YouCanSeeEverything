"""Tentative straight-line connections between adjacent recorded stops.

These are explicitly labelled inferences, never recorded road routes. The full
source/account chronology is examined before display filters or pagination.
Missing, ambiguous, overlapping, or implausible records form chain barriers.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from .config import settings
from .queries import event_filters

PRECISE_TIMES = {"minute","second","millisecond","microsecond","nanosecond"}
MAX_STOPS_PER_SOURCE = 250_000
MAX_GAPS = 100


@contextmanager
def _read_connection(path):
    """A WAL reader cannot create schema or write to an ongoing import."""
    conn = sqlite3.connect(Path(path or settings.database).resolve().as_uri()+"?mode=ro",uri=True,timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _instant(value):
    if not isinstance(value,str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z","+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError,OverflowError):
        return None


def _iso(value):
    return value.isoformat().replace("+00:00","Z") if value is not None else None


def _bound(value, *, upper=False):
    if not value:
        return None,False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        parsed = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return (parsed+timedelta(days=1),True) if upper else (parsed,False)
    parsed = _instant(value)
    if parsed is None:
        raise ValueError("Trip date filters require an ISO date or timezone-aware datetime")
    return parsed,False


def _coarse_interval(value,precision=None):
    if not isinstance(value,str):
        return None
    match = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?",value)
    if not match:
        return None
    year,month,day = (int(piece) if piece else None for piece in match.groups())
    if precision == "year":
        month,day = None,None
    elif precision == "month":
        day = None
    try:
        start = datetime(year,month or 1,day or 1,tzinfo=timezone.utc)
        if day:
            end = start+timedelta(days=1)
        elif month:
            end = datetime(year+int(month==12),1 if month==12 else month+1,1,tzinfo=timezone.utc)
        else:
            end = datetime(year+1,1,1,tzinfo=timezone.utc)
        return start,end
    except (ValueError,OverflowError):
        return None


def distance_km(latitude_a,longitude_a,latitude_b,longitude_b):
    """Great-circle lower bound, with the short wrap across the dateline."""
    a,b = math.radians(latitude_a),math.radians(latitude_b)
    delta_a = b-a
    delta_b = math.radians((longitude_b-longitude_a+180)%360-180)
    value = math.sin(delta_a/2)**2 + math.cos(a)*math.cos(b)*math.sin(delta_b/2)**2
    return 6371.0088*2*math.asin(math.sqrt(min(1,max(0,value))))


def _number(value):
    if isinstance(value,bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError,OverflowError):
        return None


def _stop(rows,mode):
    first = rows[0]
    result = {"event_ids":[first["event_id"]],"location_ids":[],"source_id":first["source_id"],
              "source":first["source"],"account_key":first["account_key"],"title":first["title"],
              "raw_start":first["event_start"],"raw_end":first["event_end"],
              "time_precision":first["time_precision"],"start":_instant(first["event_start"]),
              "end":_instant(first["event_end"]),"latitude":None,"longitude":None,
              "locator":None,"kind":mode,"reason":None,"barrier":None}
    if result["time_precision"] not in PRECISE_TIMES or result["start"] is None:
        result["reason"] = "unknown_or_imprecise_time"
        result["barrier"] = _coarse_interval(first["event_start"],result["time_precision"])
    if mode == "place_visit" and result["end"] is None:
        result["reason"] = result["reason"] or "missing_visit_end"
    elif mode == "location":
        result["end"] = result["start"]
    if result["start"] and result["end"] and result["end"]<result["start"]:
        result["reason"] = "reversed_visit_interval"
    points = [row for row in rows if row["location_id"] is not None]
    places = [row for row in points if row["point_kind"] == "place"]
    points = places or points
    if not points:
        result["reason"] = result["reason"] or "missing_explicit_coordinates"
        return result
    coordinates = set()
    for point in points:
        lat,lon = _number(point["latitude"]),_number(point["longitude"])
        if lat is None or lon is None or not -90<=lat<=90 or not -180<=lon<=180:
            result["reason"] = result["reason"] or "invalid_coordinates"
            continue
        coordinates.add((lat,lon))
    if len(coordinates)!=1:
        result["reason"] = result["reason"] or "ambiguous_coordinates"
        return result
    result["latitude"],result["longitude"] = next(iter(coordinates))
    result["location_ids"] = [point["location_id"] for point in points]
    result["locator"] = points[0]["coordinate_locator"]
    if mode == "location":
        try:
            metadata = json.loads(points[0]["point_metadata"] or "{}")
        except (ValueError,TypeError):
            metadata = {}
        accuracy = _number(metadata.get("accuracyMeters",metadata.get("accuracy")))
        if accuracy is None or not 0<=accuracy<=100:
            result["reason"] = result["reason"] or "weak_observation_accuracy"
    return result


def _endpoint(stop):
    return {"id":stop["location_ids"][0],"location_id":stop["location_ids"][0],
            "location_ids":list(stop["location_ids"]),"event_id":stop["event_ids"][0],
            "event_ids":list(stop["event_ids"]),"title":stop["title"],
            "latitude":stop["latitude"],"longitude":stop["longitude"],
            "occurred_at":_iso(stop["start"]),"end_at":_iso(stop["end"]),
            "time_precision":stop["time_precision"],"source":stop["source"],
            "source_id":stop["source_id"],"account_key":stop["account_key"],
            "coordinate_locator":stop["locator"],"coordinate_basis":"explicit_source_coordinate",
            "time_basis":"recorded_visit_interval" if stop["kind"]=="place_visit" else "recorded_observation"}


def trip_connections(path=None, *, q="",source="",category="",start="",end="",undated=False,
                     limit=2000,offset=0,max_gap_hours=72,min_distance_km=1,
                     long_distance_km=100,max_speed_kmh=1200,
                     summaries_only=False,group_id=None):
    """Infer only bounded connections between adjacent reliable recorded stops.

    Visit sources take priority. A source with no visits may use explicitly
    timed observations whose reported accuracy is at most 100 metres. Full
    source chronologies exceeding the safety bound are omitted, never sampled.
    Search/category/date filters select established legs, never manufacture new
    adjacency. A 24-hour recorded dwell begins a new inferred trip grouping.
    Summary-only queries return every selected group from that same full pass;
    group selection applies after adjacency and before leg pagination.
    """
    lower,_ = _bound(start)
    upper,upper_exclusive = _bound(end,upper=True)
    if lower and upper and (lower>=upper if upper_exclusive else lower>upper):
        raise ValueError("Trip date range is reversed")
    parameters = {"max_gap_hours":float(max_gap_hours),"min_distance_km":float(min_distance_km),
                  "long_distance_km":float(long_distance_km),"max_speed_kmh":float(max_speed_kmh),
                  "max_recorded_dwell_hours":24,"observation_accuracy_metres":100,
                  "max_stops_per_source":MAX_STOPS_PER_SOURCE}
    if (not all(math.isfinite(value) for value in parameters.values())
            or not 0<parameters["max_gap_hours"]<=168 or not 0<parameters["min_distance_km"]<=1000
            or parameters["long_distance_km"]<parameters["min_distance_km"]
            or not 0<parameters["max_speed_kmh"]<=2000):
        raise ValueError("Invalid trip inference parameters")
    limit,offset = min(max(int(limit),1),10000),max(int(offset),0)
    response = {"items":[],"total":0,"limit":limit,"offset":offset,"truncated":False,
                "gaps":[],"gaps_total":0,"gaps_truncated":False,"trips":[],"trips_total":0,
                "coverage_complete":True,"coverage":[],"parameters":parameters,
                "inferred":True,"geometry_type":"straight_line",
                "notice":"Dotted connections are inferred movement between recorded stops, not observed road routes.",
                "gap_scope":"complete source/account chronology within the selected date window"}
    def in_window(value):
        return (value is not None and (lower is None or value>=lower)
                and (upper is None or value<upper if upper_exclusive else upper is None or value<=upper))
    def gap_visible(a,b):
        values = [value for value in (a,b) if value is not None]
        if not values:
            return True
        lo,hi = min(values),max(values)
        return (lower is None or hi>=lower) and (upper is None or lo<upper if upper_exclusive else upper is None or lo<=upper)
    def gap(reason,previous,current,*,a=None,b=None,extra=None):
        a = a if a is not None else previous["end"] if previous else None
        b = b if b is not None else current["start"] if current else None
        if not gap_visible(a,b):
            return
        item = {"reason":reason,"source_id":(current or previous)["source_id"],
                "source":(current or previous)["source"],"account_key":(current or previous)["account_key"],
                "from_event_id":previous["event_ids"][0] if previous else None,
                "to_event_id":current["event_ids"][0] if current else None,
                "from_at":_iso(a),"to_at":_iso(b),"label":reason.replace("_"," ").capitalize()}
        if a is not None and b is not None:
            item["gap_hours"] = round((b-a).total_seconds()/3600,6)
        if extra:
            item.update(extra)
        response["gaps_total"] += 1
        if len(response["gaps"])<MAX_GAPS:
            response["gaps"].append(item)
    with _read_connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_locations'").fetchone():
            response["coverage_complete"] = False
            response["coverage"] = [{"status":"location_index_unavailable"}]
            return response
        where,args = event_filters(q,source,category)
        matching = None
        if q or category or undated:
            matching = set() if undated else {row[0] for row in conn.execute("SELECT e.id FROM events e"+where,args)}
        groups = conn.execute("""SELECT s.id AS source_id,s.name AS source,s.account_key,
            sum(e.category='place_visit') AS visits,sum(e.category='location') AS observations
            FROM events e JOIN sources s ON s.id=e.source_id
            WHERE e.category IN ('place_visit','location')""" + (" AND s.id=?" if source else "") +
            " GROUP BY s.id,s.account_key ORDER BY s.id",[source] if source else []).fetchall()
        summaries = {}
        selected_trip_ids = set()
        for group in groups:
            mode = "place_visit" if group["visits"] else "location"
            count = group["visits"] or group["observations"]
            coverage = {"source_id":group["source_id"],"source":group["source"],"account_key":group["account_key"],
                        "mode":mode,"recorded_stops":count,"status":"complete","duplicate_stops":0,"invalid_stops":0}
            response["coverage"].append(coverage)
            if count>MAX_STOPS_PER_SOURCE:
                response["coverage_complete"] = False
                coverage["status"] = "complete_chronology_exceeds_safety_bound"
                placeholder = {**dict(group),"event_ids":[None],"start":None,"end":None}
                gap("source_stop_limit",None,placeholder,extra={"recorded_stops":count})
                continue
            rows = conn.execute("""SELECT e.id AS event_id,e.source_id,e.title,e.occurred_at AS event_start,
                e.end_at AS event_end,e.time_precision,s.name AS source,s.account_key,
                p.id AS location_id,p.latitude,p.longitude,p.kind AS point_kind,p.locator AS coordinate_locator,
                p.metadata AS point_metadata FROM events e JOIN sources s ON s.id=e.source_id
                LEFT JOIN event_locations p ON p.event_id=e.id AND p.kind IN ('place','observation')
                WHERE e.source_id=? AND e.category=? ORDER BY e.id,p.point_index""",(group["source_id"],mode))
            stops = []
            batch = []
            for row in rows:
                if batch and row["event_id"]!=batch[0]["event_id"]:
                    stops.append(_stop(batch,mode))
                    batch = []
                batch.append(row)
            if batch:
                stops.append(_stop(batch,mode))
            stops.sort(key=lambda stop:(stop["start"] or (stop["barrier"][0] if stop["barrier"] else datetime.min.replace(tzinfo=timezone.utc)),stop["event_ids"][0]))
            merged = []
            for stop in stops:
                previous = merged[-1] if merged else None
                if (previous and not previous["reason"] and not stop["reason"]
                        and previous["start"]==stop["start"] and previous["end"]==stop["end"]
                        and previous["latitude"]==stop["latitude"] and previous["longitude"]==stop["longitude"]):
                    previous["event_ids"].extend(stop["event_ids"])
                    previous["location_ids"].extend(stop["location_ids"])
                    coverage["duplicate_stops"] += 1
                else:
                    merged.append(stop)
            barriers = [stop["barrier"] for stop in merged if stop["barrier"]]
            unplaced = [stop for stop in merged if stop["start"] is None and stop["barrier"] is None]
            if unplaced:
                # There is no defensible position for these records in source
                # chronology. Do not silently remove one and reconnect peers.
                coverage["status"] = "unplaced_time_barrier"
                coverage["invalid_stops"] = sum(bool(stop["reason"]) for stop in merged)
                response["coverage_complete"] = False
                for stop in unplaced:
                    gap("unplaced_time_prevents_source_chronology",None,stop)
                continue
            previous = None
            blocked_until = None
            trip_id = None
            for stop in merged:
                if stop["reason"]:
                    coverage["invalid_stops"] += 1
                    interval = stop["barrier"]
                    gap(stop["reason"],previous,stop,a=interval[0] if interval else None,b=interval[1] if interval else None)
                    # A known unresolved stay occupies its entire recorded
                    # interval, not just its start. Later nested visits cannot
                    # create a fresh connection inside that uncertainty.
                    active_ends = [value for value in (blocked_until,
                        stop["end"] if stop["start"] and stop["end"] and stop["end"]>=stop["start"] else None,
                        previous["end"] if previous and stop["start"] and previous["end"]>stop["start"] else None)
                        if value is not None]
                    if active_ends:
                        blocked_until = max(active_ends)
                    previous,trip_id = None,None
                    continue
                if blocked_until and stop["start"]<blocked_until:
                    gap("overlapping_intervals",None,stop,a=stop["start"],b=blocked_until)
                    blocked_until = max(blocked_until,stop["end"])
                    previous,trip_id = None,None
                    continue
                blocked_until = None
                if previous is None:
                    previous = stop
                    continue
                departure,arrival = previous["end"],stop["start"]
                hours = (arrival-departure).total_seconds()/3600
                distance = distance_km(previous["latitude"],previous["longitude"],stop["latitude"],stop["longitude"])
                if hours<0:
                    gap("overlapping_intervals",previous,stop,extra={"distance_km":round(distance,3)})
                    blocked_until = max(previous["end"],stop["end"])
                    previous,trip_id = None,None
                    continue
                if any(departure<end and arrival>=begin for begin,end in barriers):
                    gap("imprecise_stop_between",previous,stop)
                    previous,trip_id = stop,None
                    continue
                if hours>parameters["max_gap_hours"]:
                    gap("recording_gap_exceeds_bound",previous,stop,extra={"distance_km":round(distance,3)})
                    previous,trip_id = stop,None
                    continue
                if distance<parameters["min_distance_km"]:
                    if (previous["end"]-previous["start"]).total_seconds()>=24*3600:
                        trip_id = None
                    previous = stop
                    continue
                if hours<=0 or distance/hours>parameters["max_speed_kmh"]:
                    gap("zero_travel_window" if hours<=0 else "implausible_minimum_speed",previous,stop,
                        extra={"distance_km":round(distance,3),"minimum_speed_kmh":round(distance/hours,3) if hours else None})
                    previous,trip_id = None,None
                    continue
                if trip_id is None or (previous["end"]-previous["start"]).total_seconds()>=24*3600:
                    trip_id = hashlib.sha256(json.dumps([group["source_id"],group["account_key"],previous["event_ids"][0],_iso(departure)]).encode()).hexdigest()
                    summaries[trip_id] = {"id":trip_id,"source_id":group["source_id"],"source":group["source"],
                        "account_key":group["account_key"],"departure_at":_iso(departure),"arrival_at":_iso(arrival),
                        "legs":0,"distance_km":0,"max_leg_distance_km":0,"max_gap_hours":0,
                        "first_stop":_endpoint(previous),"last_stop":_endpoint(stop),
                        "_represented_events":set(),"inferred":True}
                summary = summaries[trip_id]
                summary["legs"] += 1
                summary["distance_km"] += distance
                summary["max_leg_distance_km"] = max(summary["max_leg_distance_km"],distance)
                summary["max_gap_hours"] = max(summary["max_gap_hours"],hours)
                summary["last_stop"] = _endpoint(stop)
                summary["_represented_events"].update((previous["event_ids"][0],stop["event_ids"][0]))
                summary["arrival_at"] = _iso(arrival)
                visible = (in_window(previous["start"]) and in_window(stop["start"])
                           and (group_id is None or trip_id==group_id)
                           and (matching is None or (any(event in matching for event in previous["event_ids"])
                                                     and any(event in matching for event in stop["event_ids"]))))
                if visible:
                    selected_trip_ids.add(trip_id)
                    index = response["total"]
                    response["total"] += 1
                    if not summaries_only and offset<=index<offset+limit:
                        wrapped_lon = previous["longitude"] + (stop["longitude"]-previous["longitude"]+180)%360-180
                        identity = hashlib.sha256(json.dumps([group["source_id"],group["account_key"],previous["event_ids"],stop["event_ids"]]).encode()).hexdigest()
                        response["items"].append({"id":identity,"trip_id":trip_id,"source_id":group["source_id"],
                            "source":group["source"],"account_key":group["account_key"],"from":_endpoint(previous),"to":_endpoint(stop),
                            "departure_at":_iso(departure),"arrival_at":_iso(arrival),"distance_km":round(distance,3),
                            "gap_hours":round(hours,6),"minimum_speed_kmh":round(distance/hours,3),"inferred":True,
                            "geometry_type":"straight_line","distance_basis":"great_circle_between_recorded_stops",
                            "display_path":[[previous["latitude"],previous["longitude"]],[stop["latitude"],wrapped_lon]],
                            "crosses_dateline":abs(stop["longitude"]-previous["longitude"])>180,
                            "label":f"Inferred connection · {distance:.0f} km · {hours:.1f} h between recorded stops"})
                previous = stop
        shown_trip_ids = selected_trip_ids if summaries_only else {item["trip_id"] for item in response["items"]}
        # Include an explicitly selected group's summary even on an empty page.
        if group_id in selected_trip_ids:
            shown_trip_ids.add(group_id)
        for identity in shown_trip_ids:
            summary = summaries[identity]
            summary["distance_km"] = round(summary["distance_km"],3)
            summary["max_leg_distance_km"] = round(summary["max_leg_distance_km"],3)
            summary["max_gap_hours"] = round(summary["max_gap_hours"],6)
            summary["represented_stops"] = len(summary.pop("_represented_events"))
            summary["summary_complete"] = True
            summary["long_distance"] = summary["max_leg_distance_km"]>=parameters["long_distance_km"]
            summary["kind"] = "long_distance" if summary["long_distance"] else "local_movement"
            response["trips"].append(summary)
        response["trips"].sort(key=lambda item:(item["departure_at"],item["id"]))
        for item in response["items"]:
            item["trip_kind"] = summaries[item["trip_id"]]["kind"]
        response["trips_total"] = len(selected_trip_ids)
        event_ids = {event_id for item in response["items"] for endpoint in (item["from"],item["to"]) for event_id in endpoint["event_ids"]}
        evidence = {event_id:[] for event_id in event_ids}
        identifiers = list(event_ids)
        for position in range(0,len(identifiers),800):
            batch = identifiers[position:position+800]
            rows = conn.execute("""SELECT v.event_id,v.file_id,f.original_path,v.locator,v.parser
                FROM evidence v JOIN files f ON f.id=v.file_id WHERE v.event_id IN ("""+
                ",".join("?" for _ in batch)+") ORDER BY v.event_id,f.original_path,v.locator",batch)
            for row in rows:
                evidence[row["event_id"]].append(dict(row))
        for item in response["items"]:
            for endpoint in (item["from"],item["to"]):
                entries = [entry for event_id in endpoint["event_ids"] for entry in evidence[event_id]]
                endpoint["provenance"] = entries[:20]
                endpoint["provenance_total"] = len(entries)
    response["truncated"] = response["total"]>offset+len(response["items"]) or not response["coverage_complete"]
    response["gaps_truncated"] = response["gaps_total"]>len(response["gaps"])
    return response


__all__ = ["trip_connections","distance_km"]
