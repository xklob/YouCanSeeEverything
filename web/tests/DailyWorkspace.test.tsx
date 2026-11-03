import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DailyWorkspace from "../src/DailyWorkspace";

const filters = {
  q: "",
  source: "actual-source-id",
  category: "",
  from: "",
  to: "",
  undated: false,
};
const summary = {
  day: "2024-04-05",
  date: "2024-04-05",
  observed_records: 10,
  raw_samples: 6,
  substantive_records: 3,
  technical_records: 1,
  display_groups: 2,
  measurement_groups: 2,
  source_count: 2,
  date_bases: [
    { basis: "source_local", count: 4 },
    { basis: "utc", count: 6 },
  ],
};
const projection = {
  status: "ready",
  watermark_rowid: 100,
  current_catalog_max_rowid: 105,
  caught_up_to_watermark: true,
  append_backlog_rowid_span: 5,
};
const rollup = {
  id: "stream-one",
  day: summary.day,
  source_id: "actual-source-id",
  source: "Watch export",
  category: "measurement",
  metric: "steps",
  unit: "steps",
  device: "Watch label",
  aggregation: "sum",
  sample_count: 3,
  numeric_count: 3,
  min: 1,
  max: 3,
  sample_mean: 2,
  sum: 6,
  latest: 3,
  first_at: "2024-04-05T00:00:00Z",
  last_at: "2024-04-05T01:00:00Z",
  evidence_ids: ["sample-one"],
  watermark: 100,
};
const record = {
  id: "readable-one",
  title: "A retained conversation",
  body: "Source text for this conversation.",
  occurred_at: summary.date,
  source_id: "actual-source-id",
  source: "Original export",
  source_label: "Readable source label",
  category: "conversation",
  display_class: "conversation",
  duplicate_count: 1,
  source_record_count: 2,
  media: [
    {
      file_id: "image",
      url: "/api/files/image/media",
      mime: "image/png",
      kind: "image",
      caption: "Attached source image",
    },
  ],
};
let requests: { path: string; options?: RequestInit }[];
beforeEach(() => {
  localStorage.clear();
  requests = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      requests.push({ path, options });
      let data;
      if (path.startsWith("/api/days?"))
        data = {
          items: [summary, { ...summary, day: "undated", date: null }],
          total: 2,
          limit: 31,
          offset: 0,
          projection,
        };
      else if (path.startsWith("/api/days/")) {
        const undated = path.includes("/undated?");
        data = {
          day: undated ? "undated" : summary.day,
          summary: { ...summary, day: undated ? "undated" : summary.day },
          items: [
            { ...record, occurred_at: undated ? null : record.occurred_at },
          ],
          total: 1,
          limit: 20,
          offset: 0,
          rollups: undated ? [] : [rollup],
          projection,
        };
      } else if (path.startsWith("/api/rollups/"))
        data = {
          items: [
            {
              ...record,
              id: "sample-one",
              title: "Retained measurement sample",
              body: "Observed value: 2 steps.",
              display_class: "measurement",
              media: [],
            },
          ],
          total: 3,
          limit: 20,
          offset: 0,
          rollup,
          projection,
        };
      else if (path.startsWith("/api/summaries?"))
        data = {
          status: "missing",
          result: null,
          current_coverage: {
            total_events_in_scope: 10,
            selected_events: 3,
            candidate_events: 5,
            undated_events_excluded: 1,
            sources: [],
            retrieval_limited: true,
            time_basis: "source_dates",
            limitations: [],
          },
        };
      else throw new Error(`Unexpected request ${path}`);
      return new Response(JSON.stringify(data));
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("keeps source record counts, validated metric sums, date basis, and readable evidence distinct", async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  render(
    <DailyWorkspace filters={filters} refreshKey={0} onOpenEvent={open} />,
  );
  expect(
    await screen.findByRole("heading", { name: "Day summary" }),
  ).toBeTruthy();
  expect(
    screen.getByText(/2 readable groups and 2 measurement streams/),
  ).toBeTruthy();
  expect(screen.getByText("Source local: 4 records")).toBeTruthy();
  expect(screen.getByAltText("Attached source image")).toBeTruthy();
  expect(screen.getByText(/1 additional proven duplicate/)).toBeTruthy();
  await user.click(
    screen.getByRole("button", { name: /A retained conversation/ }),
  );
  expect(open).toHaveBeenCalledWith("readable-one");
  expect(requests.some((item) => item.path.startsWith("/api/rollups/"))).toBe(
    false,
  );
  await user.click(screen.getByText(/Steps: inspect raw samples/));
  expect(
    await screen.findByRole("button", { name: /Retained measurement sample/ }),
  ).toBeTruthy();
  expect(
    requests.find((item) => item.path.startsWith("/api/days?"))?.path,
  ).toContain("source=actual-source-id");
  expect(requests.every((item) => item.options?.method !== "POST")).toBe(true);
});

it("keeps undated evidence outside chronology and lets users opt into technical traces", async () => {
  const user = userEvent.setup();
  render(
    <DailyWorkspace filters={filters} refreshKey={0} onOpenEvent={vi.fn()} />,
  );
  await screen.findByRole("heading", { name: "Day summary" });
  await user.click(
    screen.getByRole("checkbox", { name: /Include technical traces/ }),
  );
  await waitFor(() =>
    expect(
      requests.some((item) => item.path.includes("include_technical=true")),
    ).toBe(true),
  );
  await user.click(screen.getByRole("button", { name: /Undated records/ }));
  expect(
    await screen.findByRole("heading", { name: "Undated records" }),
  ).toBeTruthy();
  expect(
    (
      screen.getByRole("button", {
        name: "Previous calendar day",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  await waitFor(() =>
    expect(
      requests.some((item) => item.path.startsWith("/api/days/undated?")),
    ).toBe(true),
  );
  expect(
    screen.getByText(/without a specific trustworthy day stay here/),
  ).toBeTruthy();
});

it("shows source conflicts and distinguishes occurrences, variants and duplicate copies without statistics", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, options) => {
    const response = await original(input, options);
    if (!String(input).startsWith("/api/days/")) return response;
    const data = await response.json();
    data.rollups = [
      {
        ...rollup,
        sample_count: 2,
        occurrence_count: 2,
        variant_count: 3,
        source_record_count: 4,
        duplicate_count: 1,
        conflicting_occurrences: 1,
        numeric_count: 1,
        min: null,
        max: null,
        sample_mean: null,
        sum: null,
        latest: null,
        notes: [
          "Two retained variants report different values for one occurrence.",
        ],
      },
    ];
    return new Response(JSON.stringify(data));
  });
  const user = userEvent.setup();
  render(
    <DailyWorkspace filters={filters} refreshKey={0} onOpenEvent={vi.fn()} />,
  );
  const heading = await screen.findByRole("heading", { name: "Steps" });
  const card = within(heading.closest("article")!);
  expect(
    card.getByText("Source values conflict; statistics omitted."),
  ).toBeTruthy();
  expect(
    card.getByText(
      "2 occurrences · 3 variants · 4 retained records · 1 duplicate copy",
    ),
  ).toBeTruthy();
  expect(card.queryByText("6")).toBeNull();
  const notes = card.getByText("Calculation notes").closest("details")!;
  expect(notes.open).toBe(false);
  await user.click(card.getByText("Calculation notes"));
  expect(notes.open).toBe(true);
  expect(
    card.getByText(
      "Two retained variants report different values for one occurrence.",
    ),
  ).toBeTruthy();
  expect(heading.closest("article")!.textContent).not.toContain("{");
});

it("requests sorted days on the server, preserves filters and restores its preference", async () => {
  const user = userEvent.setup();
  const props = {filters, refreshKey: 0, onOpenEvent: vi.fn()};
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, options) => {
    const response = await original(input, options);
    if (String(input).startsWith("/api/days?")) {
      const value = await response.json(); value.total = 40;
      return Response.json(value);
    }
    return response;
  });
  const view = render(<DailyWorkspace {...props} />);
  await screen.findByText("A retained conversation");
  await user.click(screen.getByRole("button", {name: "Next observed days"}));
  await waitFor(() => expect(requests.some(item => item.path.includes("offset=31"))).toBe(true));
  await user.selectOptions(screen.getByLabelText("Days sort"), "records");
  await user.selectOptions(screen.getByLabelText("Days direction"), "asc");
  await waitFor(() => expect(requests.some(item => {
    const url = new URL(item.path, "http://localhost");
    return url.pathname === "/api/days" && url.searchParams.get("sort") === "records" && url.searchParams.get("direction") === "asc" && url.searchParams.get("offset") === "0" && url.searchParams.get("source") === filters.source;
  })).toBe(true));
  expect(requests.filter(item => item.path.startsWith("/api/days/")).every(item => !new URL(item.path, "http://localhost").searchParams.has("sort"))).toBe(true);
  view.unmount(); requests.length = 0;
  render(<DailyWorkspace {...props} />);
  await screen.findByText("A retained conversation");
  expect((screen.getByLabelText("Days sort") as HTMLSelectElement).value).toBe("records");
  expect((screen.getByLabelText("Days direction") as HTMLSelectElement).value).toBe("asc");
});
