import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { GOOGLE_MAP_ERROR, loadGoogleMaps } from "./googleLoader";
import {
  googleTripBounds,
  tripDisplayPath,
  type MapCanvasProps,
} from "./types";

export default function GoogleCanvas({
  apiKey,
  onFailure,
  ...props
}: MapCanvasProps & { apiKey: string; onFailure: () => void }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<google.maps.Map | null>(null);
  const circles = useRef(new Map<string, google.maps.Circle>());
  const lines = useRef(new Map<string, google.maps.Polyline>());
  const latest = useRef(props);
  latest.current = props;
  const failure = useRef(onFailure);
  failure.current = onFailure;
  const [api, setApi] = useState<typeof google.maps | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let live = true;
    const fail = () => {
      if (live) failure.current();
    };
    window.addEventListener(GOOGLE_MAP_ERROR, fail);
    loadGoogleMaps(apiKey)
      .then((maps) => {
        if (!live || !container.current) return;
        try {
          const instance = new maps.Map(container.current, {
            center: { lat: 20, lng: 0 },
            zoom: 2,
            mapTypeId: "roadmap",
            renderingType: maps.RenderingType.RASTER,
            zoomControl: true,
            mapTypeControl: true,
            streetViewControl: false,
            fullscreenControl: false,
            clickableIcons: false,
            gestureHandling: "cooperative",
          });
          map.current = instance;
          setApi(maps);
          setReady(true);
          instance.addListener("zoom_changed", () => {
            const zoom = instance.getZoom() ?? 2;
            for (const [id, circle] of circles.current) {
              const lat = circle.getCenter()?.lat() ?? 0;
              const pixels = id === latest.current.selectedId ? 9 : 6;
              circle.setRadius(
                ((156543.03392 * Math.cos((lat * Math.PI) / 180)) / 2 ** zoom) *
                  pixels,
              );
            }
          });
        } catch {
          fail();
        }
      })
      .catch(fail);
    return () => {
      live = false;
      window.removeEventListener(GOOGLE_MAP_ERROR, fail);
      for (const circle of circles.current.values()) {
        window.google?.maps?.event.clearInstanceListeners(circle);
        circle.setMap(null);
      }
      for (const line of lines.current.values()) {
        window.google?.maps?.event.clearInstanceListeners(line);
        line.setMap(null);
      }
      circles.current.clear();
      lines.current.clear();
      if (map.current && window.google?.maps)
        window.google.maps.event.clearInstanceListeners(map.current);
      map.current = null;
      container.current?.replaceChildren();
    };
  }, [apiKey]);

  useEffect(() => {
    const instance = map.current;
    if (!api || !instance) return;
    for (const circle of circles.current.values()) {
      api.event.clearInstanceListeners(circle);
      circle.setMap(null);
    }
    for (const line of lines.current.values()) {
      api.event.clearInstanceListeners(line);
      line.setMap(null);
    }
    circles.current.clear();
    lines.current.clear();
    const zoom = instance.getZoom() ?? 2;
    for (const point of props.locations) {
      const circle = new api.Circle({
        map: instance,
        center: { lat: point.latitude, lng: point.longitude },
        radius:
          ((156543.03392 * Math.cos((point.latitude * Math.PI) / 180)) /
            2 ** zoom) *
          6,
        strokeColor: "#ffffff",
        strokeWeight: 2,
        fillColor: "#3465b4",
        fillOpacity: 0.8,
        zIndex: 3,
        clickable: true,
      });
      circle.addListener("click", () => latest.current.onSelect(point.id));
      circles.current.set(point.id, circle);
    }
    for (const trip of props.trips) {
      const path = tripDisplayPath(trip).map(([lat, lng]) => ({ lat, lng }));
      const line = new api.Polyline({
        map: instance,
        path,
        geodesic: false,
        strokeOpacity: 0,
        strokeWeight: 8,
        zIndex: 1,
        clickable: true,
        icons: [
          {
            icon: {
              path: "M 0,-1 0,1",
              strokeColor: "#8399b3",
              strokeOpacity: 0.55,
              strokeWeight: 1.5,
              scale: 1,
            },
            offset: "0",
            repeat: "9px",
          },
        ],
      });
      line.addListener("click", () => latest.current.onSelectTrip(trip.id));
      lines.current.set(trip.id, line);
    }
  }, [api, props.locations, props.trips]);

  useEffect(() => {
    const instance = map.current;
    if (!api || !instance || !props.locations.length) return;
    const bounds = new api.LatLngBounds();
    for (const point of props.locations)
      bounds.extend({ lat: point.latitude, lng: point.longitude });
    instance.fitBounds(bounds, 35);
    const listener = api.event.addListenerOnce(instance, "idle", () => {
      if ((instance.getZoom() ?? 0) > 14) instance.setZoom(14);
    });
    return () => api.event.removeListener(listener);
  }, [api, props.locations, props.fitRequest]);

  useEffect(() => {
    const instance = map.current;
    if (!api || !instance) return;
    for (const point of props.locations) {
      const circle = circles.current.get(point.id);
      if (!circle) continue;
      const selected = point.id === props.selectedId;
      circle.setOptions({
        fillColor: selected ? "#ca8a25" : "#3465b4",
        fillOpacity: selected ? 1 : 0.8,
        zIndex: selected ? 5 : 3,
      });
      circle.setRadius(
        ((156543.03392 * Math.cos((point.latitude * Math.PI) / 180)) /
          2 ** (instance.getZoom() ?? 2)) *
          (selected ? 9 : 6),
      );
      if (selected && !props.selectedTripId)
        instance.panTo({ lat: point.latitude, lng: point.longitude });
    }
    for (const [id, line] of lines.current)
      line.setOptions({
        icons: [
          {
            icon: {
              path: "M 0,-1 0,1",
              strokeColor: id === props.selectedTripId ? "#b58b4e" : "#8399b3",
              strokeOpacity: id === props.selectedTripId ? 0.8 : 0.55,
              strokeWeight: 1.5,
              scale: 1,
            },
            offset: "0",
            repeat: "9px",
          },
        ],
      });
    const trip = props.trips.find((item) => item.id === props.selectedTripId);
    if (trip) {
      instance.fitBounds(googleTripBounds(trip), 45);
      const listener = api.event.addListenerOnce(instance, "idle", () => {
        if ((instance.getZoom() ?? 0) > 12) instance.setZoom(12);
      });
      return () => api.event.removeListener(listener);
    }
  }, [
    api,
    props.locations,
    props.trips,
    props.selectedId,
    props.selectedTripId,
  ]);

  return (
    <div className="map-canvas-wrap">
      <div
        ref={container}
        className="location-canvas"
        role="region"
        aria-label="Map of explicitly recorded locations. Use the recorded locations list to select a point."
      />
      {!ready && (
        <div className="google-map-loading" role="status">
          <LoaderCircle size={18} className="spin" />
          Loading Google Maps…
        </div>
      )}
    </div>
  );
}
