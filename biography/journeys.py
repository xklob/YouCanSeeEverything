"""Read-only grouped exploration of existing, conservatively inferred legs."""

from .trips import _bound, _instant, trip_connections


NOTICE = ("Trips group inferred straight-line connections between recorded stops. "
          "Distances are sums of those connections, not measured travel routes. "
          "Recording gaps and uncertain stops break the grouping.")


def list_journeys(path=None, *, source="", start="", end="", kind="all", limit=20, offset=0):
    if kind not in ("all", "long_distance", "local_movement"):
        raise ValueError("Unknown journey kind")
    lower, _ = _bound(start)
    upper, exclusive = _bound(end, upper=True)
    if lower and upper and (lower >= upper if exclusive else lower > upper):
        raise ValueError("Journey date range is reversed")
    limit, offset = min(max(int(limit), 1), 100), max(int(offset), 0)
    result = trip_connections(path, source=source, summaries_only=True)
    groups = []
    for group in result["trips"]:
        departure, arrival = _instant(group["departure_at"]), _instant(group["arrival_at"])
        if lower and arrival < lower:
            continue
        if upper and (departure >= upper if exclusive else departure > upper):
            continue
        if kind != "all" and group["kind"] != kind:
            continue
        groups.append(group)
    groups.sort(key=lambda item: (item["departure_at"], item["id"]), reverse=True)
    items = groups[offset:offset + limit]
    return {"items": items, "total": len(groups), "limit": limit, "offset": offset,
            "truncated": offset + len(items) < len(groups),
            "coverage_complete": result["coverage_complete"], "coverage": result["coverage"],
            "notice": NOTICE, "date_filter_basis": "whole_group_intersects_selected_window",
            "sources": [{"id": entry["source_id"], "name": entry["source"]}
                        for entry in result["coverage"] if "source_id" in entry]}


def journey_detail(journey_id, path=None, *, limit=100, offset=0):
    limit, offset = min(max(int(limit), 1), 500), max(int(offset), 0)
    result = trip_connections(path, group_id=journey_id, limit=limit, offset=offset)
    journey = next((item for item in result["trips"] if item["id"] == journey_id), None)
    if journey is None:
        return None
    return {"journey": journey, "items": result["items"], "total": result["total"],
            "limit": limit, "offset": offset,
            "truncated": offset + len(result["items"]) < result["total"],
            "complete": offset == 0 and len(result["items"]) == result["total"],
            "coverage_complete": result["coverage_complete"], "coverage": result["coverage"],
            "notice": NOTICE,
            "represented_stops_note": "Stops represented by accepted connections; short movements may be omitted."}
