import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../src/App";

vi.mock("../src/MapWorkspace", () => ({
  default: ({ filters }: { filters: { category: string } }) => (
    <p>Map type: {filters.category || "all"}</p>
  ),
}));
vi.mock("../src/DailyWorkspace", () => ({
  default: ({ filters }: { filters: { category: string } }) => (
    <p>Day type: {filters.category || "all"}</p>
  ),
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("gets day types from the projection and keeps raw map/record types in their own domain", async () => {
  const paths: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      paths.push(path);
      const data =
        path === "/api/overview"
          ? {
              total_events: 99,
              sources_count: 0,
              files_count: 0,
              stored_bytes: 0,
              date_range: { start: null, end: null },
              activity: [],
              categories: [{ name: "raw_sensor_payload", count: 99 }],
            }
          : path === "/api/daily/categories"
            ? {
                items: [
                  {
                    name: "derived_measurement_test",
                    category: "derived_measurement_test",
                    count: 77,
                  },
                ],
                projection: { status: "paused" },
              }
            : { items: [], total: 0, limit: 40, offset: 0 };
      return new Response(JSON.stringify(data));
    }),
  );
  const user = userEvent.setup();
  render(<App />);
  const modes = within(screen.getByRole("group", { name: "Archive view" }));
  const select = screen.getByRole("combobox", { name: "Filter by type" });
  await screen.findByRole("option", { name: "Raw sensor payload (99)" });
  await user.selectOptions(select, "raw_sensor_payload");
  expect(screen.getByText("Map type: raw_sensor_payload")).toBeTruthy();
  await user.click(modes.getByRole("button", { name: "Records", exact: true }));
  expect((select as HTMLSelectElement).value).toBe("raw_sensor_payload");
  await waitFor(() =>
    expect(
      paths.some(
        (path) =>
          path.startsWith("/api/events?") &&
          path.includes("category=raw_sensor_payload"),
      ),
    ).toBe(true),
  );
  await user.click(modes.getByRole("button", { name: "Days", exact: true }));
  await screen.findByRole("option", { name: "Derived measurement test (77)" });
  expect(
    screen.queryByRole("option", { name: "Raw sensor payload (99)" }),
  ).toBeNull();
  expect((select as HTMLSelectElement).value).toBe("");
  await user.selectOptions(select, "derived_measurement_test");
  expect(screen.getByText("Day type: derived_measurement_test")).toBeTruthy();
  await user.click(modes.getByRole("button", { name: "Map", exact: true }));
  expect((select as HTMLSelectElement).value).toBe("");
  expect(
    screen.getByRole("option", { name: "Raw sensor payload (99)" }),
  ).toBeTruthy();
  expect(paths.filter((path) => path === "/api/daily/categories")).toHaveLength(
    1,
  );
  expect(
    paths
      .filter((path) => path.startsWith("/api/events?"))
      .every((path) => !path.includes("derived_measurement_test")),
  ).toBe(true);
});
