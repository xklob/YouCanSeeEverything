import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import MonthlySummary from "../src/MonthlySummary";

const coverage = {
  total_events_in_scope: 100,
  selected_events: 3,
  candidate_events: 12,
  undated_events_excluded: 2,
  sources: [
    {
      source_id: "real-id",
      source: "Readable source",
      total_events: 100,
      selected_events: 3,
      status: "selected",
    },
  ],
  retrieval_limited: true,
  time_basis: "source_dates",
  limitations: ["Based on selected evidence."],
};
const result = {
  kind: "period_summary",
  period: "2024-04",
  title: "A cited month",
  answer: "A month in retained evidence.",
  citations: ["evidence-one"],
  sections: [
    {
      heading: "A recurring thread",
      sentences: [
        {
          text: "A source-supported observation.",
          citations: ["evidence-one"],
        },
      ],
    },
  ],
  coverage,
  selection: {},
  model: "gpt-6.1-sol",
  cache: {
    status: "fresh",
    key: "cache-key",
    generated_at: "2026-11-03",
    fixed_watermark: "older",
    current_watermark: "older",
  },
  limitations: ["Health measurements are shown separately."],
};
let requests: { path: string; options?: RequestInit }[];
beforeEach(() => {
  requests = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const path = String(input);
      requests.push({ path, options });
      if (path === "/api/session")
        return new Response(JSON.stringify({ token: "synthetic-session" }));
      if (path === "/api/assistant")
        return new Response(
          JSON.stringify({ id: "summary-job", status: "queued" }),
        );
      if (path === "/api/jobs/summary-job")
        return new Response(
          JSON.stringify({
            id: "summary-job",
            status: "completed",
            kind: "summarize",
            prompt: "",
            created_at: "2026-11-03",
            result,
          }),
        );
      return new Response(
        JSON.stringify({
          status: "missing",
          period: "2024-04",
          model: "gpt-6.1-sol",
          key: "key",
          fixed_watermark: null,
          current_watermark: "new",
          current_coverage: coverage,
          result: null,
        }),
      );
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it.each([2, null])('renders bounded freshness coverage without inventing omitted counts or source status (total %s)', async (total) => {
  vi.mocked(fetch).mockResolvedValue(Response.json({status: 'missing', result: null,
    current_coverage: {total_events_in_scope: total, selected_events: 0,
      sources: [{source_id: 'synthetic-source', source: 'Synthetic archive', total_events: total, selected_events: 0}],
      time_basis: 'source_dates', limitations: ['No new narrative was generated.']},
  }));
  render(<MonthlySummary day="2024-04-05" sourceId="" onOpenEvent={() => {}} />);
  expect(await screen.findByText('No saved summary for this selection')).toBeTruthy();
  expect(screen.getByText(/Synthetic archive: 0 selected/)).toBeTruthy();
  expect(screen.queryByText(/retrieval candidates|undated records excluded/)).toBeNull();
  expect(screen.queryByText(/Only selected evidence was summarized|Evidence selection is shown below/)).toBeNull();
  if (total === null) expect(screen.getByText(/Scope count unavailable\./)).toBeTruthy();
  else expect(screen.getByText(/2 records in scope/)).toBeTruthy();
});

it("only generates on request, preserves actual source IDs, and opens cited original evidence", async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  render(
    <MonthlySummary day="2024-04-05" sourceId="real-id" onOpenEvent={open} />,
  );
  await user.click(screen.getByText("Monthly summary"));
  await screen.findByText("No saved summary for this selection");
  await user.clear(screen.getByLabelText("Summary month"));
  await user.type(screen.getByLabelText("Summary month"), "2024-05");
  await waitFor(() =>
    expect(requests.some((item) => item.path.includes("period=2024-05"))).toBe(
      true,
    ),
  );
  expect(
    requests.filter((item) => item.options?.method === "POST"),
  ).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Generate summary" }));
  expect(
    await screen.findByText("A source-supported observation."),
  ).toBeTruthy();
  const submission = requests.find((item) => item.options?.method === "POST")!;
  expect(JSON.parse(String(submission.options?.body))).toEqual({
    mode: "summarize",
    period: "2024-05",
    source_ids: ["real-id"],
    prompt: "",
    model: "gpt-6.1-sol",
  });
  await user.click(screen.getByRole("button", { name: "Open cited record 1" }));
  expect(open).toHaveBeenCalledWith("evidence-one");
  expect(
    screen.getByText("Health measurements are shown separately."),
  ).toBeTruthy();
  await user.selectOptions(
    screen.getByLabelText("Summary model"),
    "gpt-6-astra",
  );
  expect(
    requests.filter((item) => item.options?.method === "POST"),
  ).toHaveLength(1);
});

it("shows a stale cached narrative with readable citations without automatically refreshing it", async () => {
  vi.mocked(fetch).mockImplementation(
    async () =>
      new Response(
        JSON.stringify({
          status: "stale",
          period: "2024-04",
          model: "gpt-6.1-sol",
          key: "key",
          fixed_watermark: "older",
          current_watermark: "newer",
          current_coverage: coverage,
          result,
        }),
      ),
  );
  const user = userEvent.setup();
  render(<MonthlySummary day="2024-04-05" sourceId="" onOpenEvent={vi.fn()} />);
  await user.click(screen.getByText("Monthly summary"));
  expect(await screen.findByText(/stale for the current catalog/)).toBeTruthy();
  expect(screen.getByText("A source-supported observation.")).toBeTruthy();
  expect(document.body.textContent).not.toContain("[evidence-one]");
  expect(document.body.textContent).not.toContain('"sections"');
  expect(
    vi
      .mocked(fetch)
      .mock.calls.every(([, options]) => options?.method !== "POST"),
  ).toBe(true);
});

it("looks up trimmed focused summaries only after blur and submits the same focus", async () => {
  const user = userEvent.setup();
  render(
    <MonthlySummary
      day="2024-04-05"
      sourceId="real-id"
      onOpenEvent={vi.fn()}
    />,
  );
  await user.click(screen.getByText("Monthly summary"));
  await screen.findByText("No saved summary for this selection");
  const before = requests.filter((item) =>
    item.path.startsWith("/api/summaries?"),
  ).length;
  await user.type(screen.getByLabelText("Summary focus"), "  people  ");
  expect(
    requests.filter((item) => item.path.startsWith("/api/summaries?")),
  ).toHaveLength(before);
  await user.tab();
  await waitFor(() =>
    expect(
      requests.some(
        (item) =>
          item.path.startsWith("/api/summaries?") &&
          new URL(item.path, "http://localhost").searchParams.get("focus") ===
            "people",
      ),
    ).toBe(true),
  );
  expect(
    requests.filter((item) => item.options?.method === "POST"),
  ).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Generate summary" }));
  await waitFor(() =>
    expect(requests.some((item) => item.options?.method === "POST")).toBe(true),
  );
  expect(
    JSON.parse(
      String(
        requests.find((item) => item.options?.method === "POST")!.options?.body,
      ),
    ).prompt,
  ).toBe("people");
});
