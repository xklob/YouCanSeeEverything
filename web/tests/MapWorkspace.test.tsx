import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import L from "leaflet";
import userEvent from "@testing-library/user-event";
import MapWorkspace, { recordedDay } from "../src/MapWorkspace";
import MediaGallery from "../src/MediaGallery";
import type { MapLocation } from "../src/api";
import { rememberMapProvider } from "../src/maps/providerSession";
import {
  withTripEndpoints,
  tripDisplayPath,
  type TripConnection,
} from "../src/maps/types";

const { tileLayer, maps } = vi.hoisted(() => ({
  tileLayer: vi.fn(),
  maps: [] as unknown[],
}));
const { googleCanvas } = vi.hoisted(() => ({ googleCanvas: vi.fn() }));
vi.mock("../src/maps/GoogleCanvas", () => ({
  default: (props: { onFailure: () => void }) => {
    googleCanvas(props);
    return (
      <div aria-label="Mock Google map">
        <button onClick={props.onFailure}>Simulate Google failure</button>
      </div>
    );
  },
}));
vi.mock("leaflet", () => {
  const chain = () => {
    const self: Record<string, unknown> = {};
    for (const key of [
      "setView",
      "addTo",
      "remove",
      "bindTooltip",
      "on",
      "setStyle",
      "setRadius",
      "bringToFront",
      "panTo",
      "fitBounds",
      "invalidateSize",
      "setLatLng",
    ])
      self[key] = vi.fn(() => self);
    self.getLatLng = () => ({ lat: 38, lng: -77 });
    self.getCenter = () => ({ lat: 38, lng: -77 });
    self.createPane = () => ({ style: {} });
    return self;
  };
  tileLayer.mockImplementation(chain);
  return {
    default: {
      map: vi.fn(() => {
        const map = chain();
        maps.push(map);
        return map;
      }),
      tileLayer,
      circleMarker: vi.fn(chain),
      polyline: vi.fn(chain),
      layerGroup: vi.fn(chain),
      latLngBounds: vi.fn((values) => values),
    },
  };
});

