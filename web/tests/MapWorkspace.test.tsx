import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import MapWorkspace, { recordedDay } from "../src/MapWorkspace";
import MediaGallery from "../src/MediaGallery";
import type { MapLocation } from "../src/api";

const { tileLayer, maps } = vi.hoisted(() => ({
  tileLayer: vi.fn(),
  maps: [] as unknown[],
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
    ])
      self[key] = vi.fn(() => self);
    self.getLatLng = () => ({ lat: 38, lng: -77 });
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
  requests = [];
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
      const data = path.startsWith("/api/map?")
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
  expect(screen.getByText(/their location is not inferred/)).toBeTruthy();
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
