import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import EnrichmentEvidence, {
  EnrichedLocation,
  publicEvidenceUrl,
} from "../src/EnrichmentEvidence";
import type { LifeEvent } from "../src/api";

const event: LifeEvent = {
  id: "registration-one",
  title: "Synthetic community event",
  body: "A registration source record.",
  occurred_at: "2024-04-05T00:00:00Z",
  source_id: "eventbrite",
  source: "Eventbrite",
  category: "registration",
  time_precision: "day",
  metadata: {
    enrichment: {
      location: {
        venue: "Community Hall",
        address: "123 Example Street",
        city: "Richmond",
        region: "Virginia",
        country: "US",
      },
      method: "manual_public_research",
      confidence: "high",
      time_precision: "day",
      original_values: {
        occurred_at: null,
        end_at: null,
        time_precision: "unknown",
      },
      reviewed_at: "2026-11-03T12:00:00Z",
      provenance: [
        {
          url: "https://example.org/events/community",
          kind: "official_event_page",
          evidence: "The public event page gives April 5 and Community Hall.",
          access_status: 200,
        },
        {
          url: "https://example.org/unavailable",
          kind: "unavailable_event_page",
          evidence: "This failed page cannot support a claim.",
          access_status: "HTTP 403",
        },
        {
          url: "javascript:alert(1)",
          kind: "unsafe_link",
          evidence: "Not a public source.",
        },
      ],
      notes: ["The original archive did not contain a usable event date."],
      personal_email: "must-not-render@example.org",
    },
  },
};
afterEach(cleanup);

it("shows reviewed venue context compactly on an event card", () => {
  render(<EnrichedLocation event={event} />);
  expect(screen.getByText("Community Hall · Richmond · Virginia")).toBeTruthy();
  expect(screen.getByText("Reviewed place")).toBeTruthy();
  expect(screen.queryByText(/must-not-render/)).toBeNull();
});

it("distinguishes reviewed dates and registration from attendance, with safe available supporting links", () => {
  render(<EnrichmentEvidence event={event} />);
  expect(
    screen.getByRole("heading", { name: "Date and place evidence" }),
  ).toBeTruthy();
  expect(screen.getByText("Reviewed public event sources")).toBeTruthy();
  expect(screen.getByText(/Original record date: unknown/)).toBeTruthy();
  expect(
    screen.getByText("Registration does not verify physical attendance."),
  ).toBeTruthy();
  expect(
    screen.getByText(/Community Hall, 123 Example Street, Richmond/),
  ).toBeTruthy();
  const source = screen.getByRole("link", { name: "Official event page" });
  expect(source.getAttribute("href")).toBe(
    "https://example.org/events/community",
  );
  expect(source.getAttribute("target")).toBe("_blank");
  expect(source.getAttribute("rel")).toBe("noopener noreferrer");
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(
    screen.getByText(/unavailable, not used as supporting evidence/),
  ).toBeTruthy();
  expect(
    screen.queryByText("This failed page cannot support a claim."),
  ).toBeNull();
  expect(screen.queryByText(/must-not-render/)).toBeNull();
  expect(screen.queryByText("Not a public source.")).toBeNull();
});

it("rejects executable, credential-bearing, and local links", () => {
  for (const url of [
    "javascript:alert(1)",
    "file:///private/archive",
    "http://127.0.0.1/private",
    "http://localhost:8000",
    "http://archive.local/path",
    "https://user:password@example.org/private",
  ])
    expect(publicEvidenceUrl(url)).toBeNull();
  expect(publicEvidenceUrl("https://example.org/public-event")).toBe(
    "https://example.org/public-event",
  );
});