const point: MapLocation = {
  id: "loc-one",
  event_id: "location-record",
  latitude: 37.5,
  longitude: -77.4,
  occurred_at: "2020-03-14T12:00:00Z",
  title: "Explicit visit",
  source: "Location archive",
  kind: "visit",
  time_precision: "day",
};
const second: MapLocation = {
  ...point,
  id: "loc-two",
  event_id: "undated-record",
  title: "Month-only place",
  time_precision: "month",
};
const filters = {
  q: "",
  source: "",
  category: "",
  from: "",
  to: "",
  undated: false,
};
let requests: string[];
beforeEach(() => {
  rememberMapProvider(null);
  requests = [];
  localStorage.clear();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      requests.push(path);
      const data =
        path === "/api/map-config"
          ? { provider: "openstreetmap", google_maps_configured: false }
          : path.startsWith("/api/trips?")
            ? { items: [], total: 0, truncated: false, gaps: [] }
            : path.startsWith("/api/map?")
              ? { items: [point, second], total: 2 }
              : {
                  items: [
                    {
                      id: "post-record",
                      title: "A post on the same day",
                      body: "This has no recorded location.",
                      occurred_at: point.occurred_at,
                      source: "Post archive",
                      category: "post",
                      media: [
                        {
                          file_id: "image-file",
                          url: "/api/files/image-file/media",
                          mime: "image/jpeg",
                          kind: "image",
                          caption: "An attached photo",
                        },
                      ],
                    },
                  ],
                  total: 1,
                  limit: 20,
                  offset: 0,
                };
      return new Response(JSON.stringify(data));
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it("links explicit points to same-day evidence across sources without claiming a post location", async () => {
  const open = vi.fn();
  const user = userEvent.setup();
  render(
    <MapWorkspace
      filters={{ ...filters, source: "location-source", q: "visit" }}
      refreshKey={0}
      onOpenEvent={open}
      onShowTimeline={vi.fn()}
    />,
  );
  await screen.findByText("A post on the same day");
  expect(screen.getByText(/do not establish a shared location/)).toBeTruthy();
  expect(
    screen.getByText(/does not establish that a post was written/),
  ).toBeTruthy();
  const request = requests.find((path) => path.startsWith("/api/map?"))!;
  expect(new URL(request, "http://localhost").searchParams.get("source")).toBe(
    "location-source",
  );
  const contextRequest = requests.find((path) =>
    path.startsWith("/api/events?"),
  )!;
  const context = new URL(contextRequest, "http://localhost").searchParams;
  expect(context.get("from")).toBe("2020-03-14");
  expect(context.get("to")).toBe("2020-03-14");
  expect(context.has("source")).toBe(false);
  expect(context.has("q")).toBe(false);
  expect(
    screen.getByAltText("An attached photo").getAttribute("src"),
  ).toContain("/api/files/image-file/media");
  await user.click(
    screen.getByRole("button", { name: /A post on the same day/ }),
  );
  expect(open).toHaveBeenCalledWith("post-record");
  await user.click(
    screen.getByRole("button", { name: "View location evidence" }),
  );
  expect(open).toHaveBeenCalledWith("location-record");
  expect(tileLayer).toHaveBeenCalledWith(
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    expect.objectContaining({ referrerPolicy: "origin", keepBuffer: 0 }),
  );
  await user.click(screen.getByRole("button", { name: /Month-only place/ }));
  expect(await screen.findByText(/no recorded day to match/)).toBeTruthy();
  expect(screen.queryByText("A post on the same day")).toBeNull();
});

it("refuses same-day links for imprecise dates", () => {
  expect(recordedDay({ ...point, time_precision: "unknown" })).toBeNull();
  expect(recordedDay({ ...point, time_precision: "year" })).toBeNull();
  expect(recordedDay(point)).toBe("2020-03-14");
});

it("embeds retained image and video attachments while refusing external image URLs", async () => {
  render(
    <MediaGallery
      media={[
        {
          file_id: "local",
          url: "/api/files/local/media",
          kind: "image",
          mime: "image/png",
          caption: "Local image",
        },
        {
          file_id: "video",
          url: "/api/files/video/media",
          kind: "video",
          mime: "video/mp4",
          caption: "Local video",
        },
        {
          file_id: "remote",
          url: "https://example.com/private.jpg",
          kind: "image",
          mime: "image/jpeg",
          caption: "Remote",
        },
      ]}
    />,
  );
  expect(screen.getByAltText("Local image").getAttribute("src")).toContain(
    "/api/files/local/media",
  );
  const video = screen.getByLabelText("Local video") as HTMLVideoElement;
  expect(video.controls).toBe(true);
  expect(video.preload).toBe("none");
  expect(document.querySelector('img[src^="https://example.com"]')).toBeNull();
  expect(
    screen.getByText("This attachment has no local media file."),
  ).toBeTruthy();
  await waitFor(() => expect(document.querySelectorAll("img").length).toBe(1));
});

it("draws only backend-provided inferred legs with off-page recorded endpoints and original evidence", async () => {
  const trip: TripConnection = {
    id: "leg-one",
    source: "Location archive",
    from: {
      ...point,
      event_id: point.event_id,
      title: "Earlier recorded stop",
    },
    to: {
      id: "off-page",
      event_id: "later-record",
      latitude: 38.2,
      longitude: -78.1,
      occurred_at: "2020-03-15T12:00:00Z",
      title: "Later recorded stop",
      time_precision: "day",
    },
    gap_hours: 24,
    distance_km: 99.3,
    inferred: true,
    geometry_type: "straight_line",
  };
  vi.mocked(fetch).mockImplementation(async (input) => {
    const path = String(input);
    requests.push(path);
    const data = path.startsWith("/api/trips?")
      ? {
          items: [trip],
          total: 12,
          truncated: true,
          gaps: [{ reason: "missing_data" }],
        }
      : path.startsWith("/api/map?")
        ? { items: [point], total: 4500 }
        : { items: [], total: 0, offset: 0, limit: 20 };
    return new Response(JSON.stringify(data));
  });
  const user = userEvent.setup();
  const open = vi.fn();
  render(
    <MapWorkspace
      filters={{ ...filters, from: "2020-03-01", to: "2020-03-31" }}
      refreshKey={0}
      onOpenEvent={open}
      onShowTimeline={vi.fn()}
    />,
  );
  await waitFor(() => expect(L.polyline).toHaveBeenCalled());
  expect(L.polyline).toHaveBeenCalledWith(
    [
      [37.5, -77.4],
      [38.2, -78.1],
    ],
    expect.objectContaining({
      dashArray: "5 6",
      pane: "inferred-trips",
      weight: 3,
    }),
  );
  expect(L.circleMarker).toHaveBeenCalledWith([38.2, -78.1], expect.anything());
  expect(screen.getByText(/12 inferred connections/)).toBeTruthy();
  expect(screen.getByText(/1 gap left unconnected/)).toBeTruthy();
  const line = vi.mocked(L.polyline).mock.results.at(-1)!.value;
  const callback = vi
    .mocked(line.on)
    .mock.calls.find((call) => call[0] === "click")?.[1];
  expect(typeof callback).toBe("function");
  act(() => {
    (callback as () => void)();
  });
  expect(
    screen.getByRole("region", { name: "Inferred trip connection details" }),
  ).toBeTruthy();
  expect(screen.getByText("99.3 km")).toBeTruthy();
  expect(screen.getByText("1 day")).toBeTruthy();
  await user.click(
    screen.getAllByRole("button", { name: "Open record", exact: true })[1],
  );
  expect(open).toHaveBeenCalledWith("later-record");
  await user.click(screen.getByRole("button", { name: "Next points" }));
  await waitFor(() =>
    expect(
      requests.some(
        (path) => path.startsWith("/api/map?") && path.includes("offset=2000"),
      ),
    ).toBe(true),
  );
  expect(
    requests.filter((path) => path.startsWith("/api/trips?")),
  ).toHaveLength(1);
  const request = new URL(
    requests.find((path) => path.startsWith("/api/trips?"))!,
    "http://localhost",
  ).searchParams;
  expect(request.get("from")).toBe("2020-03-01");
  expect(request.has("offset")).toBe(false);
  await user.click(
    screen.getByRole("checkbox", { name: "Inferred trip connections" }),
  );
  expect(
    screen.queryByRole("region", { name: "Inferred trip connection details" }),
  ).toBeNull();
  expect(withTripEndpoints([point], [trip])).toHaveLength(2);
  expect(
    tripDisplayPath({
      ...trip,
      display_path: [
        [37.5, 179.8],
        [38.2, 180.2],
      ],
    }),
  ).toEqual([
    [37.5, 179.8],
    [38.2, 180.2],
  ]);
});

it("keeps recorded stops usable when trips are unavailable and offers a separate retry", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  let failed = true;
  vi.mocked(fetch).mockImplementation(async (input, options) =>
    String(input).startsWith("/api/trips?") && failed
      ? new Response(JSON.stringify({ detail: "Trip index unavailable" }), {
          status: 503,
        })
      : original(input, options),
  );
  const user = userEvent.setup();
  render(
    <MapWorkspace
      filters={filters}
      refreshKey={0}
      onOpenEvent={vi.fn()}
      onShowTimeline={vi.fn()}
    />,
  );
  expect(await screen.findByText(/Trip connections unavailable/)).toBeTruthy();
  expect(
    await screen.findByRole("button", { name: "View location evidence" }),
  ).toBeTruthy();
  failed = false;
  await user.click(screen.getByRole("button", { name: "Retry connections" }));
  await waitFor(() =>
    expect(screen.queryByText(/Trip connections unavailable/)).toBeNull(),
  );
  expect(document.querySelector('script[src*="google"]')).toBeNull();
});

it("defaults to configured Google, respects hidden basemap, and preserves manual choices", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, options) =>
    String(input) === "/api/map-config"
      ? new Response(
          JSON.stringify({
            provider: "google",
            google_maps_configured: true,
            google_maps_api_key: "synthetic-browser-key",
          }),
        )
      : original(input, options),
  );
  localStorage.setItem("biography.basemap", "hidden");
  const user = userEvent.setup();
  const rendered = render(
    <MapWorkspace
      filters={filters}
      refreshKey={0}
      onOpenEvent={vi.fn()}
      onShowTimeline={vi.fn()}
    />,
  );
  await screen.findByRole("checkbox", { name: "Google Maps basemap" });
  const select = screen.getByRole("combobox", { name: "Map provider" });
  expect((select as HTMLSelectElement).value).toBe("google");
  expect(googleCanvas).not.toHaveBeenCalled();
  expect(tileLayer).not.toHaveBeenCalled();
  await user.click(
    screen.getByRole("checkbox", { name: "Google Maps basemap" }),
  );
  expect(googleCanvas).toHaveBeenCalled();
  await user.selectOptions(select, "openstreetmap");
  const calls = googleCanvas.mock.calls.length;
  rendered.rerender(
    <MapWorkspace
      filters={{ ...filters, q: "different" }}
      refreshKey={0}
      onOpenEvent={vi.fn()}
      onShowTimeline={vi.fn()}
    />,
  );
  expect((select as HTMLSelectElement).value).toBe("openstreetmap");
  expect(googleCanvas).toHaveBeenCalledTimes(calls);
  await user.selectOptions(select, "google");
  await user.click(
    screen.getByRole("button", { name: "Simulate Google failure" }),
  );
  expect((select as HTMLSelectElement).value).toBe("openstreetmap");
  expect(
    screen.getByText(/OpenStreetMap is active with the same recorded stops/),
  ).toBeTruthy();
  expect(
    Object.values(localStorage).some((value) =>
      String(value).includes("synthetic-browser-key"),
    ),
  ).toBe(false);
});

