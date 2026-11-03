import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TripsWorkspace from "../src/TripsWorkspace";

const first = { id: "point-a", event_id: "event-a", title: "First stop", latitude: 0,
  longitude: 0, occurred_at: "2024-01-01T00:00:00Z", end_at: "2024-01-01T01:00:00Z", provenance_total: 2 };
const last = { id: "point-b", event_id: "event-b", title: "Last stop", latitude: 0,
  longitude: 2, occurred_at: "2024-01-01T05:00:00Z", end_at: "2024-01-01T06:00:00Z", provenance_total: 1 };
const journey = { id: "journey-one", source_id: "source-one", source: "Synthetic history",
  departure_at: first.end_at, arrival_at: last.occurred_at, legs: 75, distance_km: 222,
  max_gap_hours: 4, long_distance: true, represented_stops: 76, first_stop: first,
  last_stop: last, summary_complete: true };
const leg = { id: "leg-one", trip_id: journey.id, source: journey.source, from: first, to: last,
  departure_at: first.end_at, arrival_at: last.occurred_at, distance_km: 222, gap_hours: 4,
  inferred: true, geometry_type: "straight_line" };
let requests: URL[];
let failList: boolean;
let empty: boolean;
beforeEach(() => {
  localStorage.clear();
  requests = []; failList = false; empty = false;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost:8000"); requests.push(url);
    if (url.pathname === "/api/journeys") {
      if (failList) return new Response(JSON.stringify({ detail: "Trips unavailable" }), { status: 503 });
      const offset = Number(url.searchParams.get("offset") || 0);
      return new Response(JSON.stringify({ items: empty ? [] : [{ ...journey, id: offset ? "journey-two" : journey.id }],
        total: empty ? 0 : 21, limit: 20, offset, coverage_complete: true,
        sources: [{ id: "source-one", name: "Synthetic history" }] }));
    }
    if (url.pathname.startsWith("/api/journeys/")) {
      const offset = Number(url.searchParams.get("offset") || 0);
      return new Response(JSON.stringify({ journey: { ...journey, id: url.pathname.split("/").pop() },
        items: [{ ...leg, id: offset ? "leg-later" : leg.id }], total: 75, limit: 50, offset,
        truncated: offset === 0, complete: false, coverage_complete: true }));
    }
    throw new Error(`Unexpected local endpoint: ${url.pathname}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("opens endpoint evidence and the existing map without loading a map provider", async () => {
  const user = userEvent.setup(); const onOpenEvent = vi.fn(); const onShowMap = vi.fn();
  render(<TripsWorkspace refreshKey={0} onOpenEvent={onOpenEvent} onShowMap={onShowMap} />);
  await screen.findByText("2 source references");
  await user.click(screen.getAllByRole("button", { name: /Open record/ })[0]);
  expect(onOpenEvent).toHaveBeenCalledWith("event-a");
  await user.click(screen.getByRole("button", { name: "View on map" }));
  expect(onShowMap).toHaveBeenCalledWith({ from: "2024-01-01", to: "2024-01-01", sourceId: "source-one", tripId: "journey-one" });
  expect(screen.getByText(/This page is part of the trip/)).toBeTruthy();
  expect(screen.getByText(/222 km straight-line/)).toBeTruthy();
  expect(requests.every((url) => url.origin === "http://localhost:8000")).toBe(true);
  expect(document.querySelector('script[src*="google"]')).toBeNull();
});

it("paginates groups and connections independently and resets group paging on filters", async () => {
  const user = userEvent.setup();
  render(<TripsWorkspace refreshKey={0} onOpenEvent={vi.fn()} onShowMap={vi.fn()} />);
  await screen.findByText("2 source references");
  await user.click(screen.getByRole("button", { name: "Next connections" }));
  await waitFor(() => expect(requests.some((url) => url.pathname.endsWith("journey-one") && url.searchParams.get("offset") === "50")).toBe(true));
  await user.click(screen.getByRole("button", { name: "Next trips" }));
  await waitFor(() => expect(requests.some((url) => url.pathname === "/api/journeys" && url.searchParams.get("offset") === "20")).toBe(true));
  await screen.findByRole("button", { name: "View on map" });
  await user.selectOptions(screen.getByRole("combobox", { name: "Trip distance" }), "long_distance");
  await waitFor(() => expect(requests.some((url) => url.pathname === "/api/journeys" && url.searchParams.get("offset") === "0" && url.searchParams.get("kind") === "long_distance")).toBe(true));
});

it("keeps empty coverage explicit without inventing trips", async () => {
  empty = true;
  render(<TripsWorkspace refreshKey={0} onOpenEvent={vi.fn()} onShowMap={vi.fn()} />);
  await screen.findByText("No trips in this selection");
  expect(screen.queryByRole("button", { name: "View on map" })).toBeNull();
  expect(requests.filter((url) => url.pathname.startsWith("/api/journeys/")).length).toBe(0);
});

it("recovers from a failed request without a page reload", async () => {
  failList = true; const user = userEvent.setup();
  render(<TripsWorkspace refreshKey={0} onOpenEvent={vi.fn()} onShowMap={vi.fn()} />);
  await screen.findByRole("alert"); failList = false;
  await user.click(screen.getByRole("button", { name: "Retry trips" }));
  await screen.findByText("2 source references");
});

it("sorts the full trip query, resets both pages and keeps its preference after remount", async () => {
  const user = userEvent.setup();
  const props = {refreshKey: 0, onOpenEvent: vi.fn(), onShowMap: vi.fn()};
  const view = render(<TripsWorkspace {...props} />);
  await screen.findByText("2 source references");
  await user.click(screen.getByRole("button", {name: "Next trips"}));
  await waitFor(() => expect(requests.some(url => url.searchParams.get("offset") === "20")).toBe(true));
  await user.selectOptions(screen.getByLabelText("Trips sort"), "distance");
  await waitFor(() => expect(requests.some(url => url.pathname === "/api/journeys" && url.searchParams.get("sort") === "distance" && url.searchParams.get("offset") === "0" && url.searchParams.get("direction") === "desc")).toBe(true));
  await user.selectOptions(screen.getByLabelText("Trips direction"), "asc");
  await waitFor(() => expect(requests.some(url => url.pathname === "/api/journeys" && url.searchParams.get("sort") === "distance" && url.searchParams.get("direction") === "asc")).toBe(true));
  view.unmount(); requests.length = 0;
  render(<TripsWorkspace {...props} />);
  await screen.findByText("2 source references");
  expect((screen.getByLabelText("Trips sort") as HTMLSelectElement).value).toBe("distance");
  expect(requests.find(url => url.pathname === "/api/journeys")?.searchParams.get("direction")).toBe("asc");
  expect(requests.filter(url => url.pathname.startsWith("/api/journeys/")).every(url => !url.searchParams.has("sort") && url.searchParams.get("offset") === "0")).toBe(true);
});
