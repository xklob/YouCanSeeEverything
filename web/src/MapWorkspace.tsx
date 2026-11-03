import { useEffect, useMemo, useState } from "react";
import {
  ArrowUpRight,
  CalendarDays,
  CircleAlert,
  Compass,
  FileText,
  LoaderCircle,
  LocateFixed,
  MapPin,
  X,
} from "lucide-react";
import {
  api,
  count,
  formatDate,
  humanize,
  type LifeEvent,
  type MapLocation,
  type Page,
} from "./api";
import MediaGallery from "./MediaGallery";
import LeafletCanvas from "./maps/LeafletCanvas";
import GoogleCanvas from "./maps/GoogleCanvas";
import {
  validStop,
  withTripEndpoints,
  type TripConnection,
  type TripsPage,
  type MapConfig,
} from "./maps/types";

type MapPage = { items: MapLocation[]; total: number };
type MapFilters = {
  q: string;
  source: string;
  category: string;
  from: string;
  to: string;
  undated: boolean;
};

export function recordedDay(location: MapLocation): string | null {
  // Calendar precision cannot establish a day for records dated only to a month/year.
  if (["unknown", "year", "month"].includes(location.time_precision || ""))
    return null;
  return location.occurred_at?.match(/^\d{4}-\d{2}-\d{2}/)?.[0] ?? null;
}

function TripDetails({
  trip,
  onOpenEvent,
  close,
}: {
  trip: TripConnection;
  onOpenEvent: (id: string) => void;
  close: () => void;
}) {
  const gap = Number.isFinite(trip.gap_hours)
    ? trip.gap_hours >= 24
      ? `${(trip.gap_hours / 24).toLocaleString("en-US", { maximumFractionDigits: 1 })} ${trip.gap_hours === 24 ? "day" : "days"}`
      : `${trip.gap_hours.toLocaleString("en-US", { maximumFractionDigits: 1 })} ${trip.gap_hours === 1 ? "hour" : "hours"}`
    : "Unknown";
  return (
    <section
      className="trip-details"
      aria-label="Inferred trip connection details"
    >
      <div className="trip-detail-heading">
        <h3>
          <span className="legend-trip" />
          Inferred connection
        </h3>
        <button
          className="icon-button"
          onClick={close}
          aria-label="Close inferred connection"
        >
          <X size={15} />
        </button>
      </div>
      <p>
        A straight-line link between recorded stops. The route and mode of
        travel are not recorded.
      </p>
      <div className="trip-endpoints">
        {(
          [
            ["From recorded stop", trip.from, trip.departure_at],
            ["To recorded stop", trip.to, trip.arrival_at],
          ] as const
        ).map(([label, stop, at]) => (
          <div key={label}>
            <span>{label}</span>
            <h4>{stop.title || "Recorded stop"}</h4>
            <p>{formatDate(at || stop.occurred_at, stop.time_precision)}</p>
            <button onClick={() => onOpenEvent(stop.event_id)}>
              Open original event
              <ArrowUpRight size={12} />
            </button>
          </div>
        ))}
      </div>
      <div className="trip-facts">
        <span>
          Time between records <strong>{gap}</strong>
        </span>
        <span>
          Straight-line distance{" "}
          <strong>
            {Number.isFinite(trip.distance_km)
              ? `${trip.distance_km.toLocaleString("en-US", { maximumFractionDigits: 1 })} km`
              : "Unknown"}
          </strong>
        </span>
      </div>
    </section>
  );
}