it("shows a disabled Google choice and setup hint when no key is configured", async () => {
  render(
    <MapWorkspace
      filters={filters}
      refreshKey={0}
      onOpenEvent={vi.fn()}
      onShowTimeline={vi.fn()}
    />,
  );
  await screen.findByRole("button", { name: "View location evidence" });
  const option = screen.getByRole("option", {
    name: "Google Maps · not configured",
  }) as HTMLOptionElement;
  expect(option.disabled).toBe(true);
  expect(
    screen.getByText(/Add a localhost-restricted browser key/),
  ).toBeTruthy();
  expect(googleCanvas).not.toHaveBeenCalled();
});

it("keeps an explicit provider choice across map view remounts without storing a key", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, options) =>
    String(input) === "/api/map-config"
      ? new Response(
          JSON.stringify({
            provider: "google",
            google_maps_configured: true,
            google_maps_api_key: "synthetic-browser-key",
          }),
        )
      : original(input, options),
  );
  const user = userEvent.setup();
  const first = render(
    <MapWorkspace
      filters={filters}
      refreshKey={0}
      onOpenEvent={vi.fn()}
      onShowTimeline={vi.fn()}
    />,
  );
  await screen.findByRole("checkbox", { name: "Google Maps basemap" });
  await user.selectOptions(
    screen.getByRole("combobox", { name: "Map provider" }),
    "openstreetmap",
  );
  await user.click(
    screen.getByRole("checkbox", { name: "OpenStreetMap basemap" }),
  );
  const calls = googleCanvas.mock.calls.length;
  first.unmount();
  render(
    <MapWorkspace
      filters={filters}
      refreshKey={0}
      onOpenEvent={vi.fn()}
      onShowTimeline={vi.fn()}
    />,
  );
  await screen.findByRole("button", { name: "View location evidence" });
  await waitFor(() =>
    expect(
      (
        screen.getByRole("combobox", {
          name: "Map provider",
        }) as HTMLSelectElement
      ).value,
    ).toBe("openstreetmap"),
  );
  expect(
    (
      screen.getByRole("checkbox", {
        name: "OpenStreetMap basemap",
      }) as HTMLInputElement
    ).checked,
  ).toBe(false);
  expect(googleCanvas).toHaveBeenCalledTimes(calls);
  expect(
    Object.values(localStorage).some((value) =>
      String(value).includes("synthetic-browser-key"),
    ),
  ).toBe(false);
});

