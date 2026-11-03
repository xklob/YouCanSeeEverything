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
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(report))),
  );
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
  expect(screen.getAllByText("Unknown").length).toBeGreaterThan(0);
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