export default function MapWorkspace({
  filters,
  refreshKey,
  onOpenEvent,
  onShowTimeline,
}: {
  filters: MapFilters;
  refreshKey: number;
  onOpenEvent: (id: string) => void;
  onShowTimeline: () => void;
}) {
  const [page, setPage] = useState<MapPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [selectedId, setSelectedId] = useState("");
  const [tilesEnabled, setTilesEnabled] = useState(() => {
    try {
      return window.localStorage.getItem("biography.basemap") !== "hidden";
    } catch {
      return true;
    }
  });
  const [mapOffset, setMapOffset] = useState(0);
  const [fitRequest, setFitRequest] = useState(0);
  const [sameDay, setSameDay] = useState<Page<LifeEvent> | null>(null);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState("");
  const [dayOffset, setDayOffset] = useState(0);
  const [tripsEnabled, setTripsEnabled] = useState(true);
  const [tripsPage, setTripsPage] = useState<TripsPage | null>(null);
  const [tripsLoading, setTripsLoading] = useState(true);
  const [tripsError, setTripsError] = useState("");
  const [tripsRetry, setTripsRetry] = useState(0);
  const [selectedTripId, setSelectedTripId] = useState("");
  const [provider, setProvider] = useState<"openstreetmap" | "google">(
    "openstreetmap",
  );
  const [mapConfig, setMapConfig] = useState<MapConfig | null>(null);
  const [providerError, setProviderError] = useState("");
  const [googleWasSelected, setGoogleWasSelected] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api<MapConfig>("/map-config", { signal: controller.signal })
      .then((config) => {
        if (!controller.signal.aborted) setMapConfig(config);
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setMapConfig({
            provider: "openstreetmap",
            google_maps_configured: false,
          });
      });
    return () => controller.abort();
  }, []);
  const { q, source, category, from, to, undated } = filters;
  const filterKey = [q, source, category, from, to, undated].join("\0");
  const [previousFilters, setPreviousFilters] = useState(filterKey);
  if (previousFilters !== filterKey) {
    setPreviousFilters(filterKey);
    setMapOffset(0);
  }

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      limit: "2000",
      offset: String(mapOffset),
    });
    for (const [key, value] of Object.entries({
      q,
      source,
      category,
      from,
      to,
    }))
      if (value) params.set(key, value);
    if (undated) params.set("undated", "true");
    api<MapPage>(`/map?${params}`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        const valid = result.items.filter(
          (point) =>
            Number.isFinite(point.latitude) &&
            Number.isFinite(point.longitude) &&
            Math.abs(point.latitude) <= 90 &&
            Math.abs(point.longitude) <= 180,
        );
        setPage({ ...result, items: valid });
        setSelectedId((previous) =>
          valid.some((point) => point.id === previous)
            ? previous
            : valid[0]?.id || "",
        );
        setDayOffset(0);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [q, source, category, from, to, undated, refreshKey, retry, mapOffset]);

  useEffect(() => {
    if (!tripsEnabled) {
      setSelectedTripId("");
      setTripsLoading(false);
      return;
    }
    const controller = new AbortController();
    setTripsLoading(true);
    setTripsError("");
    setTripsPage(null);
    setSelectedTripId("");
    const params = new URLSearchParams({ limit: "2000" });
    for (const [key, value] of Object.entries({
      q,
      source,
      category,
      from,
      to,
    }))
      if (value) params.set(key, value);
    if (undated) params.set("undated", "true");
    api<TripsPage>(`/trips?${params}`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        setTripsPage({
          ...result,
          items: result.items.filter(
            (trip) =>
              trip.inferred === true &&
              trip.geometry_type === "straight_line" &&
              validStop(trip.from) &&
              validStop(trip.to),
          ),
        });
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setTripsError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setTripsLoading(false);
      });
    return () => controller.abort();
  }, [
    q,
    source,
    category,
    from,
    to,
    undated,
    refreshKey,
    tripsEnabled,
    tripsRetry,
  ]);

  const locations = useMemo(() => page?.items ?? [], [page]);
  const trips = useMemo(
    () => (tripsEnabled ? (tripsPage?.items ?? []) : []),
    [tripsEnabled, tripsPage],
  );
  const canvasLocations = useMemo(
    () => withTripEndpoints(locations, trips),
    [locations, trips],
  );
  const selectedTrip = trips.find((trip) => trip.id === selectedTripId);
  const gapCount = tripsPage?.gaps_total ?? tripsPage?.gaps?.length ?? 0;
  const selected = canvasLocations.find((point) => point.id === selectedId);
  const day = selected ? recordedDay(selected) : null;
  useEffect(() => {
    if (!day) {
      setSameDay(null);
      setContextLoading(false);
      return;
    }
    const controller = new AbortController();
    setContextLoading(true);
    setContextError("");
    setSameDay(null);
    const params = new URLSearchParams({
      from: day,
      to: day,
      limit: "20",
      offset: String(dayOffset),
      sort: "asc",
    });
    api<Page<LifeEvent>>(`/events?${params}`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setSameDay(result);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setContextError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setContextLoading(false);
      });
    return () => controller.abort();
  }, [day, dayOffset, refreshKey]);

  const selectLocation = (id: string) => {
    setSelectedId(id);
    setDayOffset(0);
  };

  return (
    <section className="map-workspace" aria-label="Map and same-day timeline">
      <div className="map-heading">
        <div>
          <h2>
            <MapPin size={17} />
            Places in your history
          </h2>
          <p>
            Select a recorded point to explore that day across your sources.
          </p>
        </div>
        <span className="map-point-count">
          {loading
            ? "Loading locations…"
            : `${count(page?.total)} recorded ${page?.total === 1 ? "point" : "points"}`}
        </span>
      </div>
      <div className="map-integrity-note">
        <Compass size={14} />
        <span>
          Markers come from explicit source coordinates. Same-day posts provide
          temporal context; their location is not inferred. Dotted links infer
          connections between recorded stops, not recorded routes.
        </span>
      </div>
      <div className="trip-controls-row">
        <label>
          <input
            type="checkbox"
            checked={tripsEnabled}
            onChange={(event) => setTripsEnabled(event.target.checked)}
          />
          Inferred trip connections
        </label>
        <div className="map-legend">
          <span>
            <i className="legend-stop" />
            Recorded stops
          </span>
          <span>
            <i className="legend-trip" />
            Inferred connection
          </span>
        </div>
      </div>
      {tripsEnabled && (
        <div className="trip-status" role="status">
          {tripsLoading ? (
            "Loading connections from recorded stops…"
          ) : tripsError ? (
            <>
              <span>
                Trip connections unavailable. Recorded stops remain visible.
              </span>
              <button onClick={() => setTripsRetry((value) => value + 1)}>
                Retry connections
              </button>
            </>
          ) : (
            <span>
              {count(tripsPage?.total)} inferred connections
              {tripsPage?.truncated
                ? ` · First ${count(trips.length)} shown; narrow the dates to see a smaller period.`
                : ""}
              {gapCount
                ? ` · ${count(gapCount)} ${gapCount === 1 ? "gap" : "gaps"} left unconnected.`
                : ""}
              {trips.length === 0
                ? " Only dated recorded stops can be connected."
                : ""}
            </span>
          )}
        </div>
      )}
      {loading && !page ? (
        <div className="map-loading" role="status">
          <LoaderCircle className="spin" size={22} />
          Finding recorded locations…
        </div>
      ) : error && !page ? (
        <div className="notice error" role="alert">
          <CircleAlert size={17} />
          <span>{error}</span>
          <button onClick={() => setRetry((value) => value + 1)}>
            Try again
          </button>
        </div>
      ) : (
        <>
          {!canvasLocations.length && (
            <div className="empty-state map-empty-inline">
              <div className="empty-icon">
                <MapPin size={26} />
              </div>
              <h3>No explicit locations in this selection</h3>
              <p>
                Location records with coordinates appear here after import.
                Ordinary posts are never assigned a location from date alone.
              </p>
              <button className="map-timeline-link" onClick={onShowTimeline}>
                Browse the timeline instead
                <ArrowUpRight size={14} />
              </button>
            </div>
          )}
          {error && (
            <div className="notice error" role="alert">
              <CircleAlert size={15} />
              <span>{error}</span>
              <button onClick={() => setRetry((value) => value + 1)}>
                Try again
              </button>
            </div>
          )}
          <div className="map-controls">
            <label className="map-provider-label">
              Map provider
              <select
                aria-label="Map provider"
                value={provider}
                onChange={(event) => {
                  setProviderError("");
                  if (
                    event.target.value === "google" &&
                    mapConfig?.google_maps_configured &&
                    mapConfig.google_maps_api_key
                  ) {
                    setGoogleWasSelected(true);
                    setProvider("google");
                  } else setProvider("openstreetmap");
                }}
              >
                <option value="openstreetmap">OpenStreetMap</option>
                <option
                  value="google"
                  disabled={
                    !mapConfig?.google_maps_configured ||
                    !mapConfig.google_maps_api_key
                  }
                >
                  Google Maps
                  {mapConfig?.google_maps_configured &&
                  mapConfig.google_maps_api_key
                    ? ""
                    : " · not configured"}
                </option>
              </select>
            </label>
            <label>
              <input
                type="checkbox"
                checked={tilesEnabled}
                onChange={(event) => {
                  setTilesEnabled(event.target.checked);
                  try {
                    window.localStorage.setItem(
                      "biography.basemap",
                      event.target.checked ? "shown" : "hidden",
                    );
                  } catch {
                    /* Preference storage can be disabled. */
                  }
                }}
              />
              {provider === "google"
                ? "Google Maps basemap"
                : "OpenStreetMap basemap"}
            </label>
            <button onClick={() => setFitRequest((value) => value + 1)}>
              <LocateFixed size={14} />
              Fit recorded points
            </button>
          </div>
          {!mapConfig?.google_maps_configured && (
            <p className="google-setup-hint">
              Google Maps is optional. Add a localhost-restricted browser key in
              the private local configuration to enable it; OpenStreetMap stays
              the default.
            </p>
          )}
          {providerError && (
            <div className="provider-fallback-note" role="status">
              {providerError}
            </div>
          )}
          {provider === "google" &&
          tilesEnabled &&
          mapConfig?.google_maps_api_key ? (
            <GoogleCanvas
              apiKey={mapConfig.google_maps_api_key}
              onFailure={() => {
                setProvider("openstreetmap");
                setProviderError(
                  "Google Maps could not load. OpenStreetMap is active with the same recorded stops and inferred connections.",
                );
              }}
              locations={canvasLocations}
              selectedId={selectedId}
              onSelect={selectLocation}
              tilesEnabled={tilesEnabled}
              fitRequest={fitRequest}
              trips={trips}
              selectedTripId={selectedTripId}
              onSelectTrip={setSelectedTripId}
            />
          ) : (
            <LeafletCanvas
              locations={canvasLocations}
              selectedId={selectedId}
              onSelect={selectLocation}
              tilesEnabled={tilesEnabled && provider === "openstreetmap"}
              fitRequest={fitRequest}
              trips={trips}
              selectedTripId={selectedTripId}
              onSelectTrip={setSelectedTripId}
            />
          )}
          <p className="map-privacy-note">
            {provider === "google"
              ? "Google Maps receives the viewed area and coordinates used to render map overlays. Your post text is not sent. Google map loads may use your project quota."
              : "Basemap requests reveal the viewed map area to OpenStreetMap. Hide the basemap to stop external tile requests. Your posts are not sent."}
            {googleWasSelected &&
              " Hiding the basemap removes the Google map; its JavaScript remains loaded until the page is closed."}
          </p>
          {selectedTrip && (
            <TripDetails
              trip={selectedTrip}
              onOpenEvent={onOpenEvent}
              close={() => setSelectedTripId("")}
            />
          )}
          {tripsEnabled && trips.length > 0 && (
            <details className="trip-list">
              <summary>
                Browse {count(trips.length)} inferred connections
              </summary>
              <p>
                Chronological recorded stops from the trip index, independent of
                the point page. Straight lines do not establish the road,
                transport, or path taken.
              </p>
              <div>
                {trips.slice(0, 200).map((trip) => (
                  <button
                    key={trip.id}
                    className={selectedTripId === trip.id ? "selected" : ""}
                    onClick={() => setSelectedTripId(trip.id)}
                  >
                    <span>
                      {trip.from.title || "Recorded stop"} →{" "}
                      {trip.to.title || "Recorded stop"}
                    </span>
                    <span>
                      {formatDate(trip.departure_at || trip.from.occurred_at)} ·{" "}
                      {count(Math.round(trip.distance_km))} km
                    </span>
                  </button>
                ))}
              </div>
              {trips.length > 200 && (
                <p>
                  First 200 listed. All loaded connections are on the map;
                  narrow the date filter for a shorter list.
                </p>
              )}
            </details>
          )}
          {page && (page.total > locations.length || mapOffset > 0) && (
            <div className="map-limit-note">
              <span>
                Showing {count(mapOffset + 1)}–
                {count(mapOffset + locations.length)} of {count(page.total)}{" "}
                recorded points. Use date filters to explore a smaller period.
              </span>
              <div>
                {mapOffset > 0 && (
                  <button
                    onClick={() =>
                      setMapOffset((value) => Math.max(0, value - 2000))
                    }
                  >
                    Newer points
                  </button>
                )}
                {mapOffset + 2000 < page.total && (
                  <button onClick={() => setMapOffset((value) => value + 2000)}>
                    Older points
                  </button>
                )}
              </div>
            </div>
          )}
          <div className="map-context-grid">
            <section className="location-list-section">
              <div className="map-section-heading">
                <h3>Recorded locations</h3>
                <span>{count(locations.length)} loaded</span>
              </div>
              <div
                className="recorded-location-list"
                tabIndex={0}
                role="region"
                aria-label="Recorded locations list"
              >
                {locations.slice(0, 200).map((point) => (
                  <button
                    key={point.id}
                    className={`recorded-location ${point.id === selectedId ? "selected" : ""}`}
                    onClick={() => selectLocation(point.id)}
                  >
                    <span className="location-list-icon">
                      <MapPin size={14} />
                    </span>
                    <span>
                      <strong>{point.title || "Recorded location"}</strong>
                      <span>
                        {point.time_precision === "unknown"
                          ? "Date uncertain"
                          : formatDate(
                              point.occurred_at,
                              point.time_precision,
                            )}{" "}
                        · {point.source}
                      </span>
                      <span className="location-coordinates">
                        {point.latitude.toFixed(5)},{" "}
                        {point.longitude.toFixed(5)} ·{" "}
                        {humanize(point.kind || "point")}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
              {locations.length > 200 && (
                <p className="list-limit-note">
                  First 200 shown in the list. All loaded points are on the map;
                  use filters to narrow your selection.
                </p>
              )}
            </section>
            <section
              className="day-context-section"
              aria-live="polite"
              tabIndex={0}
              aria-label="Same-day context panel"
            >
              <div className="map-section-heading">
                <h3>
                  <CalendarDays size={14} />
                  {day ? formatDate(day) : "Same-day context"}
                </h3>
                {selected && (
                  <button
                    className="icon-button"
                    onClick={() => {
                      setSelectedId("");
                      setSameDay(null);
                    }}
                    aria-label="Clear selected location"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
              {selected && (
                <div className="selected-location-summary">
                  <span className="selected-location-label">
                    EXPLICIT LOCATION RECORD
                  </span>
                  <h4>{selected.title || "Recorded location"}</h4>
                  <p>
                    {selected.latitude.toFixed(5)},{" "}
                    {selected.longitude.toFixed(5)} · {selected.source}
                  </p>
                  {selected.metadata?.time_basis === "parent_event_start" && (
                    <p className="location-time-basis">
                      Date follows the parent record's start time.
                    </p>
                  )}
                  <button onClick={() => onOpenEvent(selected.event_id)}>
                    View location evidence
                    <ArrowUpRight size={12} />
                  </button>
                </div>
              )}
              <p className="temporal-context-note">
                Records dated to the same day, across all sources. This does not
                establish that a post was written at the selected place.
              </p>
              {!selected ? (
                <p className="context-empty">
                  Choose a point on the map or in the list.
                </p>
              ) : !day ? (
                <p className="context-empty">
                  This location has no recorded day to match against other
                  records.
                </p>
              ) : contextLoading ? (
                <div className="context-loading">
                  <LoaderCircle className="spin" size={16} />
                  Loading that day…
                </div>
              ) : contextError ? (
                <div className="notice error" role="alert">
                  <CircleAlert size={15} />
                  {contextError}
                </div>
              ) : !sameDay?.items.length ? (
                <p className="context-empty">
                  No records are available for this day.
                </p>
              ) : (
                <>
                  <div className="same-day-events">
                    {sameDay.items.map((event) => (
                      <article
                        key={event.id}
                        className={`same-day-event ${event.id === selected.event_id ? "location-origin" : ""}`}
                      >
                        <button
                          className="same-day-open"
                          onClick={() => onOpenEvent(event.id)}
                        >
                          <span>
                            {event.source}
                            <span>{humanize(event.category)}</span>
                          </span>
                          <h4>{event.title || "Untitled moment"}</h4>
                          {event.body && <p>{event.body}</p>}
                          <span className="same-day-evidence">
                            <FileText size={11} />
                            {event.id === selected.event_id
                              ? "Selected location record"
                              : "Open original context"}
                            <ArrowUpRight size={11} />
                          </span>
                        </button>
                        <MediaGallery media={event.media} compact />
                      </article>
                    ))}
                  </div>
                  <div className="day-pagination">
                    <span>{count(sameDay.total)} records this day</span>
                    {dayOffset > 0 && (
                      <button
                        onClick={() =>
                          setDayOffset((value) => Math.max(0, value - 20))
                        }
                      >
                        Previous
                      </button>
                    )}
                    {dayOffset + 20 < sameDay.total && (
                      <button
                        onClick={() => setDayOffset((value) => value + 20)}
                      >
                        Next
                      </button>
                    )}
                  </div>
                </>
              )}
            </section>
          </div>
        </>
      )}
    </section>
  );
}