it("paginates same-day records using has_more when the total is unavailable", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, options) => {
    const path = String(input);
    if (!path.startsWith("/api/events?")) return original(input, options);
    requests.push(path);
    const offset = Number(
      new URL(path, "http://localhost").searchParams.get("offset"),
    );
    return new Response(
      JSON.stringify({
        items: [
          {
            id: "day-record",
            title: "Day evidence with an unknown total",
            body: "A retained source record.",
            source: "Calendar",
            category: "activity",
          },
        ],
        total: null,
        total_status: "pending",
        counted_at: null,
        has_more: offset === 0,
        limit: 20,
        offset,
      }),
    );
  });
  const user = userEvent.setup();
  render(
    <MapWorkspace
      filters={filters}
      refreshKey={0}
      onOpenEvent={vi.fn()}
      onShowTimeline={vi.fn()}
    />,
  );
  await screen.findByText("Day evidence with an unknown total");
  expect(screen.getByText("1 shown")).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Next", exact: true }));
  await waitFor(() =>
    expect(
      requests.some(
        (path) => path.startsWith("/api/events?") && path.includes("offset=20"),
      ),
    ).toBe(true),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "Next", exact: true }),
    ).toBeNull(),
  );
  expect(
    screen.getByRole("button", { name: "Previous", exact: true }),
  ).toBeTruthy();
});

