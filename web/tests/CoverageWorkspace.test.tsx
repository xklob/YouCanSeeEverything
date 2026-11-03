import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CoverageWorkspace from "../src/CoverageWorkspace";

const report = {
  scope: "observed_imported_records",
  continuous_coverage_asserted: false,
  generated_at: "2026-11-03T00:00:00Z",
  totals: {
    sources: 1,
    files: 13,
    bytes: 1300,
    parsed_files: 5,
    metadata_only_files: 2,
    retained_only_files: 1,
    review_files: 1,
    events: null,
    dated_events: null,
    undated_events: null,
  },
  sources: [
    {
      id: "actual-source-id",
      name: "Example archive",
      status: "importing",
      queue_order: 1,
      notes: ["A source note."],
      files: {
        total: 13,
        bytes: 1300,
        by_parse_status: {},
        parsed: 5,
        partial: 2,
        metadata_only: 2,
        retained_only: 1,
        empty: 0,
        excluded: 0,
        pending: 2,
        needs_review: 1,
      },
      records: {
        status: "refreshing",
        total: null,
        dated: null,
        undated: null,
        first_observed: "2020-01-01",
        last_observed: "2024-01-01",
        measured_at: null,
      },
      extraction: {
        review_files: 1,
        pending_files: 2,
        history_absence_established: false,
      },
      classification: { status: "source_label_only", label: "Example archive" },
    },
  ],
  active_imports: [
    {
      id: "import-one",
      source_id: "actual-source-id",
      source: "Example archive",
      status: "running",
      files: { completed: 5, total: 10, fraction: 0.5 },
      bytes: { completed: 80, total: 100, fraction: 0.8 },
      events_reported: 9,
      started_at: "2026-11-03",
      error: "A bounded extraction error.",
    },
  ],
  queue: [
    {
      source_id: "actual-source-id",
      source: "Example archive",
      status: "queued",
      position: 1,
      snapshot_bytes: 100,
    },
  ],
  indexing: {
    catalog_max_event_rowid: 105,
    coverage_stats: {
      status: "refreshing",
      catalog_max_event_rowid: 100,
      measured_at: null,
    },
    daily: {
      status: "ready",
      watermark_rowid: 90,
      current_catalog_max_rowid: 105,
    },
  },
  backup: { status: "not_supplied" },
};
beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(report))),
  );
});

it('distinguishes current archive verification from completed inputs and shows an old heartbeat', async () => {
  const data = {...report, active_imports: [{...report.active_imports[0], activity: {
    phase: 'source_cleanup_verification', label: 'Verifying input before cleanup',
    filename: 'fictional-history.zip', retained_entries: 4500,
    progress: {completed: 250, total: 1000, fraction: .25},
    updated_at: '2025-01-01T00:00:00Z', seconds_since_update: 300,
  }}]};
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(data))));
  render(<CoverageWorkspace refreshKey={0} onBrowseSource={() => {}} />);
  expect(await screen.findByText('Verifying input before cleanup')).toBeTruthy();
  expect(screen.getByText('fictional-history.zip')).toBeTruthy();
  expect(screen.getByText('4,500 entries verified from this input')).toBeTruthy();
  expect(screen.getByText('Last update 5 min ago')).toBeTruthy();
  expect(screen.getByRole('progressbar', {name: 'Example archive: current check'}).getAttribute('aria-valuenow')).toBe('25');
  expect(screen.getByRole('progressbar', {name: 'Example archive: input bytes'}).getAttribute('aria-valuenow')).toBe('80');
  expect(screen.getByRole('progressbar', {name: 'Example archive: input files'}).getAttribute('aria-valuenow')).toBe('50');
});

it("sorts the full coverage source inventory without changing execution queue order", async () => {
  const user = userEvent.setup();
  const sample = report.sources[0];
  const data = {
    ...report,
    sources: [
      { ...sample, id: "unknown", name: "Unknown source" },
      { ...sample, id: "known", name: "Known source", records: { ...sample.records, total: 20 } },
      { ...sample, id: "zero", name: "Zero source", records: { ...sample.records, total: 0 } },
    ],
    queue: [
      { ...report.queue[0], source_id: "unknown", source: "Unknown source", position: 1 },
      { ...report.queue[0], source_id: "known", source: "Known source", position: 2 },
    ],
  };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(data))));
  render(<CoverageWorkspace refreshKey={0} onBrowseSource={vi.fn()} />);
  await screen.findByLabelText("Source coverage sort");
  const names = () => [...document.querySelectorAll(".coverage-source h3")].map(node => node.textContent);
  await user.selectOptions(screen.getByLabelText("Source coverage sort"), "records");
  expect(names()).toEqual(["Zero source", "Known source", "Unknown source"]);
  await user.selectOptions(screen.getByLabelText("Source coverage direction"), "desc");
  expect(names()).toEqual(["Known source", "Zero source", "Unknown source"]);
  expect([...document.querySelectorAll(".coverage-queue strong")].map(node => node.textContent)).toEqual(["Unknown source", "Known source"]);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("distinguishes input file and byte progress from retained inventory and unknown counts", async () => {
  const user = userEvent.setup();
  const browse = vi.fn();
  render(<CoverageWorkspace refreshKey={0} onBrowseSource={browse} />);
  expect(
    await screen.findByRole("heading", { name: "Source coverage" }),
  ).toBeTruthy();
  expect(
    screen
      .getByRole("progressbar", { name: "Example archive: input files" })
      .getAttribute("aria-valuenow"),
  ).toBe("50");
  expect(
    screen
      .getByRole("progressbar", { name: "Example archive: input bytes" })
      .getAttribute("aria-valuenow"),
  ).toBe("80");
  expect(screen.getAllByText("Counting…").length).toBeGreaterThan(0);
  expect(screen.queryByText("Unknown")).toBeNull();
  expect(screen.getByText(/do not establish continuous coverage/)).toBeTruthy();
  expect(
    screen.getByText(/Retained files alone do not verify a separate backup/),
  ).toBeTruthy();
  expect(screen.getByText("90")).toBeTruthy();
  expect(screen.getByText("105")).toBeTruthy();
  expect(screen.getByText(/Positions are not record counts/)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Browse records" }));
  expect(browse).toHaveBeenCalledWith("actual-source-id");
  expect(screen.getByText("A bounded extraction error.")).toBeTruthy();
});
