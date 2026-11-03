import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { CircleAlert, Layers3 } from "lucide-react";
import { tripDisplayPath, type MapCanvasProps } from "./types";

export default function LeafletCanvas({
  locations,
  selectedId,
  onSelect,
  tilesEnabled,
  fitRequest,
  trips,
  selectedTripId,
  onSelectTrip,
}: MapCanvasProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const markers = useRef(new Map<string, L.CircleMarker>());
  const select = useRef(onSelect);
  select.current = onSelect;
  const selectTrip = useRef(onSelectTrip);
  selectTrip.current = onSelectTrip;
  const [tileError, setTileError] = useState(false);

  useEffect(() => {
    if (!container.current) return;
    const instance = L.map(container.current, {
      zoomControl: true,
      scrollWheelZoom: false,
      preferCanvas: true,
    }).setView([20, 0], 2);
    map.current = instance;
    instance.createPane("inferred-trips").style.zIndex = "350";
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
    for (const trip of trips) {
      const line = L.polyline(tripDisplayPath(trip), {
        color: trip.id === selectedTripId ? "#b58b4e" : "#8399b3",
        weight: 2,
        opacity: trip.id === selectedTripId ? 0.8 : 0.5,
        dashArray: "2 7",
        lineCap: "round",
        pane: "inferred-trips",
      });
      const label = document.createElement("span");
      label.textContent =
        "Inferred connection between recorded stops. No recorded route.";
      line.bindTooltip(label);
      line.on("click", () => selectTrip.current(trip.id));
      line.addTo(group);
    }
    return () => {
      group.remove();
    };
  }, [trips, selectedTripId]);

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

  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    const trip = trips.find((item) => item.id === selectedTripId);
    const path = trip ? tripDisplayPath(trip) : null;
    const anchor = path
      ? (path[0][1] + path[1][1]) / 2
      : instance.getCenter().lng;
    for (const point of locations)
      markers.current
        .get(point.id)
        ?.setLatLng([
          point.latitude,
          point.longitude + 360 * Math.round((anchor - point.longitude) / 360),
        ]);
    if (path)
      instance.fitBounds(path, {
        padding: [45, 45],
        maxZoom: 12,
        animate: false,
      });
  }, [selectedTripId, trips, locations, fitRequest]);

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