it("pages every loaded point and connection without changing selection or requesting another provider", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  const points = Array.from({length: 405}, (_, index) => ({...point, id: `point-${index}`, event_id: `event-${index}`, title: `Loaded point ${index + 1}`}));
  const legs = Array.from({length: 405}, (_, index) => ({id: `leg-${index}`, trip_id: "trip", inferred: true, geometry_type: "straight_line", from: {...point, title: `Stop ${index + 1}`}, to: {...point, title: "Destination"}, distance_km: index + 1, gap_hours: 1}));
  vi.mocked(fetch).mockImplementation(async (input, options) => {
    const path = String(input);
    if (path.startsWith("/api/map?") || path.startsWith("/api/trips?")) {
      requests.push(path);
      return Response.json(path.startsWith("/api/map?") ? {items: points, total: 2405} : {items: legs, total: 9000, truncated: true});
    }
    return original(input, options);
  });
  const user = userEvent.setup();
  const props = {filters, refreshKey: 0, onOpenEvent: vi.fn(), onShowTimeline: vi.fn()};
  const view = render(<MapWorkspace {...props}/>);
  const list = await screen.findByRole("region", {name: "Recorded locations list"});
  await screen.findByText("A post on the same day");
  const tiles = tileLayer.mock.calls.length, mapCalls = requests.filter(path => path.startsWith("/api/map?")).length;
  expect(within(list).getAllByRole("button")).toHaveLength(200);
  await user.click(screen.getByRole("button", {name: "Next loaded locations"}));
  await user.click(within(list).getByRole("button", {name: /^Loaded point 201/}));
  await user.click(screen.getByRole("button", {name: "Next loaded locations"}));
  expect(within(list).getAllByRole("button")).toHaveLength(5);
  expect(within(list).getByText("Loaded point 405")).toBeTruthy();
  expect((screen.getByRole("button", {name: "Next loaded locations"}) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole("heading", {name: "Loaded point 201"})).toBeTruthy();
  await user.click(screen.getByRole("button", {name: "Previous loaded locations"}));
  expect(within(list).getByText("Loaded point 201")).toBeTruthy();
  await user.click(screen.getByText("Browse 405 inferred connections"));
  await user.click(screen.getByRole("button", {name: "Next loaded connections"}));
  await user.click(screen.getByRole("button", {name: /Stop 201 → Destination/}));
  await user.click(screen.getByRole("button", {name: "Next loaded connections"}));
  expect(screen.getByRole("button", {name: /Stop 405 → Destination/})).toBeTruthy();
  expect(screen.getByRole("heading", {name: "Stop 201"})).toBeTruthy();
  expect(screen.getByText(/First 405 shown/)).toBeTruthy();
  expect(tileLayer).toHaveBeenCalledTimes(tiles);
  expect(requests.filter(path => path.startsWith("/api/map?"))).toHaveLength(mapCalls);
  expect(requests.filter(path => path.startsWith("/api/trips?"))).toHaveLength(1);
  await user.selectOptions(screen.getByLabelText("Recorded locations direction"), "asc");
  await waitFor(() => expect(requests.some(path => path.startsWith("/api/map?") && path.includes("direction=asc") && path.includes("offset=0"))).toBe(true));
  expect(within(list).getByText("Loaded point 1")).toBeTruthy();
  expect(screen.getByRole("button", {name: /Stop 1 → Destination/})).toBeTruthy();
  await user.click(screen.getByRole("button", {name: "Next loaded locations"}));
  await user.click(screen.getByRole("button", {name: "Next loaded connections"}));
  await user.click(screen.getByRole("button", {name: "Next points", exact: true}));
  await waitFor(() => expect(requests.some(path => path.startsWith("/api/map?") && path.includes("offset=2000"))).toBe(true));
  expect(within(list).getByText("Loaded point 1")).toBeTruthy();
  expect(screen.getByRole("button", {name: /Stop 1 → Destination/})).toBeTruthy();
  await user.click(screen.getByRole("button", {name: "Next loaded locations"}));
  await user.click(screen.getByRole("button", {name: "Next loaded connections"}));
  view.rerender(<MapWorkspace {...props} filters={{...filters,q:"narrow"}}/>);
  await waitFor(() => expect(requests.some(path => path.startsWith("/api/map?") && path.includes("q=narrow") && path.includes("offset=0"))).toBe(true));
  expect(within(list).getByText("Loaded point 1")).toBeTruthy();
  expect(screen.getByRole("button", {name: /Stop 1 → Destination/})).toBeTruthy();
}, 20000);

it("resets same-day paging when Date or Import order sorting changes", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, options) => {
    if (!String(input).startsWith("/api/events?")) return original(input, options);
    requests.push(String(input));
    return Response.json({items: [{id:"same-day",title:"Same-day sorted record",source:"Synthetic",category:"post"}],total:100,limit:20,offset:0});
  });
  const user = userEvent.setup();
  render(<MapWorkspace filters={filters} refreshKey={0} onOpenEvent={vi.fn()} onShowTimeline={vi.fn()}/>);
  await screen.findByText("Same-day sorted record");
  await user.click(screen.getByRole("button", {name:"Next",exact:true}));
  await waitFor(() => expect(requests.some(path => path.startsWith("/api/events?") && path.includes("offset=20"))).toBe(true));
  await user.selectOptions(screen.getByLabelText("Same-day records sort"), "imported");
  await user.selectOptions(screen.getByLabelText("Same-day records direction"), "desc");
  await waitFor(() => expect(requests.some(path => path.startsWith("/api/events?") && path.includes("sort=imported") && path.includes("direction=desc") && path.includes("offset=0"))).toBe(true));
  expect(requests.filter(path => path.startsWith("/api/map?"))).toHaveLength(1);
});
