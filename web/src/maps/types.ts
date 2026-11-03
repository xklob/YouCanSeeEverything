import type { MapLocation } from "../api";

export interface TripStop {
  id?: string;
  location_id?: string;
  event_id: string;
  event_ids?: string[];
  latitude: number;
  longitude: number;
  occurred_at: string | null;
  end_at?: string | null;
  time_precision?: string;
  title: string;
  source?: string;
}
export interface TripConnection {
  id: string;
  trip_id?: string;
  source: string;
  source_id?: string;
  from: TripStop;
  to: TripStop;
  distance_km: number;
  gap_hours: number;
  departure_at?: string;
  arrival_at?: string;
  inferred: true;
  geometry_type: "straight_line";
  label?: string;
  display_path?: [[number, number], [number, number]];
}
export interface TripsPage {
  items: TripConnection[];
  total: number;
  truncated?: boolean;
  gaps?: Record<string, unknown>[];
  gaps_total?: number;
  gaps_truncated?: boolean;
  coverage_complete?: boolean;
}
export interface MapConfig {
  provider: "openstreetmap" | "google";
  google_maps_configured: boolean;
  google_maps_api_key?: string;
}

export function tripDisplayPath(
  trip: TripConnection,
): [[number, number], [number, number]] {
  const path = trip.display_path;
  return path?.length === 2 &&
    path.every(
      (point) =>
        Array.isArray(point) &&
        point.length === 2 &&
        point.every(Number.isFinite) &&
        Math.abs(point[0]) <= 90 &&
        Math.abs(point[1]) <= 540,
    )
    ? path
    : [
        [trip.from.latitude, trip.from.longitude],
        [trip.to.latitude, trip.to.longitude],
      ];
}
export function googleTripBounds(
  trip: TripConnection,
): google.maps.LatLngBoundsLiteral {
  const path = tripDisplayPath(trip);
  const wrap = (lng: number) => ((((lng + 180) % 360) + 360) % 360) - 180;
  return {
    north: Math.max(path[0][0], path[1][0]),
    south: Math.min(path[0][0], path[1][0]),
    west: wrap(Math.min(path[0][1], path[1][1])),
    east: wrap(Math.max(path[0][1], path[1][1])),
  };
}
export interface MapCanvasProps {
  locations: MapLocation[];
  selectedId: string;
  onSelect: (id: string) => void;
  tilesEnabled: boolean;
  fitRequest: number;
  trips: TripConnection[];
  selectedTripId: string;
  onSelectTrip: (id: string) => void;
}

export function validStop(stop: TripStop): boolean {
  return Boolean(
    stop &&
    stop.event_id &&
    Number.isFinite(stop.latitude) &&
    Number.isFinite(stop.longitude) &&
    Math.abs(stop.latitude) <= 90 &&
    Math.abs(stop.longitude) <= 180,
  );
}
export function tripPoint(stop: TripStop, source: string): MapLocation {
  return {
    id:
      stop.id ||
      stop.location_id ||
      `trip:${stop.event_id}:${stop.latitude}:${stop.longitude}:${stop.occurred_at}`,
    event_id: stop.event_id,
    latitude: stop.latitude,
    longitude: stop.longitude,
    occurred_at: stop.occurred_at,
    time_precision: stop.time_precision,
    title: stop.title,
    source: stop.source || source,
    kind: "recorded_stop",
  };
}
export function withTripEndpoints(
  locations: MapLocation[],
  trips: TripConnection[],
): MapLocation[] {
  const key = (point: MapLocation) =>
    `${point.event_id}:${point.latitude}:${point.longitude}:${point.occurred_at}`;
  const result = new Map(locations.map((point) => [key(point), point]));
  for (const trip of trips)
    for (const stop of [trip.from, trip.to]) {
      const point = tripPoint(stop, trip.source);
      if (!result.has(key(point))) result.set(key(point), point);
    }
  return [...result.values()];
}
