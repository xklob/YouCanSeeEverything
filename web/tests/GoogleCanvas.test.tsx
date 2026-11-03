import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import GoogleCanvas from "../src/maps/GoogleCanvas";
import type { MapCanvasProps } from "../src/maps/types";

const props: MapCanvasProps = {
  locations: [
    {
      id: "point",
      event_id: "record",
      latitude: 37.5,
      longitude: -77.4,
      occurred_at: "2020-03-14",
      title: "Recorded stop",
      source: "Synthetic",
      kind: "place",
    },
  ],
  selectedId: "point",
  onSelect: vi.fn(),
  tilesEnabled: true,
  fitRequest: 0,
  trips: [
    {
      id: "leg",
      source: "Synthetic",
      from: {
        event_id: "record",
        latitude: 37.5,
        longitude: -77.4,
        occurred_at: "2020-03-14",
        title: "Start",
      },
      to: {
        event_id: "record-two",
        latitude: 38,
        longitude: -78,
        occurred_at: "2020-03-15",
        title: "End",
      },
      inferred: true,
      geometry_type: "straight_line",
      gap_hours: 24,
      distance_km: 70,
    },
  ],
  selectedTripId: "",
  onSelectTrip: vi.fn(),
};
let maps: Record<string, unknown>;
let circles: Record<string, ReturnType<typeof vi.fn>>[];
let lines: Record<string, ReturnType<typeof vi.fn>>[];
beforeEach(() => {
  circles = [];
  lines = [];
  const overlay = () => ({
    setMap: vi.fn(),
    addListener: vi.fn(),
    setOptions: vi.fn(),
    setRadius: vi.fn(),
    getCenter: vi.fn(() => ({ lat: () => 37.5 })),
  });
  maps = {
    RenderingType: { RASTER: "RASTER" },
    Map: vi.fn(function () {
      return {
        addListener: vi.fn(),
        getZoom: vi.fn(() => 8),
        fitBounds: vi.fn(),
        panTo: vi.fn(),
        setZoom: vi.fn(),
      };
    }),
    Circle: vi.fn(function () {
      const value = overlay();
      circles.push(value);
      return value;
    }),
    Polyline: vi.fn(function () {
      const value = overlay();
      lines.push(value);
      return value;
    }),
    LatLngBounds: vi.fn(function () {
      return { extend: vi.fn() };
    }),
    event: {
      clearInstanceListeners: vi.fn(),
      addListenerOnce: vi.fn(),
      removeListener: vi.fn(),
    },
  };
  vi.stubGlobal("google", { maps });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("creates one Google map while filters only replace coordinate and dotted-trip overlays", async () => {
  const failure = vi.fn();
  const rendered = render(
    <GoogleCanvas
      {...props}
      apiKey="synthetic-browser-key"
      onFailure={failure}
    />,
  );
  await waitFor(() => {
    expect(maps.Map).toHaveBeenCalledTimes(1);
    expect(maps.Polyline).toHaveBeenCalledWith(
      expect.objectContaining({
        geodesic: false,
        strokeOpacity: 0,
        zIndex: 1,
        icons: [expect.objectContaining({ repeat: "12px" })],
      }),
    );
    expect(maps.Circle).toHaveBeenCalledWith(
      expect.objectContaining({ zIndex: 3 }),
    );
  });
  rendered.rerender(
    <GoogleCanvas
      {...props}
      locations={[]}
      trips={[]}
      apiKey="synthetic-browser-key"
      onFailure={failure}
    />,
  );
  rendered.rerender(
    <GoogleCanvas
      {...props}
      locations={[
        ...props.locations,
        { ...props.locations[0], id: "new-point" },
      ]}
      apiKey="synthetic-browser-key"
      onFailure={failure}
    />,
  );
  expect(maps.Map).toHaveBeenCalledTimes(1);
  expect(failure).not.toHaveBeenCalled();
  rendered.unmount();
  expect(
    circles.every((circle) =>
      circle.setMap.mock.calls.some((call) => call[0] === null),
    ),
  ).toBe(true);
  expect(
    lines.every((line) =>
      line.setMap.mock.calls.some((call) => call[0] === null),
    ),
  ).toBe(true);
  expect(document.querySelector('script[src*="googleapis"]')).toBeNull();
});

it("reports Google authentication failures without passing a key or remote URL", async () => {
  const failure = vi.fn();
  render(
    <GoogleCanvas
      {...props}
      apiKey="synthetic-browser-key"
      onFailure={failure}
    />,
  );
  await waitFor(() => expect(maps.Map).toHaveBeenCalledTimes(1));
  window.dispatchEvent(new Event("biography-google-map-error"));
  expect(failure).toHaveBeenCalledWith();
});
