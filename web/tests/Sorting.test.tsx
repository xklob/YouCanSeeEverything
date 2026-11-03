import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../src/App";
import { SortControls, sortItems, useSortPreference, type SortPreference } from "../src/sorting";

vi.mock("../src/MapWorkspace", () => ({ default: () => null }));
vi.mock("../src/SavedSummaries", () => ({ default: () => null }));
beforeEach(() => localStorage.clear());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("sorts unknown and nonfinite measurements last in either direction without mutating input or reordering ties", () => {
  const items = [{ id: "unknown", value: null }, { id: "high", value: 10 }, { id: "zero", value: 0 }, { id: "equal", value: 10 }, { id: "invalid", value: NaN }];
  const preference: SortPreference = ["records", "asc", vi.fn(), vi.fn()];
  expect(sortItems(items, preference, row => row.value).map(row => row.id)).toEqual(["zero", "high", "equal", "unknown", "invalid"]);
  expect(sortItems(items, ["records", "desc", preference[2], preference[3]], row => row.value).map(row => row.id)).toEqual(["high", "equal", "zero", "unknown", "invalid"]);
  expect(items[0].id).toBe("unknown");
});

it("validates stored fields and directions, persists independent view preferences, and tolerates unavailable storage", async () => {
  const user = userEvent.setup();
  const Harness = ({ view = "example" }: { view?: string }) => {
    const preference = useSortPreference(view, "date", ["date", "name"], "desc");
    return <SortControls label={view} options={[["date", "Date"], ["name", "Name"]]} value={preference} />;
  };
  localStorage.setItem("archive-sort:example", JSON.stringify(["arbitrary-field", "sideways"]));
  let mounted = render(<Harness />);
  expect((screen.getByLabelText("example sort") as HTMLSelectElement).value).toBe("date");
  await user.selectOptions(screen.getByLabelText("example sort"), "name");
  await user.selectOptions(screen.getByLabelText("example direction"), "asc");
  expect(JSON.parse(localStorage.getItem("archive-sort:example")!)).toEqual(["name", "asc"]);
  mounted.unmount();
  mounted = render(<Harness />);
  expect((screen.getByLabelText("example sort") as HTMLSelectElement).value).toBe("name");
  mounted.unmount();
  mounted = render(<Harness view="other" />);
  expect((screen.getByLabelText("other sort") as HTMLSelectElement).value).toBe("date");
  mounted.unmount();
  const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Unavailable"); });
  const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Unavailable"); });
  mounted = render(<Harness />);
  await user.selectOptions(screen.getByLabelText("example sort"), "name");
  expect((screen.getByLabelText("example sort") as HTMLSelectElement).value).toBe("name");
  mounted.unmount(); get.mockRestore(); set.mockRestore();
});

const source = (id: string, name: string, event_count: number | null) => ({ id, name, event_count, file_count: 2, bytes: 100, status: "ready" });
const run = (name: string) => ({ id: name, source: name, status: "completed", total_files: 1, completed_files: 1, total_bytes: 100, completed_bytes: 100, events: 1, started_at: "2024-01-01" });
const task = (prompt: string) => ({ id: prompt, prompt, kind: "ask", status: "completed", created_at: "2024-01-01" });
let paths: string[];
function mockLibrary() {
  paths = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input); paths.push(path);
    const url = new URL(path, "http://localhost");
    let data: unknown = { items: [] };
    if (url.pathname === "/api/overview") data = { sources_count: 3, files_count: 6, stored_bytes: 300, total_events: null, date_range: {}, activity: [], categories: [] };
    if (url.pathname === "/api/sources") data = { items: [source("unknown", "Zulu unknown", null), source("large", "Beta known", 10), source("zero", "Alpha zero", 0)] };
    if (url.pathname === "/api/imports" || url.pathname === "/api/jobs") {
      const offset = Number(url.searchParams.get("offset") || 0);
      const sorted = url.searchParams.has("sort");
      const name = sorted && !offset ? "Alpha server result" : "Zulu server result";
      data = { items: [url.pathname.endsWith("imports") ? run(name) : task(name)], limit: 1, offset, total: null, has_more: offset === 0 };
    }
    return new Response(JSON.stringify(data));
  }));
}

it("sorts the full source library and leaves unknown counts last in both directions", async () => {
  mockLibrary(); const user = userEvent.setup(); render(<App />);
  await user.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Sources", exact: true }));
  await screen.findByRole("heading", { name: "Beta known" });
  const names = () => [...document.querySelectorAll(".source-card h3")].map(node => node.textContent);
  expect(names()).toEqual(["Alpha zero", "Beta known", "Zulu unknown"]);
  await user.selectOptions(screen.getByLabelText("Sources sort"), "records");
  expect(names()).toEqual(["Alpha zero", "Beta known", "Zulu unknown"]);
  await user.selectOptions(screen.getByLabelText("Sources direction"), "desc");
  expect(names()).toEqual(["Beta known", "Alpha zero", "Zulu unknown"]);
});

it("requests globally sorted import pages and resets pagination when sorting changes", async () => {
  mockLibrary(); const user = userEvent.setup(); render(<App />);
  await user.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Sources", exact: true }));
  await screen.findByLabelText("Import activity sort");
  await waitFor(() => expect((screen.getByRole("button", { name: "Next imports" }) as HTMLButtonElement).disabled).toBe(false));
  await user.click(screen.getByRole("button", { name: "Next imports" }));
  await waitFor(() => expect(paths.some(path => path.includes("/imports?") && path.includes("offset=1"))).toBe(true));
  await user.selectOptions(screen.getByLabelText("Import activity sort"), "name");
  await screen.findByText("Alpha server result");
  const latest = new URL(paths.filter(path => path.includes("/imports?")).at(-1)!, "http://localhost");
  expect(latest.searchParams.get("sort")).toBe("name");
  expect(latest.searchParams.get("offset")).toBe("0");
  expect(screen.getByText(/Browse order only/)).toBeTruthy();
});

it("requests globally sorted assistant pages and ignores obsolete responses after a sort change", async () => {
  mockLibrary(); const user = userEvent.setup();
  const original = globalThis.fetch;
  let release: ((value: Response) => void) | undefined;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    const path = String(input);
    if (path.includes("/jobs?") && path.includes("offset=1")) {
      paths.push(path); return new Promise<Response>(resolve => { release = resolve; });
    }
    return original(input, options);
  }));
  render(<App />);
  await user.click(within(screen.getByRole("navigation")).getByRole("button", { name: /^Ask/ }));
  await screen.findByRole("heading", { name: "Zulu server result" });
  await user.click(screen.getByRole("button", { name: "Next activity" }));
  await waitFor(() => expect(release).toBeTruthy());
  await user.selectOptions(screen.getByLabelText("Assistant activity sort"), "prompt");
  await screen.findByRole("heading", { name: "Alpha server result" });
  await act(async () => release!(new Response(JSON.stringify({ items: [task("Obsolete page")], limit: 1, offset: 1, has_more: false }))));
  expect(screen.queryByRole("heading", { name: "Obsolete page" })).toBeNull();
  const latest = new URL(paths.filter(path => path.includes("/jobs?")).at(-1)!, "http://localhost");
  expect(latest.searchParams.get("sort")).toBe("prompt");
  expect(latest.searchParams.get("offset")).toBe("0");
});
