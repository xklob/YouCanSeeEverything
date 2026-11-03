import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import {
  ArrowUpRight,
  CalendarDays,
  CircleAlert,
  Compass,
  FileText,
  Layers3,
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

function LocationCanvas({
  locations,
  selectedId,
  onSelect,
  tilesEnabled,
  fitRequest,
}: {
  locations: MapLocation[];
  selectedId: string;
  onSelect: (id: string) => void;
  tilesEnabled: boolean;
  fitRequest: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const markers = useRef(new Map<string, L.CircleMarker>());
  const select = useRef(onSelect);
  select.current = onSelect;
  const [tileError, setTileError] = useState(false);

  useEffect(() => {
    if (!container.current) return;
    const instance = L.map(container.current, {
      zoomControl: true,
      scrollWheelZoom: false,
      preferCanvas: true,
    }).setView([20, 0], 2);
    map.current = instance;
    const resize = new ResizeObserver(() => instance.invalidateSize());
    resize.observe(container.current);
    return () => {
      resize.disconnect();
      instance.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !tilesEnabled) return;
    setTileError(false);
    const layer = L.tileLayer(
      "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19,
        referrerPolicy: "origin",
        keepBuffer: 0,
      },
    );
    layer.on("tileerror", () => setTileError(true));
    layer.addTo(instance);
    return () => {
      layer.remove();
    };
  }, [tilesEnabled]);

  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    const group = L.layerGroup().addTo(instance);
    markers.current.clear();
    for (const point of locations) {
      const marker = L.circleMarker([point.latitude, point.longitude], {
        radius: 6,
        color: "#ffffff",
        weight: 2,
        fillColor: "#3465b4",
        fillOpacity: 0.8,
      });
      const label = document.createElement("span");
      label.textContent = point.title || "Recorded location";
      marker.bindTooltip(label, { direction: "top" });
      marker.on("click", () => select.current(point.id));
      marker.addTo(group);
      markers.current.set(point.id, marker);
    }
    if (locations.length)
      instance.fitBounds(
        L.latLngBounds(
          locations.map((point) => [point.latitude, point.longitude]),
        ),
        { padding: [35, 35], maxZoom: 14, animate: false },
      );
    return () => {
      group.remove();
      markers.current.clear();
    };
  }, [locations, fitRequest]);

  useEffect(() => {
    for (const [id, marker] of markers.current) {
      marker.setStyle({
        fillColor: id === selectedId ? "#ca8a25" : "#3465b4",
        fillOpacity: id === selectedId ? 1 : 0.8,
      });
      marker.setRadius(id === selectedId ? 9 : 6);
      if (id === selectedId) {
        marker.bringToFront();
        map.current?.panTo(marker.getLatLng(), { animate: false });
      }
    }
  }, [selectedId, locations]);

  return (
    <div className="map-canvas-wrap">
      <div
        ref={container}
        className="location-canvas"
        role="region"
        aria-label="Map of explicitly recorded locations. Use the recorded locations list to select a point."
      />
      {!tilesEnabled && (
        <div className="basemap-disabled">
          <Layers3 size={14} />
          Basemap hidden. Your recorded points remain visible.
        </div>
      )}
      {tilesEnabled && tileError && (
        <div className="map-tile-error" role="status">
          <CircleAlert size={13} />
          Some map tiles are unavailable. Recorded points and the location list
          still work.
        </div>
      )}
    </div>
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

  const locations = useMemo(() => page?.items ?? [], [page]);
  const selected = locations.find((point) => point.id === selectedId);
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
          temporal context; their location is not inferred. No routes are drawn.
        </span>
      </div>
      {loading ? (
        <div className="map-loading" role="status">
          <LoaderCircle className="spin" size={22} />
          Finding recorded locations…
        </div>
      ) : error ? (
        <div className="notice error" role="alert">
          <CircleAlert size={17} />
          <span>{error}</span>
          <button onClick={() => setRetry((value) => value + 1)}>
            Try again
          </button>
        </div>
      ) : !locations.length ? (
        <div className="empty-state">
          <div className="empty-icon">
            <MapPin size={26} />
          </div>
          <h3>No explicit locations in this selection</h3>
          <p>
            Location records with coordinates appear here after import. Ordinary
            posts are never assigned a location from date alone.
          </p>
          <button className="map-timeline-link" onClick={onShowTimeline}>
            Browse the timeline instead
            <ArrowUpRight size={14} />
          </button>
        </div>
      ) : (
        <>
          <div className="map-controls">
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
              OpenStreetMap basemap
            </label>
            <button onClick={() => setFitRequest((value) => value + 1)}>
              <LocateFixed size={14} />
              Fit recorded points
            </button>
          </div>
          <LocationCanvas
            locations={locations}
            selectedId={selectedId}
            onSelect={selectLocation}
            tilesEnabled={tilesEnabled}
            fitRequest={fitRequest}
          />
          <p className="map-privacy-note">
            Basemap requests reveal the viewed map area to OpenStreetMap. Hide
            the basemap to stop external tile requests. Your posts are not sent.
          </p>
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
              <div className="recorded-location-list">
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
            <section className="day-context-section" aria-live="polite">
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
